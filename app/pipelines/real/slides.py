"""课件：按章（或几节）做一讲 PPT。

任务参数：chapter（必填）、sections（可选，如 "5.1-5.3"）、pages（默认 12）、
style = pptmaster-template（默认，套老师模板背景）| pptmaster-default（PPT Master 简洁风格）| beamer，
template（模板包名，默认 BOOKSMITH_SLIDES_TEMPLATE 或 shu）。

PPT Master 路线（不让模型跑 PPT Master 的完整工作流——那套说明 600KB+，便宜模型跟不住；
由流水线确定性地调用它的脚本，模型只负责"按约定写 SVG 页面"）：
  OpenCode（booksmith-book + booksmith-slides）读章节 → 写 outline.md + svg/NN_*.svg (+ notes.md) 并提交 →
  流水线：建 PPT Master 项目（slides/chNN/<deck>/_work，不进 git）→ 复制 SVG、按页面类型铺模板背景 →
  compact_svg_styles 规整 → svg_quality_checker（final）→ 有错误把报告喂回新会话修 SVG（最多 3 次）→
  notes.md → total_md_split → svg_to_pptx --quick-generate → 课件.pptx；
  再用 PyMuPDF 把 SVG 渲染成 课件预览.pdf（预览与 PowerPoint 渲染略有差别，以 PPTX 为准）。
Beamer 路线：模型写 slides.tex → build_linux.sh --file → 课件.pdf；再把每页渲染成图片拼成 课件（图片版）.pptx。
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import sys
from pathlib import Path

from ..base import Ctx, Result
from .checks import changed_files
from .common import (check_cancel, commit_all, copy_artifact, data_dir, env_path, pptmaster_dir, run_proc,
                     task_chapter, task_params, truncate)
from .flow import agent, build_and_fix, compose_summary, result, start_job, task_header

SKILLS = ["booksmith-book", "booksmith-slides"]
STYLES = ("pptmaster-template", "pptmaster-default", "beamer")
MAX_SVG_FIX = 3


def templates_root() -> Path:
    return env_path("BOOKSMITH_SLIDE_TEMPLATES", data_dir() / "slide-templates")


def pptm_skill_dir() -> Path:
    d = pptmaster_dir()
    for c in (d / "skills" / "ppt-master", d):
        if (c / "scripts" / "svg_to_pptx.py").is_file():
            return c
    return d / "skills" / "ppt-master"


def pptm_python() -> str:
    return os.environ.get("BOOKSMITH_PPTMASTER_PYTHON", "") or sys.executable


def deck_slug(ctx: Ctx, ch: int) -> str:
    p = task_params(ctx)
    secs = str(p.get("sections") or "").strip()
    base = f"ch{ch:02d}"
    if secs:
        base += "-" + re.sub(r"[^0-9a-zA-Z]+", "_", secs).strip("_")
    return f"{base}-t{ctx.task.get('id')}" if not ctx.task.get("parent_id") else f"{base}-t{ctx.task.get('parent_id')}"


class SlidesPipeline:
    kind = "slides"

    def run(self, ctx: Ctx) -> Result:
        params = task_params(ctx)
        style = str(params.get("style") or "").strip() or "pptmaster-template"
        if style not in STYLES:
            style = "pptmaster-template"
        tpl_name = str(params.get("template") or os.environ.get("BOOKSMITH_SLIDES_TEMPLATE", "shu"))
        tpl_dir = templates_root() / tpl_name
        notes_start = []
        if style == "pptmaster-template" and not (tpl_dir / "template.json").is_file():
            notes_start.append(f"找不到模板包 {tpl_dir}，改用 pptmaster-default 风格。")
            style = "pptmaster-default"

        job = start_job(ctx, SKILLS)
        job.warnings += notes_start
        tid = job.tid
        ch = task_chapter(ctx)
        if not ch:
            return result(job, "failed", compose_summary(job, title=f"课件 #{tid}：缺少章号",
                          status_line="❌ 任务没有指定章号（参数 chapter，或需求里写“第 N 章”）。", checks=None), [])
        pages = params.get("pages") or 12
        secs = params.get("sections") or "整章"
        slug = deck_slug(ctx, ch)
        deck = f"slides/ch{ch:02d}/{slug}"
        deck_dir = Path(ctx.workdir) / deck
        tpl_rel = None
        if style == "pptmaster-template":
            # 模板说明复制进 worktree（.opencode/ 不进 git），让模型能读到
            dst = Path(ctx.workdir) / ".opencode" / "slide-template"
            shutil.rmtree(dst, ignore_errors=True)
            shutil.copytree(tpl_dir, dst, ignore=shutil.ignore_patterns("*.png", "*.jpg", "*.emf"))
            tpl_rel = ".opencode/slide-template/template.md"
        ctx.log("准备", f"风格 {style}" + (f"，模板 {tpl_name}" if tpl_rel else "") + f"，目录 {deck}")

        head = task_header(job, "做课件")
        style_hint = {
            "pptmaster-template": f"风格 `pptmaster-template`：先读模板说明 `{tpl_rel}`，背景由流水线铺，你不要画背景。",
            "pptmaster-default": "风格 `pptmaster-default`：按技能 2.3 节的简洁风格。",
            "beamer": "风格 `beamer`：按技能第三节写 slides.tex 并编译通过。",
        }[style]
        prompt = (
            f"{head}\n\n## 你要做的\n"
            f"先读技能 booksmith-book，再读 booksmith-slides，给第 {ch} 章（范围：{secs}）做一讲课件，约 {pages} 页。\n"
            f"- 课件目录：`{deck}/`（outline.md、" + ("slides.tex" if style == "beamer" else "svg/NN_xxx.svg、notes.md") + "）。\n"
            f"- {style_hint}\n"
            "- 内容只取自书稿；不改任何章节正文。\n"
            f"- 写完 `git add {deck}/` 下的源文件并 `git commit`。\n"
            "- 最后一条消息输出技能第四节规定的最终说明。"
        )
        r = agent(job, prompt, title=f"课件#{tid} 制作", step="课件")
        check_cancel(ctx)
        commit_all(ctx.workdir, f"agent: 课件 #{tid} 第 {ch} 章（流水线补提交）")
        outside = [p for st, p in changed_files(ctx.workdir, job.base) if not p.startswith("slides/")]
        if outside:
            job.warnings.append("改动了 slides/ 以外的文件：" + "、".join(outside[:10]))

        if style == "beamer":
            return self._beamer(ctx, job, deck, ch, head)
        return self._pptmaster(ctx, job, deck, deck_dir, ch, style, tpl_dir, head, r)

    # ------------------------------------------------------------ PPT Master
    def _pptmaster(self, ctx, job, deck, deck_dir, ch, style, tpl_dir, head, r) -> Result:
        tid = job.tid
        svg_src = deck_dir / "svg"
        svgs = sorted(svg_src.glob("*.svg")) if svg_src.is_dir() else []
        if not svgs:
            return result(job, "failed", compose_summary(job, title=f"课件 #{tid}：没有生成页面",
                          status_line=f"❌ `{deck}/svg/` 下没有 SVG。" + (f"会话错误：{r.error}" if not r.ok else ""),
                          checks=None), [])
        skill = pptm_skill_dir()
        if not (skill / "scripts" / "svg_to_pptx.py").is_file():
            return result(job, "failed", compose_summary(job, title=f"课件 #{tid}：缺少 PPT Master",
                          status_line=f"❌ 找不到 PPT Master（{skill}）；SVG 源文件已提交在分支上。", checks=None), [])
        py = pptm_python()
        work = deck_dir / "_work"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        pr = run_proc([py, str(skill / "scripts" / "project_manager.py"), "init", "deck", "--quick-generate"], work, ctx, timeout=120)
        projs = sorted((work / "projects").glob("deck_*")) if (work / "projects").is_dir() else []
        if pr.rc != 0 or not projs:
            return result(job, "failed", compose_summary(job, title=f"课件 #{tid}：PPT Master 初始化失败",
                          status_line="❌ project_manager.py init 失败：\n```\n" + pr.output[-1500:] + "\n```", checks=None), [])
        proj = projs[-1]
        bgs = {}
        if style == "pptmaster-template":
            spec = json.loads((tpl_dir / "template.json").read_text(encoding="utf-8"))
            bgs = spec.get("backgrounds") or {}
            (proj / "images").mkdir(exist_ok=True)
            for f in set(bgs.values()):
                if (tpl_dir / f).is_file():
                    shutil.copyfile(tpl_dir / f, proj / "images" / f)

        report = {}
        errors: dict[str, list[str]] = {}
        for rnd in range(MAX_SVG_FIX + 1):
            check_cancel(ctx)
            self._stage_svgs(svg_src, proj, bgs)
            run_proc([py, str(skill / "scripts" / "compact_svg_styles.py"), str(proj / "svg_output"), "--inplace"],
                     work, ctx, timeout=300)
            ck = run_proc([py, str(skill / "scripts" / "svg_quality_checker.py"), str(proj), "--quick-generate",
                           "--canonical-authoring", "--stage", "final", "--json"], work, ctx, timeout=600)
            rep_path = proj / "validation" / "svg_quality_report.json"
            report = json.loads(rep_path.read_text(encoding="utf-8")) if rep_path.is_file() else {}
            errors = {f["file"]: f.get("errors") or [] for f in report.get("files", []) if f.get("errors")}
            n_err = sum(len(v) for v in errors.values())
            ctx.log("检查", f"PPT Master 检查：{len(svgs)} 页，{n_err} 个错误" + (f"（第 {rnd + 1} 轮）" if rnd else ""))
            if not errors and ck.rc == 0:
                break
            if not errors and ck.rc != 0:
                errors = {"(整体)": [truncate(ck.output[-1500:], 1500)]}
            if rnd == MAX_SVG_FIX:
                break
            digest = "\n".join(f"### {f}\n" + "\n".join(f"- {truncate(e, 400)}" for e in errs[:8])
                               for f, errs in list(errors.items())[:12])
            fix = (f"{head}\n\n（课件 SVG 已写好并提交在 `{deck}/svg/`，现在只修下面 PPT Master 检查报出的错误。）\n\n"
                   "先读技能 booksmith-slides 的 2.2 节硬规则，然后逐条修改对应的 SVG 源文件（"
                   f"`{deck}/svg/` 下的同名文件；报告里的路径是导出副本，不要改副本）。不要改页面内容和页数，只修格式问题。"
                   "改完 `git add` 并 `git commit`，最后用三五行说明改了什么。\n\n## 检查报告（错误）\n" + digest)
            agent(job, fix, title=f"课件#{tid} 修复SVG{rnd + 1}", step="修复课件")
            commit_all(ctx.workdir, f"agent: 课件 #{tid} SVG 修复（流水线补提交）")
            svgs = sorted(svg_src.glob("*.svg"))

        arts = []
        check_ok = not errors
        pptx = None
        export_out = ""
        if check_ok:
            notes_md = deck_dir / "notes.md"
            with_notes = False
            if notes_md.is_file():
                (proj / "notes").mkdir(exist_ok=True)
                shutil.copyfile(notes_md, proj / "notes" / "total.md")
                sp = run_proc([py, str(skill / "scripts" / "total_md_split.py"), str(proj)], work, ctx, timeout=120)
                with_notes = sp.rc == 0
                if not with_notes:
                    job.warnings.append("讲稿 notes.md 拆分失败，导出时不带备注：" + truncate(sp.output[-300:], 300))
            ex = run_proc([py, str(skill / "scripts" / "svg_to_pptx.py"), str(proj), "--quick-generate",
                           "--with-notes" if with_notes else "--no-notes"], work, ctx, timeout=900)
            export_out = ex.output
            outs = sorted((proj / "exports").glob("*.pptx")) if (proj / "exports").is_dir() else []
            if ex.rc == 0 and outs:
                pptx = outs[-1]
                arts.append(copy_artifact(pptx, ctx.out_dir, "课件.pptx", "pptx"))
                ctx.log("导出", f"已导出 PPTX（{len(svgs)} 页，{'带' if with_notes else '不带'}讲稿备注）")
            else:
                ctx.log("导出", "svg_to_pptx 失败")
        prev = self._preview_pdf(proj / "svg_output", ctx.out_dir / "课件预览.pdf")
        if prev:
            arts.append({"name": "课件预览.pdf", "path": str(prev), "kind": "pdf"})
        commit_all(ctx.workdir, f"agent: 课件 #{tid}（流水线补提交）")

        ok = pptx is not None
        if ok:
            status_line = (f"✅ 课件已导出：{len(svgs)} 页原生可编辑 PPTX（风格 {style}）。"
                           "`课件预览.pdf` 是 SVG 的近似渲染，字体、公式以 PowerPoint 打开的 PPTX 为准。")
        elif not check_ok:
            status_line = f"❌ PPT Master 检查修复 {MAX_SVG_FIX} 次后仍有错误，未能导出 PPTX。SVG 源文件在分支上。"
        else:
            status_line = "❌ 导出 PPTX 失败。"
        extra = []
        if errors:
            extra.append(("PPT Master 检查剩余错误", "\n".join(f"- `{f}`：" + "；".join(truncate(e, 200) for e in v[:3])
                                                         for f, v in errors.items())))
        if export_out and not ok:
            extra.append(("导出输出", "```\n" + export_out[-2000:] + "\n```"))
        warns = sum(len(f.get("warnings") or []) for f in report.get("files", []))
        extra.append(("产物", f"- 页数：{len(svgs)}；PPT Master 警告 {warns} 条（不影响导出）\n- 源文件：`{deck}/`（outline.md、svg/、notes.md）"))
        summary = compose_summary(job, title=f"课件 #{tid}：第 {ch} 章", status_line=status_line, checks=None,
                                  extra_sections=extra)
        return result(job, "awaiting_review" if ok else "failed", summary, arts)

    @staticmethod
    def _stage_svgs(src: Path, proj: Path, bgs: dict) -> None:
        out = proj / "svg_output"
        shutil.rmtree(out, ignore_errors=True)
        out.mkdir(parents=True)
        for f in sorted(src.glob("*.svg")):
            text = f.read_text(encoding="utf-8", errors="replace")
            if bgs and 'data-pptx-role="background"' not in text:
                m = re.search(r'data-pptx-page-role="([a-z]+)"', text)
                bg = bgs.get(m.group(1) if m else "content") or bgs.get("content")
                if bg:
                    tag = (f'<image id="booksmith-bg" data-pptx-role="background" href="../images/{bg}" '
                           'x="0" y="0" width="1280" height="720" preserveAspectRatio="none"/>')
                    text = re.sub(r"(<svg\b[^>]*>)", r"\1\n  " + tag.replace("\\", "\\\\"), text, count=1)
            (out / f.name).write_text(text, encoding="utf-8", newline="\n")

    @staticmethod
    def _preview_pdf(svg_dir: Path, dst: Path) -> Path | None:
        """PyMuPDF 渲染 SVG → 多页 PDF；外部图片先内联成 data URI（MuPDF 不读外部图片）。"""
        try:
            import pymupdf as fitz  # PyMuPDF
        except ImportError:
            return None
        files = sorted(svg_dir.glob("*.svg")) if svg_dir.is_dir() else []
        if not files:
            return None
        out = fitz.open()
        for f in files:
            text = f.read_text(encoding="utf-8", errors="replace")

            def inline(m):
                p = (f.parent / m.group(1)).resolve()
                if p.is_file():
                    mime = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"
                    return f'href="data:{mime};base64,{base64.b64encode(p.read_bytes()).decode()}"'
                return m.group(0)
            text = re.sub(r'href="(?!data:)([^"]+\.(?:png|jpg|jpeg))"', inline, text)
            try:
                d = fitz.open(stream=text.encode("utf-8"), filetype="svg")
                out.insert_pdf(fitz.open("pdf", d.convert_to_pdf()))
            except Exception:
                continue
        if out.page_count == 0:
            return None
        dst.parent.mkdir(parents=True, exist_ok=True)
        out.save(dst, garbage=4, deflate=True)
        return dst

    # ------------------------------------------------------------ Beamer
    def _beamer(self, ctx, job, deck, ch, head) -> Result:
        tid = job.tid
        tex = f"{deck}/slides.tex"
        if not (Path(ctx.workdir) / tex).is_file():
            return result(job, "failed", compose_summary(job, title=f"课件 #{tid}：没有生成 slides.tex",
                          status_line=f"❌ `{tex}` 不存在。", checks=None), [])
        builds = build_and_fix(job, [{"file": tex}], fix_prompt_head=f"{head}\n\n（slides.tex 已写好，只修编译问题。）",
                               title=f"课件#{tid}")
        commit_all(ctx.workdir, f"agent: 课件 #{tid} 编译修复（流水线补提交）")
        b = builds[-1]
        arts = []
        if b.usable:
            arts.append(copy_artifact(b.pdf, ctx.out_dir, "课件.pdf", "pdf"))
            pp = self._pdf_to_pptx(b.pdf, ctx.out_dir / "课件（图片版）.pptx")
            if pp:
                arts.append({"name": "课件（图片版）.pptx", "path": str(pp), "kind": "pptx"})
        ok = b.rc == 0 and b.usable
        status_line = ("✅ Beamer 课件编译通过。PPTX 是逐页图片拼成的，不能在 PowerPoint 里改字；要改请改 slides.tex。"
                       if ok else "❌ Beamer 编译修复 3 次后仍失败。")
        summary = compose_summary(job, title=f"课件 #{tid}：第 {ch} 章（Beamer）", status_line=status_line, checks=None)
        return result(job, "awaiting_review" if ok else "failed", summary, arts)

    @staticmethod
    def _pdf_to_pptx(pdf: Path, dst: Path) -> Path | None:
        try:
            import pymupdf as fitz
            from pptx import Presentation
            from pptx.util import Emu
        except ImportError:
            return None
        doc = fitz.open(pdf)
        prs = Presentation()
        prs.slide_width, prs.slide_height = Emu(12192000), Emu(6858000)
        blank = prs.slide_layouts[6]
        tmp = dst.parent / "_pages"
        tmp.mkdir(parents=True, exist_ok=True)
        for i, pg in enumerate(doc):
            png = tmp / f"p{i:03d}.png"
            pg.get_pixmap(matrix=fitz.Matrix(2, 2)).save(png)
            s = prs.slides.add_slide(blank)
            s.shapes.add_picture(str(png), 0, 0, prs.slide_width, prs.slide_height)
        prs.save(dst)
        shutil.rmtree(tmp, ignore_errors=True)
        return dst
