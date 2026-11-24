"""从老师现有的 .pptx 课件做一个"课件模板包"，供 slides 流水线的 pptmaster-template 风格使用。

做法：从课件里挑三页（封面、目录/节标题、普通内容页），删掉页面上的所有形状，只留版式/母版上的装饰
（校徽、地图、色带），用 LibreOffice 导出成 PDF，再用 PyMuPDF 渲染成 1280×720 的背景 PNG。
生成页面时，流水线把对应背景作为 PPT 的页面背景，文字、图形都是可编辑的原生对象画在上面。

用法（在装了 LibreOffice、python-pptx、PyMuPDF 的电脑上跑，例如本机 Windows）：
    python scripts/make_slide_template.py 第五讲.pptx out/shu --cover 1 --section 2 --content 6 \
        --name shu --title "上海大学课件模板" --primary "#0058C4"
然后把 out/shu 整个目录拷到树莓派 ~/BookSmith/data/slide-templates/shu/（容器里是 /data/slide-templates/shu）。

模板包里有校徽等素材，**不要提交进公开仓库**。生成后请打开 template.md 按实际情况修改颜色、版心说明。
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

DEFAULT_SOFFICE = [r"C:\Program Files\LibreOffice\program\soffice.exe", "soffice", "libreoffice"]


def find_soffice(explicit: str | None) -> str:
    for c in ([explicit] if explicit else []) + DEFAULT_SOFFICE:
        if c and (Path(c).is_file() or shutil.which(c)):
            return c
    sys.exit("找不到 LibreOffice（soffice），用 --soffice 指定路径")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pptx")
    ap.add_argument("out")
    ap.add_argument("--cover", type=int, default=1, help="封面页的页码（从 1 开始）")
    ap.add_argument("--section", type=int, default=2, help="目录/节标题页的页码")
    ap.add_argument("--content", type=int, default=3, help="普通内容页的页码")
    ap.add_argument("--name", default="teacher")
    ap.add_argument("--title", default="老师现有课件模板")
    ap.add_argument("--primary", default="#0058C4", help="主色（标题、强调）")
    ap.add_argument("--font", default="Microsoft YaHei")
    ap.add_argument("--soffice")
    a = ap.parse_args()

    from pptx import Presentation  # noqa: E402
    import fitz  # PyMuPDF

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    prs = Presentation(a.pptx)
    w, h = prs.slide_width, prs.slide_height
    keep = [a.cover - 1, a.section - 1, a.content - 1]
    ids = list(prs.slides._sldIdLst)
    order = {}
    for i, sid in enumerate(ids):
        if i not in keep:
            prs.part.drop_rel(sid.rId)
            prs.slides._sldIdLst.remove(sid)
        else:
            order[i] = len(order)
    for s in prs.slides:
        for sh in list(s.shapes):
            sh._element.getparent().remove(sh._element)
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / "blank.pptx"
        prs.save(tmp)
        subprocess.run([find_soffice(a.soffice), "--headless", "--convert-to", "pdf", "--outdir", td, str(tmp)],
                       check=True, capture_output=True, timeout=300)
        doc = fitz.open(Path(td) / "blank.pdf")
        # 页面顺序与原课件中的顺序一致
        names_by_src = {a.cover - 1: "bg_cover.png", a.section - 1: "bg_section.png", a.content - 1: "bg_content.png"}
        for src_idx, pos in sorted(order.items(), key=lambda x: x[1]):
            pg = doc[pos]
            z = 1920 / pg.rect.width                      # 1.5 倍分辨率，投影更清楚
            pg.get_pixmap(matrix=fitz.Matrix(z, z)).save(out / names_by_src[src_idx])
        doc.close()                                    # Windows 下不关闭就删不掉临时目录

    spec = {
        "name": a.name,
        "title": a.title,
        "canvas": [1280, round(1280 * h / w)],
        "font": a.font,
        "backgrounds": {"cover": "bg_cover.png", "toc": "bg_section.png", "section": "bg_section.png",
                        "content": "bg_content.png", "ending": "bg_cover.png"},
        "colors": {"primary": a.primary, "text": "#1F2937", "muted": "#64748B", "on_primary": "#FFFFFF",
                   "light": "#EAF2FC"},
    }
    (out / "template.json").write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
    md = out / "template.md"
    if not md.exists():
        md.write_text(f"# 模板：{a.title}\n\n（请按背景图的实际样子写：每类页面的背景上已有什么装饰、文字应放在哪个矩形区域、"
                      "标题字号与颜色、正文字号、项目符号写法。）\n", encoding="utf-8")
    print(f"已生成 {out}：", ", ".join(p.name for p in sorted(out.iterdir())))


if __name__ == "__main__":
    main()
