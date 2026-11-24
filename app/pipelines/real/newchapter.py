"""出新：写新章节 / 新小节，两个阶段。

阶段一（task 没有 outline）：OpenCode（booksmith-book + booksmith-newchapter）只出大纲，存到
  chapters/chN/notes/outline*.md 并提交 → Result(status='awaiting_outline', summary=大纲 Markdown)。
阶段二（task["outline"] 是人确认/修改后的大纲）：按大纲写正文、接入 chapter.tex / main.tex →
  编译受影响章节（失败喂错误修，最多 3 次）→ 程序自检 → 全书编译 → 产物 PDF + 说明（diff 由 worker 生成）。
打回的子任务带 feedback 与原大纲，在同一分支上继续改。
"""
from __future__ import annotations

import re
from pathlib import Path

from ..base import Ctx, Result
from .checks import changed_files, run_checks
from .common import check_cancel, commit_all, copy_artifact, git, task_chapter, task_params
from .flow import (affected_chapters, agent, build_and_fix, compose_summary, fix_checks, result, start_job,
                   task_header)

SKILLS = ["booksmith-book", "booksmith-newchapter"]


class NewChapterPipeline:
    kind = "newchapter"

    def run(self, ctx: Ctx) -> Result:
        outline = (ctx.task.get("outline") or "").strip()
        return self._write(ctx, outline) if outline else self._outline(ctx)

    # ------------------------------------------------------------ 阶段一
    def _outline(self, ctx: Ctx) -> Result:
        job = start_job(ctx, SKILLS)
        tid = job.tid
        head = task_header(job, "写新内容 · 阶段一：只出大纲")
        prompt = (
            f"{head}\n\n## 你要做的（阶段一）\n"
            "先读技能 booksmith-book，再读 booksmith-newchapter，按「阶段一：只出大纲」完成：查重与衔接（grep 书里已有的相关内容）、"
            "读相邻章节的写法、按模板写大纲。\n"
            "- 你没有联网检索工具：计划引用的资料只能来自附件和书稿已有的参考文献表，不要凭记忆列文献；资料不够就写进「缺口与风险」。\n"
            "- 大纲存为 `chapters/chN/notes/outline-<英文短主题>.md`（新整章用 `outline.md`），只 `git add` 这个文件并提交。\n"
            "- **不写正文，不改任何 .tex，不改 main.tex。**\n"
            "- 最后一条消息原样输出完整的大纲 Markdown（不要加别的话）。"
        )
        r = agent(job, prompt, title=f"出新#{tid} 大纲", step="大纲")
        check_cancel(ctx)
        commit_all(ctx.workdir, f"agent: 出新 #{tid} 大纲（流水线补提交）")
        files = [p for st, p in changed_files(ctx.workdir, job.base) if re.search(r"notes/outline[^/]*\.md$", p)]
        text = ""
        if files:
            text = (Path(ctx.workdir) / files[-1]).read_text(encoding="utf-8", errors="replace").strip()
        if len(text) < 200 and r.text.strip():
            text = r.text.strip()
        if len(text) < 200:
            summary = compose_summary(job, title=f"出新 #{tid}：大纲生成失败",
                                      status_line="❌ 没有得到大纲。" + (f"会话错误：{r.error}" if not r.ok else ""),
                                      checks=None)
            return result(job, "failed", summary, [])
        stray = [p for st, p in changed_files(ctx.workdir, job.base) if p.endswith(".tex") or p == "main.tex"]
        note = ""
        if stray:
            note = "\n\n> ⚠ 阶段一本不应改 .tex，但智能体改了：" + "、".join(stray) + "（审核时请留意）"
        res = result(job, "awaiting_outline", text + note, [])
        # 大纲阶段：说明.md 就是大纲；summary 必须是大纲本身（网页把它当作可编辑的大纲草稿）
        return res

    # ------------------------------------------------------------ 阶段二
    def _write(self, ctx: Ctx, outline: str) -> Result:
        job = start_job(ctx, SKILLS)
        tid = job.tid
        params = task_params(ctx)
        head = task_header(job, "写新内容 · 阶段二：按确认的大纲写正文")
        prompt = (
            f"{head}\n\n## 已确认的大纲（以此为准；人改过的地方就是要求）\n\n{outline}\n\n## 你要做的（阶段二）\n"
            "先读技能 booksmith-book，再读 booksmith-newchapter，按「阶段二」完成：\n"
            "- 文件放法与接入方式严格按技能（已有章加节：新文件 + 该章 chapter.tex 加一行 \\input；新整章：main.tex 只加一行 "
            "`\\input{chapters/chN/chapter}`；绝不创建 ACCEPTED）。\n"
            "- 逐节写，每写完一节用 `sh tools/build_linux.sh --chapter N` 编译一次，有错就修（最多 3 次）。"
            "**不要运行全书编译**（流水线会做）。\n"
            "- 引用只用附件、书稿已有文献表里能确认的资料；不确定的事实按技能标 `% 待核实` 并记入 notes/verify.md。\n"
            "- 每节写完就 `git add <具体文件>` 并 `git commit`。\n"
            "- 最后一条消息只输出技能规定的「最终说明」Markdown。"
        )
        r = agent(job, prompt, title=f"出新#{tid} 正文", step="写作")
        check_cancel(ctx)
        commit_all(ctx.workdir, f"agent: 出新 #{tid} 正文（流水线补提交）")

        chapters, full_needed = affected_chapters(ctx.workdir, job.base)
        ch_hint = task_chapter(ctx)
        if not chapters and ch_hint:
            chapters = [ch_hint]
        tex_changed = [p for st, p in changed_files(ctx.workdir, job.base) if p.endswith(".tex")]
        if not tex_changed:
            summary = compose_summary(job, title=f"出新 #{tid}：没有写出正文",
                                      status_line="❌ 智能体没有新增或修改任何 .tex。" + (f"会话错误：{r.error}" if not r.ok else ""),
                                      checks=None)
            return result(job, "failed", summary, [])

        fix_head = (f"{head}\n\n## 已确认的大纲\n\n{outline}\n\n（正文已经写好并提交，现在只处理下面的问题，不要扩大改动范围。）")
        ch_results = build_and_fix(job, [{"chapter": c} for c in chapters], fix_prompt_head=fix_head,
                                   title=f"出新#{tid}") if chapters else []
        compile_ok = all(b.rc == 0 for b in ch_results)
        allowed = ("main.tex",)
        rep = run_checks(ctx.workdir, allow_shrink=bool(params.get("allow_shrink")),
                         build_logs=[b.output for b in ch_results], allowed_forbidden=allowed)
        if not rep.ok and compile_ok:
            ctx.log("自检", "发现问题，交给智能体修一次：" + "；".join(rep.problems())[:300])
            fix_checks(job, rep, fix_prompt_head=fix_head, title=f"出新#{tid}")
            commit_all(ctx.workdir, f"agent: 出新 #{tid} 自检修复（流水线补提交）")
            if chapters:
                ch_results = build_and_fix(job, [{"chapter": c} for c in chapters], fix_prompt_head=fix_head,
                                           title=f"出新#{tid}", max_rounds=1)
                compile_ok = all(b.rc == 0 for b in ch_results)
            rep = run_checks(ctx.workdir, allow_shrink=bool(params.get("allow_shrink")),
                             build_logs=[b.output for b in ch_results], allowed_forbidden=allowed)
        # main.tex 只允许加一行
        diff = git(ctx.workdir, "diff", "--numstat", job.base, "--", "main.tex").stdout.split()
        if diff and (int(diff[0] or 0) > 1 or int(diff[1] or 0) > 0):
            rep.forbidden.append(f"main.tex 改动超过一行（+{diff[0]} -{diff[1]}）")

        arts = []
        for b in ch_results:
            if b.usable:
                arts.append(copy_artifact(b.pdf, ctx.out_dir, f"第{b.target.strip('第 章')}章预览.pdf", "pdf"))
        full_ok = None
        if compile_ok and rep.ok and params.get("full_build", True) not in (False, "false", "0", 0):
            fb = build_and_fix(job, [{}], fix_prompt_head=fix_head, title=f"出新#{tid}", max_rounds=1)
            full_ok = fb[-1].rc == 0
            if fb[-1].usable:
                arts.append(copy_artifact(fb[-1].pdf, ctx.out_dir, "全书.pdf", "pdf"))
        commit_all(ctx.workdir, f"agent: 出新 #{tid} 编译修复（流水线补提交）")

        ok = compile_ok and rep.ok and full_ok is not False
        if ok:
            status_line = "✅ 新内容已写好：章节编译通过，程序自检通过" + ("，全书编译通过。" if full_ok else "。") + \
                          "新画的图未经目视检查，请重点看 PDF 里的图。"
        else:
            why = [w for w, bad in (("章节编译修复 3 次后仍失败", not compile_ok), ("程序自检未通过", not rep.ok),
                                    ("全书编译未通过", full_ok is False)) if bad]
            status_line = "❌ 需要人工处理：" + "、".join(why) + f"。改动保留在分支 `{ctx.task.get('branch')}` 上。"
        summary = compose_summary(job, title=f"出新 #{tid}：{ctx.task.get('title') or ''}",
                                  status_line=status_line, checks=rep)
        return result(job, "awaiting_review" if ok else "failed", summary, arts)
