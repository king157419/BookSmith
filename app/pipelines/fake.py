"""FakePipeline：不调模型、不编译，几秒内产出假改动、假 PDF。四种 kind 都能用。

- maintain：在 worktree 里写 booksmith-fake/task-<id>.md 并提交，出一个 PDF。
- exercises：提交 exercises/chNN/fake-task-<id>.tex，出 学生版.pdf + 教师版.pdf。
- slides：提交 slides/chNN/fake-task-<id>.md，出 课件预览.pdf。
- newchapter：task["outline"] 为空时只出大纲 → awaiting_outline；确认后写正文。

每一步之间 sleep BOOKSMITH_FAKE_DELAY 秒（默认 0.5，测试里设 0），并检查取消。
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from ..bookrepo import BookRepo
from .base import Ctx, PipelineCancelled, Result


def minimal_pdf(lines: list[str]) -> bytes:
    """纯 Python 写一个单页 PDF（Helvetica，只支持 ASCII 文本）。"""
    def esc(s: str) -> str:
        s = s.encode("ascii", "replace").decode("ascii")
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    y = 780
    ops = ["BT", "/F1 16 Tf", f"72 {y} Td", f"({esc(lines[0] if lines else '')}) Tj", "/F1 11 Tf"]
    for line in lines[1:]:
        ops.append("0 -20 Td")
        ops.append(f"({esc(line)}) Tj")
    ops.append("ET")
    stream = "\n".join(ops).encode("latin-1")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


class FakePipeline:
    def __init__(self, kind: str):
        self.kind = kind
        self.delay = float(os.environ.get("BOOKSMITH_FAKE_DELAY") or "0.5")

    # ---- 小工具 ----
    def _step(self, ctx: Ctx, step: str, message: str) -> None:
        if ctx.cancelled():
            raise PipelineCancelled()
        ctx.log(step, message)
        if self.delay:
            time.sleep(self.delay)
        if ctx.cancelled():
            raise PipelineCancelled()

    def _pdf(self, ctx: Ctx, name: str, title: str) -> dict:
        t = ctx.task
        path = ctx.out_dir / name
        path.write_bytes(minimal_pdf([
            title,
            f"BookSmith FakePipeline - task #{t['id']} ({self.kind})",
            f"branch: {t.get('branch') or ''}",
            "This is a placeholder PDF. The real pipeline compiles the book with LaTeX.",
        ]))
        return {"name": name, "path": str(path), "kind": "pdf"}

    @staticmethod
    def _chapter(ctx: Ctx) -> str:
        ch = ctx.task.get("params", {}).get("chapter")
        try:
            return f"ch{int(ch):02d}"
        except (TypeError, ValueError):
            return "ch00"

    @staticmethod
    def _root_id(ctx: Ctx) -> int:
        """打回的子任务沿用父任务分支 agent/<根任务id>-...，演示文件也写回同一个。"""
        branch = ctx.task.get("branch") or ""
        try:
            return int(branch.split("/", 1)[1].split("-", 1)[0])
        except (IndexError, ValueError):
            return int(ctx.task["id"])

    def _write(self, ctx: Ctx, rel: str, text: str) -> Path:
        p = ctx.workdir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8", newline="\n") as f:
            f.write(text)
        return p

    def _note(self, ctx: Ctx) -> str:
        t = ctx.task
        lines = [f"\n## 任务 #{t['id']}：{t['title']}\n", f"\n需求：{t.get('request') or ''}\n"]
        if t.get("feedback"):
            lines.append(f"\n审核意见：{t['feedback']}\n")
        return "".join(lines)

    # ---- 主流程 ----
    def run(self, ctx: Ctx) -> Result:
        t = ctx.task
        self._step(ctx, "读稿", f"读取需求与相关章节（假流水线，kind={self.kind}）")
        if t.get("feedback"):
            self._step(ctx, "读稿", f"本轮带着审核意见继续改：{t['feedback'][:80]}")
        for a in t.get("params", {}).get("attachments", []):
            self._step(ctx, "读稿", f"读取附件：{a.get('name')}")

        if self.kind == "newchapter" and not (t.get("outline") or "").strip():
            self._step(ctx, "大纲", "检索资料并起草大纲")
            outline = (
                f"# {t['title']}\n\n"
                "1. 引言：为什么需要这一节\n"
                "2. 核心概念与定义\n"
                "3. 典型方法与对比\n"
                "4. 小结与习题建议\n"
            )
            return Result(status="awaiting_outline", summary=outline, artifacts=[])

        arts: list[dict] = []
        ch = self._chapter(ctx)
        rid = self._root_id(ctx)
        if self.kind == "maintain":
            self._step(ctx, "改稿", "修改书稿")
            self._write(ctx, f"booksmith-fake/task-{rid}.md", self._note(ctx))
            BookRepo.commit_all(ctx.workdir, f"agent: 维护 #{t['id']} {t['title']}")
            self._step(ctx, "编译", "编译受影响章节（假）")
            arts.append(self._pdf(ctx, "预览.pdf", "Maintenance preview"))
        elif self.kind == "exercises":
            count = t.get("params", {}).get("count") or 10
            self._step(ctx, "出题", f"生成 {count} 道题并验算答案")
            body = "".join(f"\\item 假题目 {i + 1}\n" for i in range(int(count)))
            self._write(ctx, f"exercises/{ch}/fake-task-{rid}.tex",
                        f"% {t['title']}\n\\begin{{enumerate}}\n{body}\\end{{enumerate}}\n")
            BookRepo.commit_all(ctx.workdir, f"agent: 习题 #{t['id']} {t['title']}")
            self._step(ctx, "编译", "生成学生版与教师版 PDF（假）")
            arts.append(self._pdf(ctx, "学生版.pdf", "Exercises - student"))
            arts.append(self._pdf(ctx, "教师版.pdf", "Exercises - teacher (with answers)"))
        elif self.kind == "slides":
            style = t.get("params", {}).get("style") or "简洁"
            self._step(ctx, "课件", f"按「{style}」风格生成课件")
            self._write(ctx, f"slides/{ch}/fake-task-{rid}.md", f"# {t['title']}\n\n- 风格：{style}\n")
            BookRepo.commit_all(ctx.workdir, f"agent: 课件 #{t['id']} {t['title']}")
            self._step(ctx, "编译", "导出课件预览 PDF（假）")
            arts.append(self._pdf(ctx, "课件预览.pdf", "Slides preview"))
        elif self.kind == "newchapter":
            self._step(ctx, "写作", "按确认的大纲分节写作")
            self._write(ctx, f"chapters/fake-task-{rid}.tex",
                        f"% {t['title']}\n% 大纲：\n" + "".join(f"% {l}\n" for l in t["outline"].splitlines()))
            BookRepo.commit_all(ctx.workdir, f"agent: 出新 #{t['id']} {t['title']}")
            self._step(ctx, "编译", "编译新章节（假）")
            arts.append(self._pdf(ctx, "新章节.pdf", "New chapter preview"))
        else:
            return Result(status="failed", summary=f"FakePipeline 不支持 kind={self.kind}")

        self._step(ctx, "自检", "章节数/字数/公式数无异常减少（假）")
        summary = (
            f"### 改了什么\n\n这是 **FakePipeline** 的演示结果（kind=`{self.kind}`）。\n\n"
            f"- 在分支 `{t.get('branch')}` 上提交了一个演示文件\n"
            f"- 产出 {len(arts)} 个 PDF\n\n"
            "### 自检\n\n| 检查 | 结果 |\n|---|---|\n| 编译 | 通过（假） |\n| 字数变化 | 正常 |\n"
        )
        if t.get("feedback"):
            summary += f"\n### 针对审核意见\n\n> {t['feedback']}\n"
        return Result(status="awaiting_review", summary=summary, artifacts=arts)
