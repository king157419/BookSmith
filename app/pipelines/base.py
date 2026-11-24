"""流水线接口（docs/04-架构与接口.md「流水线接口」）与注册表。

写一条真实流水线只需要：
1. 实现一个类，带 `kind` 属性和 `run(ctx) -> Result` 方法；
2. 在 `app/pipelines/real.py`（或 real/ 包）里放一个模块级列表 `PIPELINES = [MaintainPipeline(), ...]`；
3. 设环境变量 `BOOKSMITH_PIPELINES=real`。

worker 负责的事（流水线不用管）：
- 建分支和 worktree（ctx.workdir 已经检出 `agent/<id>-<slug>`），打回的子任务沿用同一分支；
- 运行结束后把 worktree 里没提交的改动自动提交一次；
- status 为 awaiting_review 时，生成相对 master 的 `changes.diff` 产物（流水线不必自己出 diff）；
- 把 Result.artifacts 里不在 out_dir 下的文件复制进 out_dir 再登记；
- 捕获异常（记为 failed）、处理取消、合并后清理 worktree。

流水线要做的事：
- 在 ctx.workdir 里改书稿并 git 提交（可用 `app.bookrepo.BookRepo.commit_all`）；
- 产物（PDF/PPTX/MD…）写进 ctx.out_dir；
- 用 ctx.log(step, message) 汇报进度（网页实时显示），长循环里经常检查 ctx.cancelled()；
- 返回 Result。status='awaiting_outline' 时，summary 就是大纲草稿（Markdown），
  用户确认/修改后 ctx.task["outline"] 会是确认后的大纲，任务重新排队再次调用 run。

ctx.task 是 tasks 表一行转成的 dict，另加 `params`（已解析的 params_json），
其中 `params["attachments"]` 是上传附件列表 [{"name","path","size"}]。
打回产生的子任务：`task["parent_id"]` 指向原任务，`task["feedback"]` 是审核意见，
`task["outline"]` 沿用原任务的大纲。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol, runtime_checkable

RESULT_STATUSES = ("awaiting_review", "awaiting_outline", "failed")


@dataclass
class Ctx:
    task: dict                         # tasks 表一行（含 params、outline、feedback）
    workdir: Path                      # 本任务专用的书稿 git worktree（分支 agent/<id>-<slug>）
    out_dir: Path                      # 本任务产物目录（DATA_DIR/tasks/<id>/）
    log: Callable[[str, str], None]    # log(step, message)
    cancelled: Callable[[], bool]


@dataclass
class Result:
    status: str                        # 'awaiting_review' | 'awaiting_outline' | 'failed'
    summary: str                       # 给人看的"改了什么、为什么、自检结果"（Markdown）
    artifacts: list[dict] = field(default_factory=list)   # [{"name","path","kind"}]
    cost: dict | None = None           # 可选：模型用量（词元、花费），worker 写入 tasks.cost_json


@runtime_checkable
class Pipeline(Protocol):
    kind: str

    def run(self, ctx: Ctx) -> Result: ...


class PipelineCancelled(Exception):
    """流水线发现 ctx.cancelled() 为真时可以抛这个异常，worker 会把任务记为已取消。"""


# ---------------- 注册表 ----------------
_REGISTRY: dict[str, Pipeline] = {}


def register(pipeline: Pipeline) -> Pipeline:
    if not getattr(pipeline, "kind", None):
        raise ValueError("pipeline 缺少 kind")
    _REGISTRY[pipeline.kind] = pipeline
    return pipeline


def get_pipeline(kind: str) -> Pipeline | None:
    return _REGISTRY.get(kind)


def registered_kinds() -> list[str]:
    return sorted(_REGISTRY)


def clear_registry() -> None:
    _REGISTRY.clear()
