"""自检：改动前后的结构数量对比、引用可解析、改动文件清单、编译错误摘要。

- 数量对比按"单元"（每一章、front、exercises、slides）统计：章节数（\\chapter/\\section/\\subsection）、
  中文字符数、公式数、图数、表数、\\label 数、例题数。改动前 = 分支与 master 的 merge-base（git show 读），
  改动后 = worktree 当前文件。只统计被改动过的单元。
- "异常减少"：结构数量（节、图、表、公式、标签、例题）任一减少，或中文字数减少超过 5% 且超过 300 字。
  任务参数 allow_shrink=true 时只警告不失败（需求本身就是删内容时用）。
- 引用：在改动过的 .tex 里找 \\ref/\\eqref/\\autoref/\\cref/\\pageref 和 \\cite，对照全书（worktree）
  的 \\label 和 \\bibitem；找不到的就是坏引用（单章编译时跨章引用显示 ?? 是正常的，这里不受影响）。
  另读编译日志里的 undefined 警告，同样按"全书是否存在"过滤。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .common import git, merge_base

TEX_DIRS = ("chapters", "ch4", "front", "exercises", "slides")
SKIP_PARTS = {"notes", "review", "build", "_build", "_work", ".opencode"}

CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
COMMENT_RE = re.compile(r"(?<!\\)%.*")
EQ_ENVS = r"equation\*?|align\*?|gather\*?|multline\*?|eqnarray\*?|flalign\*?"
METRICS = {
    "chapters": re.compile(r"\\chapter\*?\s*[\[{]"),
    "sections": re.compile(r"\\(?:sub)?section\*?\s*[\[{]"),
    "equations": re.compile(r"\\begin\{(?:" + EQ_ENVS + r")\}|\\\["),
    "figures": re.compile(r"\\begin\{figure\*?\}"),
    "tables": re.compile(r"\\begin\{(?:table\*?|longtable)\}"),
    "labels": re.compile(r"\\label\{"),
    "examples": re.compile(r"\\begin\{example\}"),
}
LABELS_CN = {"chapters": "章", "sections": "节/小节", "equations": "公式", "figures": "图", "tables": "表",
             "labels": "标签", "examples": "例题", "cjk": "中文字数"}
REF_RE = re.compile(r"\\(?:ref|eqref|autoref|cref|Cref|pageref|nameref)\{([^}]+)\}")
CITE_RE = re.compile(r"\\cite[pt]?\*?(?:\[[^\]]*\])*\{([^}]+)\}")
LABEL_RE = re.compile(r"\\label\{([^}]+)\}")
BIBITEM_RE = re.compile(r"\\bibitem(?:\[[^\]]*\])?\{([^}]+)\}")
EXAMPLE_LABEL_RE = re.compile(r"\\begin\{example\}(?:\[[^\]]*\])?\{([^}]+)\}")


def unit_of(path: str) -> str | None:
    p = Path(path)
    parts = p.parts
    if not parts or p.suffix != ".tex" or SKIP_PARTS & set(parts):
        return None
    if parts[0] == "chapters" and len(parts) >= 2:
        return parts[1]                       # ch5
    if parts[0] == "ch4":
        return "ch4"
    if parts[0] in ("front", "exercises", "slides"):
        return parts[0] if len(parts) < 3 else f"{parts[0]}/{parts[1]}"
    if parts[0] == "tex":
        return None                           # 旧稿，不统计
    if len(parts) == 1:
        return "root"                         # main.tex 等
    return None


def strip_comments(text: str) -> str:
    return "\n".join(COMMENT_RE.sub("", line) for line in text.splitlines())


def stats_of(text: str) -> dict:
    t = strip_comments(text)
    d = {k: len(r.findall(t)) for k, r in METRICS.items()}
    d["cjk"] = len(CJK_RE.findall(t))
    return d


def add_stats(a: dict, b: dict) -> dict:
    keys = set(a) | set(b)
    return {k: a.get(k, 0) + b.get(k, 0) for k in keys}


# ---------------------------------------------------------------- 改动清单
def changed_files(workdir: Path, base: str | None = None) -> list[tuple[str, str]]:
    """[(状态, 路径)]：相对 merge-base 的已提交改动 + 未提交改动 + 未跟踪文件。"""
    base = base or merge_base(workdir)
    seen: dict[str, str] = {}
    p = git(workdir, "diff", "--name-status", "--no-renames", base)
    for line in p.stdout.splitlines():
        if "\t" in line:
            st, path = line.split("\t", 1)
            seen[path] = st[0]
    p = git(workdir, "ls-files", "--others", "--exclude-standard")
    for path in p.stdout.splitlines():
        if path.strip():
            seen.setdefault(path.strip(), "A")
    return sorted(((st, path) for path, st in seen.items()), key=lambda x: x[1])


def files_at(workdir: Path, rev: str) -> list[str]:
    p = git(workdir, "ls-tree", "-r", "--name-only", rev, "--", *TEX_DIRS, "main.tex")
    return [x for x in p.stdout.splitlines() if x.endswith(".tex")]


def read_at(workdir: Path, rev: str, path: str) -> str:
    p = git(workdir, "show", f"{rev}:{path}")
    return p.stdout if p.returncode == 0 else ""


def unit_stats(workdir: Path, units: set[str], rev: str | None) -> dict[str, dict]:
    """rev=None 读 worktree；否则读 git 里的版本。"""
    out: dict[str, dict] = {u: {} for u in units}
    if rev is None:
        for d in TEX_DIRS:
            for f in (workdir / d).rglob("*.tex") if (workdir / d).is_dir() else []:
                rel = f.relative_to(workdir).as_posix()
                u = unit_of(rel)
                if u in units:
                    out[u] = add_stats(out[u], stats_of(f.read_text(encoding="utf-8", errors="replace")))
        for f in workdir.glob("*.tex"):
            if "root" in units:
                out["root"] = add_stats(out["root"], stats_of(f.read_text(encoding="utf-8", errors="replace")))
    else:
        for rel in files_at(workdir, rev):
            u = unit_of(rel)
            if u in units:
                out[u] = add_stats(out[u], stats_of(read_at(workdir, rev, rel)))
    return out


# ---------------------------------------------------------------- 引用
def all_labels_and_keys(workdir: Path) -> tuple[set[str], set[str]]:
    labels, keys = set(), set()
    for d in ("chapters", "ch4", "front", "tex"):
        root = workdir / d
        if not root.is_dir():
            continue
        for f in root.rglob("*.tex"):
            if SKIP_PARTS & set(f.relative_to(workdir).parts):
                continue
            t = strip_comments(f.read_text(encoding="utf-8", errors="replace"))
            labels.update(LABEL_RE.findall(t))
            labels.update(EXAMPLE_LABEL_RE.findall(t))
            keys.update(k.strip() for k in BIBITEM_RE.findall(t))
    return labels, keys


def refs_in(text: str) -> tuple[set[str], set[str]]:
    t = strip_comments(text)
    refs = set()
    for m in REF_RE.findall(t):
        refs.update(x.strip() for x in m.split(",") if x.strip())
    cites = set()
    for m in CITE_RE.findall(t):
        cites.update(x.strip() for x in m.split(",") if x.strip())
    return refs, cites


UNDEF_REF_RE = re.compile(r"LaTeX Warning: Reference [`'‘]([^'’]+)['’] on page \d+ undefined")
UNDEF_CIT_RE = re.compile(r"LaTeX Warning: Citation [`'‘]([^'’]+)['’] on page \d+ undefined")


def undefined_in_log(log_text: str) -> tuple[set[str], set[str]]:
    return set(UNDEF_REF_RE.findall(log_text)), set(UNDEF_CIT_RE.findall(log_text))


# ---------------------------------------------------------------- 汇总
@dataclass
class CheckReport:
    changed: list[tuple[str, str]] = field(default_factory=list)
    before: dict = field(default_factory=dict)
    after: dict = field(default_factory=dict)
    shrink: list[str] = field(default_factory=list)       # 异常减少的描述
    bad_refs: list[str] = field(default_factory=list)
    bad_cites: list[str] = field(default_factory=list)
    forbidden: list[str] = field(default_factory=list)    # 改了不该改的文件
    log_undefined: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.shrink or self.bad_refs or self.bad_cites or self.forbidden)

    def problems(self) -> list[str]:
        out = []
        out += [f"内容异常减少：{s}" for s in self.shrink]
        if self.bad_refs:
            out.append("找不到标签的引用：" + "、".join(sorted(self.bad_refs)[:20]))
        if self.bad_cites:
            out.append("找不到文献条目的 \\cite：" + "、".join(sorted(self.bad_cites)[:20]))
        out += [f"改动了不允许改的文件：{f}" for f in self.forbidden]
        return out

    def markdown(self) -> str:
        lines = ["| 检查 | 结果 |", "|---|---|"]
        lines.append(f"| 改动文件 | {len(self.changed)} 个 |")
        lines.append("| 内容数量 | " + ("无异常减少" if not self.shrink else "⚠ " + "；".join(self.shrink)) + " |")
        ref_problems = [p for p in self.problems() if p.startswith(("找不到标签", "找不到文献"))]
        lines.append("| 引用可解析 | " + ("改动文件中的 \\ref / \\cite 均能在全书找到" if not ref_problems
                                        else "⚠ " + "；".join(ref_problems)) + " |")
        if self.log_undefined:
            lines.append("| 编译日志 | 有全书中也找不到的未定义引用：" + "、".join(self.log_undefined[:10]) + " |")
        if self.forbidden:
            lines.append("| 禁改文件 | ⚠ " + "、".join(self.forbidden) + " |")
        out = "\n".join(lines)
        if self.before or self.after:
            out += "\n\n**数量对比（改动前 → 改动后）**\n\n| 单元 | " + " | ".join(LABELS_CN[k] for k in LABELS_CN) + " |\n"
            out += "|---|" + "---|" * len(LABELS_CN) + "\n"
            for u in sorted(set(self.before) | set(self.after)):
                b, a = self.before.get(u, {}), self.after.get(u, {})
                cells = []
                for k in LABELS_CN:
                    x, y = b.get(k, 0), a.get(k, 0)
                    cells.append(f"{x}" if x == y else f"{x} → **{y}**")
                out += f"| {u} | " + " | ".join(cells) + " |\n"
        if self.changed:
            out += "\n**改动文件**\n\n" + "\n".join(f"- `{st}` {p}" for st, p in self.changed[:60])
            if len(self.changed) > 60:
                out += f"\n- ……共 {len(self.changed)} 个"
        if self.notes:
            out += "\n\n" + "\n".join(f"- {n}" for n in self.notes)
        return out


FORBIDDEN = ("bookstyle.sty", "bookbook.sty", "pandoc-shim.tex", "fonts.tex", "tools/")


def run_checks(workdir: Path, *, allow_shrink: bool = False, build_logs: list[str] | None = None,
               allowed_forbidden: tuple[str, ...] = (), base: str | None = None) -> CheckReport:
    base = base or merge_base(workdir)
    rep = CheckReport()
    rep.changed = changed_files(workdir, base)
    units = {u for _, p in rep.changed if (u := unit_of(p))}
    if units:
        rep.before = unit_stats(workdir, units, base)
        rep.after = unit_stats(workdir, units, None)
        for u in sorted(units):
            b, a = rep.before.get(u, {}), rep.after.get(u, {})
            if not b:
                continue  # 新增单元
            for k in METRICS:
                if a.get(k, 0) < b.get(k, 0):
                    rep.shrink.append(f"{u} 的{LABELS_CN[k]} {b[k]} → {a.get(k, 0)}")
            bc, ac = b.get("cjk", 0), a.get("cjk", 0)
            if bc and bc - ac > max(300, bc * 0.05):
                rep.shrink.append(f"{u} 的中文字数 {bc} → {ac}（减少 {bc - ac}）")
    if allow_shrink and rep.shrink:
        rep.notes.append("任务允许删减内容（allow_shrink），以下减少只作提示：" + "；".join(rep.shrink))
        rep.shrink = []

    # 不该动的文件
    for st, p in rep.changed:
        if any(p == f or (f.endswith("/") and p.startswith(f)) for f in FORBIDDEN) and \
                not any(p == f or p.startswith(f) for f in allowed_forbidden):
            rep.forbidden.append(p)
        if Path(p).name == "ACCEPTED":
            rep.forbidden.append(p)
        if p.startswith(("tex/",)) or "/notes/" in p and not p.endswith((".md",)) or "/review/" in p:
            rep.forbidden.append(p)
    rep.forbidden = sorted(set(rep.forbidden))

    # 引用
    labels, keys = all_labels_and_keys(workdir)
    for st, p in rep.changed:
        f = workdir / p
        if st == "D" or not p.endswith(".tex") or not f.is_file():
            continue
        if p.startswith("exercises/") or p.startswith("slides/"):
            continue  # 独立文档不能引用书稿标签（技能已规定），不在这里查
        refs, cites = refs_in(f.read_text(encoding="utf-8", errors="replace"))
        rep.bad_refs += [r for r in refs if r not in labels and "#" not in r]
        rep.bad_cites += [c for c in cites if c not in keys]
    rep.bad_refs = sorted(set(rep.bad_refs))
    rep.bad_cites = sorted(set(rep.bad_cites))
    for log in build_logs or []:
        r, c = undefined_in_log(log)
        rep.log_undefined += [x for x in r if x not in labels] + [x for x in c if x not in keys]
    rep.log_undefined = sorted(set(rep.log_undefined))
    return rep


def compile_error_digest(build_output: str, limit: int = 2500) -> str:
    """给模型看的编译错误摘要：错误摘要段 + 最后几行。"""
    from .common import extract_summary
    s = extract_summary(build_output)
    return s[-limit:]
