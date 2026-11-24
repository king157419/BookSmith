"""单线程任务队列。

方案：应用启动时（FastAPI lifespan）起一个守护线程，循环取最早的 queued 任务执行，
同一时间只跑一个任务（书稿仓库改动天然串行）。新任务入队时 wake() 立即唤醒，
否则每 2 秒轮询一次。测试里关掉线程，直接调用 run_once()。

注意：uvicorn 只能开 1 个 worker 进程（--workers 1），否则会有多个队列线程。
"""
from __future__ import annotations

import logging
import shutil
import threading
import traceback
from pathlib import Path

from . import pipelines
from .bookrepo import BookRepo, RepoError, branch_name
from .config import Settings
from .db import ARTIFACT_KINDS, DB
from .pipelines.base import RESULT_STATUSES, Ctx, PipelineCancelled, Result

log = logging.getLogger("booksmith.worker")


def _guess_kind(name: str) -> str:
    ext = Path(name).suffix.lower().lstrip(".")
    return {"pdf": "pdf", "pptx": "pptx", "diff": "diff", "patch": "diff", "md": "md",
            "log": "log", "txt": "log", "png": "png"}.get(ext, "md")


class Worker:
    def __init__(self, settings: Settings, db: DB, repo: BookRepo):
        self.settings = settings
        self.db = db
        self.repo = repo
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.current_task_id: int | None = None

    # ---------- 线程控制 ----------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="booksmith-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout)

    def wake(self) -> None:
        self._wake.set()

    def _loop(self) -> None:
        log.info("任务队列线程已启动")
        while not self._stop.is_set():
            try:
                did = self.run_once()
            except Exception:  # 不让线程死掉
                log.exception("worker 循环异常")
                did = False
            if not did:
                self._wake.wait(2.0)
                self._wake.clear()

    # ---------- 执行一个任务 ----------
    def run_once(self) -> bool:
        task = self.db.claim_next_queued()
        if task is None:
            return False
        self.current_task_id = task["id"]
        try:
            self._run_task(task)
        finally:
            self.current_task_id = None
        return True

    def _run_task(self, task: dict) -> None:
        tid = task["id"]
        db = self.db

        def logf(step: str, message: str, level: str = "info") -> None:
            db.add_event(tid, step, message, level)

        def cancelled() -> bool:
            return db.get_status(tid) == "cancelled"

        logf("worker", "开始执行")
        pipeline = pipelines.get_pipeline(task["kind"])
        if pipeline is None:
            self._finish_failed(tid, f"没有注册 kind={task['kind']} 的流水线"
                                     f"（BOOKSMITH_PIPELINES={self.settings.pipelines}）。")
            return

        # 分支与 worktree
        branch = task.get("branch") or branch_name(tid, task["title"], task["kind"])
        try:
            workdir = self.repo.ensure_worktree(tid, branch)
        except (RepoError, OSError) as e:
            self._finish_failed(tid, f"准备书稿分支失败：{e}")
            return
        if task.get("branch") != branch:
            db.update_task(tid, branch=branch)
            task["branch"] = branch
        logf("worker", f"分支 {branch}，工作目录已就绪")

        out_dir = self.settings.tasks_dir / str(tid)
        out_dir.mkdir(parents=True, exist_ok=True)
        ctx = Ctx(task=task, workdir=workdir, out_dir=out_dir,
                  log=lambda step, msg: logf(step, msg), cancelled=cancelled)

        try:
            result = pipeline.run(ctx)
        except PipelineCancelled:
            result = None
        except Exception as e:
            log.exception("任务 #%s 流水线异常", tid)
            (out_dir / "error.log").write_text(traceback.format_exc(), encoding="utf-8")
            db.add_artifact(tid, "error.log", str(out_dir / "error.log"), "log")
            self._finish_failed(tid, f"流水线异常：{type(e).__name__}: {e}")
            return

        if cancelled() or result is None:
            logf("worker", "任务已取消", "warn")
            db.update_task(tid, expect_status=("running", "cancelled"), status="cancelled")
            self.repo.remove_worktree(tid)
            return

        if not isinstance(result, Result) or result.status not in RESULT_STATUSES:
            self._finish_failed(tid, f"流水线返回了无效结果：{result!r}")
            return

        # 兜底：提交流水线留下的未提交改动
        try:
            if BookRepo.commit_all(workdir, f"agent: #{tid} 未提交的改动（worker 自动提交）"):
                logf("worker", "已自动提交流水线遗留的改动", "warn")
        except RepoError as e:
            logf("worker", f"自动提交失败：{e}", "warn")

        self._register_artifacts(tid, out_dir, result.artifacts)
        if getattr(result, "cost", None):
            try:
                import json
                db.update_task(tid, cost_json=json.dumps(result.cost, ensure_ascii=False))
            except Exception as e:  # 记用量失败不影响任务
                logf("worker", f"记录用量失败：{e}", "warn")

        if result.status == "awaiting_outline":
            db.update_task(tid, expect_status="running", status="awaiting_outline", summary=result.summary)
            logf("worker", "大纲已生成，等待确认")
        elif result.status == "awaiting_review":
            try:
                diff = self.repo.diff(branch)
                (out_dir / "changes.diff").write_text(diff, encoding="utf-8")
                if not any(a["name"] == "changes.diff" for a in db.list_artifacts(tid)):
                    db.add_artifact(tid, "changes.diff", str(out_dir / "changes.diff"), "diff")
            except RepoError as e:
                logf("worker", f"生成 diff 失败：{e}", "warn")
            db.update_task(tid, expect_status="running", status="awaiting_review", summary=result.summary)
            logf("worker", "完成，等待审核")
        else:
            self._finish_failed(tid, "流水线报告失败", summary=result.summary)

    def _register_artifacts(self, tid: int, out_dir: Path, artifacts: list[dict]) -> None:
        out_real = out_dir.resolve()
        for a in artifacts or []:
            try:
                src = Path(a["path"])
                name = a.get("name") or src.name
                kind = a.get("kind") or _guess_kind(name)
                if kind not in ARTIFACT_KINDS:
                    kind = _guess_kind(name)
                if not src.is_file():
                    self.db.add_event(tid, "worker", f"产物不存在，已跳过：{name}", "warn")
                    continue
                if out_real not in src.resolve().parents:
                    dst = out_dir / Path(name).name
                    shutil.copyfile(src, dst)
                    src = dst
                self.db.add_artifact(tid, name, str(src.resolve()), kind)
            except Exception as e:  # 单个产物出错不影响整体
                self.db.add_event(tid, "worker", f"登记产物失败：{a!r}：{e}", "warn")

    def _finish_failed(self, tid: int, message: str, summary: str | None = None) -> None:
        self.db.add_event(tid, "worker", message, "error")
        fields = {"status": "failed"}
        if summary:
            fields["summary"] = summary
        elif not (self.db.get_task(tid) or {}).get("summary"):
            fields["summary"] = message
        self.db.update_task(tid, expect_status="running", **fields)
        try:
            self.repo.remove_worktree(tid)
        except Exception:
            pass
