"""习题：按章出题 → 学生版 / 教师版两个 PDF。

OpenCode（booksmith-book + booksmith-exercises）在 exercises/chNN/ 下写 body/student/teacher.tex 并提交 →
流水线用 `sh tools/build_linux.sh --file exercises/chNN/{student,teacher}.tex` 编译（失败喂错误、新会话修，最多 3 次）→
程序检查：学生版 PDF 里不能出现"答案与解析"、两版题数一致、没有改动 exercises/ 以外的文件 → 产物两个 PDF + 说明。
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from ..base import Ctx, Result
from .checks import changed_files
from .common import check_cancel, commit_all, copy_artifact, task_chapter, task_params
from .flow import agent, build_and_fix, compose_summary, result, start_job, task_header

SKILLS = ["booksmith-book", "booksmith-exercises"]


def pdf_text(pdf: Path) -> str:
    try:
        p = subprocess.run(["pdftotext", "-layout", str(pdf), "-"], capture_output=True, timeout=120)
        return p.stdout.decode("utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return ""


class ExercisesPipeline:
    kind = "exercises"

    def run(self, ctx: Ctx) -> Result:
        job = start_job(ctx, SKILLS)
        tid = job.tid
        ch = task_chapter(ctx)
        if not ch:
            summary = compose_summary(job, title=f"习题 #{tid}：缺少章号",
                                      status_line="❌ 任务没有指定章号（参数 chapter，或需求里写“第 N 章”）。", checks=None)
            return result(job, "failed", summary, [])
        params = task_params(ctx)
        nn = f"ch{ch:02d}"
        count = params.get("count") or 10
        head = task_header(job, "出习题")
        prompt = (
            f"{head}\n\n## 你要做的\n"
            f"先读技能 booksmith-book，再读 booksmith-exercises，严格按它的流程给第 {ch} 章出 {count} 道题"
            "（题型比例、难度、用途按需求；需求没说的用技能里的默认值）。\n"
            f"- 产物：`exercises/{nn}/body.tex`、`exercises/{nn}/student.tex`、`exercises/{nn}/teacher.tex`；"
            "`exercises/common.tex` 不存在时从技能 templates/ 复制。模板在 `.opencode/skills/booksmith-exercises/templates/`。\n"
            f"- 编译：`sh tools/build_linux.sh --file exercises/{nn}/student.tex` 和 `... --file exercises/{nn}/teacher.tex`，"
            "两个都要通过，有错就修（最多 3 次）。\n"
            "- 不要修改任何章节正文（chapters/、ch4/、front/、main.tex、*.sty 一律不动）。\n"
            f"- `git add exercises/` 下你新建或改过的 .tex 文件（不要加 PDF 和编译产物），然后 `git commit`。\n"
            "- 最后一条消息只输出技能第 7 步规定的「最终说明」Markdown（题单、知识点覆盖、验算记录、查重、编译、待人确认）。"
        )
        r = agent(job, prompt, title=f"习题#{tid} 出题", step="出题")
        check_cancel(ctx)
        commit_all(ctx.workdir, f"agent: 习题 #{tid} 第 {ch} 章（流水线补提交）")

        stu = f"exercises/{nn}/student.tex"
        tea = f"exercises/{nn}/teacher.tex"
        if not (Path(ctx.workdir) / stu).is_file() or not (Path(ctx.workdir) / tea).is_file():
            summary = compose_summary(job, title=f"习题 #{tid}：失败",
                                      status_line=f"❌ 没有生成 {stu} / {tea}。"
                                                  + (f"OpenCode 会话错误：{r.error}" if not r.ok else ""), checks=None)
            return result(job, "failed", summary, [])

        fix_head = f"{head}\n\n（习题文件已经写好并提交，现在只修编译问题，不要改题目内容。）"
        builds = build_and_fix(job, [{"file": stu}, {"file": tea}], fix_prompt_head=fix_head, title=f"习题#{tid}")
        commit_all(ctx.workdir, f"agent: 习题 #{tid} 编译修复（流水线补提交）")
        compile_ok = all(b.rc == 0 for b in builds)

        # 程序检查
        problems, notes = [], []
        bs, bt = builds[0], builds[1]
        if bs.usable and bt.usable:
            ts, tt = pdf_text(bs.pdf), pdf_text(bt.pdf)
            if ts and "答案与解析" in ts:
                problems.append("学生版 PDF 里出现了“答案与解析”")
            if tt and "答案与解析" not in tt:
                problems.append("教师版 PDF 里没有答案框")
            n_s = len(re.findall(rf"(?m)^\s*{ch}\.\d+\s", ts))
            n_t = len(re.findall(rf"(?m)^\s*{ch}\.\d+\s", tt))
            body = (Path(ctx.workdir) / f"exercises/{nn}/body.tex").read_text(encoding="utf-8", errors="replace")
            n_src = len(re.findall(r"\\problem\{", body))
            notes.append(f"body.tex 中 \\problem 共 {n_src} 道（要求 {count} 道）")
            if n_src and abs(n_src - int(count)) > 0:
                notes.append(f"⚠ 题数与要求不一致：{n_src} ≠ {count}")
            if ts and tt and n_s != n_t:
                notes.append(f"按题号粗数：学生版 {n_s} 处、教师版 {n_t} 处（仅供参考）")
        outside = [p for st, p in changed_files(ctx.workdir, job.base)
                   if not p.startswith("exercises/")]
        if outside:
            problems.append("改动了 exercises/ 以外的文件：" + "、".join(outside[:10]))

        arts = []
        if bs.usable:
            arts.append(copy_artifact(bs.pdf, ctx.out_dir, "学生版.pdf", "pdf"))
        if bt.usable:
            arts.append(copy_artifact(bt.pdf, ctx.out_dir, "教师版.pdf", "pdf"))

        ok = compile_ok and not problems and bs.usable and bt.usable
        status_line = ("✅ 习题已生成：学生版、教师版编译通过，学生版不含答案。" if ok else
                       "❌ 需要人工处理：" + "；".join(problems or ["编译修复 3 次后仍失败"]))
        check_md = "\n".join(f"- {x}" for x in (problems + notes)) or "- 无问题"
        summary = compose_summary(job, title=f"习题 #{tid}：第 {ch} 章", status_line=status_line, checks=None,
                                  extra_sections=[("程序检查", check_md)])
        return result(job, "awaiting_review" if ok else "failed", summary, arts)
