"""不经网页，直接在命令行跑一条真实流水线（调试 / 在树莓派宿主机上实测用）。

模拟 worker 做的事：建分支和 worktree → 调 pipeline.run(ctx) → 提交遗留改动 → 写 changes.diff。
进度打印到终端，产物写进 <data>/tasks/<id>/。

例：
    python3 scripts/run_pipeline.py --kind maintain --repo ~/book --data ~/bs-data --id 1 \
        --title "统一术语" --request "第 5 章里……" --param chapter=5 --param full_build=false
    python3 scripts/run_pipeline.py --kind newchapter --id 7 --outline-file outline.md ...   # 阶段二
    python3 scripts/run_pipeline.py --kind slides --param chapter=5 --param pages=5 --param style=pptmaster-template

需要的环境变量与容器里一致：BOOKSMITH_MODEL_API_KEY、BOOKSMITH_OPENCODE_BIN、BOOKSMITH_BUILD_PREFIX（宿主机用 docker 编译时）等，
见 app/pipelines/real/common.py。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.bookrepo import BookRepo, branch_name  # noqa: E402
from app.config import load_dotenv  # noqa: E402
from app.pipelines.base import Ctx  # noqa: E402


def parse_value(v: str):
    try:
        return json.loads(v)
    except ValueError:
        return v


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", required=True, choices=["maintain", "exercises", "slides", "newchapter"])
    ap.add_argument("--repo")
    ap.add_argument("--data")
    ap.add_argument("--id", type=int, default=int(time.time()) % 100000)
    ap.add_argument("--title", default="命令行测试任务")
    ap.add_argument("--request", default="")
    ap.add_argument("--request-file")
    ap.add_argument("--param", action="append", default=[], help="key=value（value 按 JSON 解析，失败按字符串）")
    ap.add_argument("--outline-file", help="newchapter 阶段二：已确认的大纲")
    ap.add_argument("--feedback", default="")
    ap.add_argument("--parent-id", type=int)
    ap.add_argument("--branch", help="沿用已有分支（打回场景）")
    ap.add_argument("--env-file", default=None, help="先读取的 .env（只设置尚未存在的变量）")
    a = ap.parse_args()

    if a.env_file:
        load_dotenv(a.env_file)
    data = Path(a.data if a.data is not None else os.environ.get("DATA_DIR", "/data")).absolute()
    repo_dir = a.repo or os.environ.get("BOOK_REPO_DIR") or "/book"
    os.environ["DATA_DIR"] = str(data)
    from app.pipelines.real import PIPELINES   # 读完环境变量再导入
    pipe = {p.kind: p for p in PIPELINES}[a.kind]

    repo = BookRepo(repo_dir, data / "worktrees")
    request = Path(a.request_file).read_text(encoding="utf-8") if a.request_file else a.request
    params = {}
    for kv in a.param:
        k, _, v = kv.partition("=")
        params[k] = parse_value(v)
    branch = a.branch or branch_name(a.id, a.title, a.kind)
    task = {"id": a.id, "kind": a.kind, "title": a.title, "request": request, "params": params,
            "params_json": json.dumps(params, ensure_ascii=False), "branch": branch, "parent_id": a.parent_id,
            "outline": Path(a.outline_file).read_text(encoding="utf-8") if a.outline_file else "",
            "feedback": a.feedback, "status": "running"}
    workdir = repo.ensure_worktree(a.id, branch)
    out_dir = data / "tasks" / str(a.id)
    out_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    def log(step: str, msg: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')} +{int(time.time() - t0):4d}s] {step}：{msg}"
        print(line, flush=True)
        with (out_dir / "events.log").open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    stop_file = out_dir / "CANCEL"
    ctx = Ctx(task=task, workdir=workdir, out_dir=out_dir, log=log, cancelled=lambda: stop_file.exists())
    log("开始", f"kind={a.kind} 分支 {branch} 工作目录 {workdir}")
    res = pipe.run(ctx)
    BookRepo.commit_all(workdir, f"agent: #{a.id} 未提交的改动（脚本自动提交）")
    if res.status == "awaiting_review":
        (out_dir / "changes.diff").write_text(repo.diff(branch), encoding="utf-8")
    (out_dir / "result.json").write_text(json.dumps({"status": res.status, "artifacts": res.artifacts,
                                                     "cost": getattr(res, "cost", None)},
                                                    ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "summary.md").write_text(res.summary, encoding="utf-8")
    log("结束", f"状态 {res.status}，产物：{', '.join(x['name'] for x in res.artifacts)}，总用时 {int(time.time() - t0)}s")
    return 0 if res.status != "failed" else 1


if __name__ == "__main__":
    sys.exit(main())
