"""真实流水线（OpenCode + DeepSeek + 容器内 TeX Live）。BOOKSMITH_PIPELINES=real 时由 app.pipelines.load_pipelines 加载。

- maintain    维护已有内容           maintain.py
- exercises   按章出题（学生版/教师版） exercises.py
- slides      按章做课件（PPT Master / Beamer）slides.py
- newchapter  写新章节（大纲 → 正文）  newchapter.py

共用：opencode_runner.py（调 OpenCode）、checks.py（自检）、flow.py（编译-修复循环、说明拼装）、common.py。
"""
from __future__ import annotations

from .exercises import ExercisesPipeline
from .maintain import MaintainPipeline
from .newchapter import NewChapterPipeline
from .slides import SlidesPipeline

PIPELINES = [MaintainPipeline(), ExercisesPipeline(), SlidesPipeline(), NewChapterPipeline()]


def register_all() -> list[str]:
    """不经 load_pipelines 时手动注册（脚本、测试用）。"""
    from ..base import register
    for p in PIPELINES:
        register(p)
    return [p.kind for p in PIPELINES]
