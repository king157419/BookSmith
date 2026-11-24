"""按 BOOKSMITH_PIPELINES 选择并注册流水线：fake（默认，开发/测试）| real。"""
from __future__ import annotations

import importlib
import logging

from .base import (Ctx, Pipeline, PipelineCancelled, Result, clear_registry, get_pipeline,
                   register, registered_kinds)

log = logging.getLogger("booksmith.pipelines")

__all__ = ["Ctx", "Result", "Pipeline", "PipelineCancelled", "register", "get_pipeline",
           "registered_kinds", "load_pipelines"]


def load_pipelines(mode: str = "fake") -> list[str]:
    clear_registry()
    if mode == "fake":
        from .fake import FakePipeline
        for kind in ("maintain", "exercises", "slides", "newchapter"):
            register(FakePipeline(kind))
    elif mode == "real":
        try:
            mod = importlib.import_module("app.pipelines.real")
        except ImportError as e:
            log.error("BOOKSMITH_PIPELINES=real，但无法导入 app.pipelines.real：%s", e)
            return []
        for p in getattr(mod, "PIPELINES", []):
            register(p)
    else:
        raise ValueError(f"未知的 BOOKSMITH_PIPELINES：{mode!r}（应为 fake 或 real）")
    kinds = registered_kinds()
    log.info("已注册流水线（%s）：%s", mode, ", ".join(kinds) or "无")
    return kinds
