"""服务端渲染辅助：安全 Markdown、unified diff 解析。"""
from __future__ import annotations

from dataclasses import dataclass, field

from markdown_it import MarkdownIt
from markupsafe import Markup

# html=False：原始 HTML 一律转义；markdown-it 自带 validateLink，拦截 javascript:/vbscript:/data: 链接
_md = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False})
_md.enable(["table", "strikethrough"])


def render_markdown(text: str | None) -> Markup:
    return Markup(_md.render(text or ""))


@dataclass
class DiffLine:
    kind: str      # add | del | ctx | hunk | meta
    text: str


@dataclass
class DiffFile:
    old: str
    new: str
    lines: list[DiffLine] = field(default_factory=list)
    added: int = 0
    removed: int = 0
    binary: bool = False
    truncated: bool = False
    hidden_lines: int = 0

    @property
    def name(self) -> str:
        if self.new == "/dev/null":
            return self.old
        if self.old not in (self.new, "/dev/null"):
            return f"{self.old} → {self.new}"
        return self.new

    @property
    def status(self) -> str:
        if self.old == "/dev/null":
            return "新增"
        if self.new == "/dev/null":
            return "删除"
        if self.old != self.new:
            return "重命名"
        return "修改"


@dataclass
class ParsedDiff:
    files: list[DiffFile]
    truncated: bool
    total_added: int
    total_removed: int


def _strip_prefix(p: str) -> str:
    p = p.strip()
    if p.startswith('"') and p.endswith('"'):
        p = p[1:-1]
    if p.startswith(("a/", "b/")):
        return p[2:]
    return p


def parse_diff(text: str, max_lines_per_file: int = 800, max_total_lines: int = 4000) -> ParsedDiff:
    files: list[DiffFile] = []
    cur: DiffFile | None = None
    shown_total = 0
    any_trunc = False
    for raw in (text or "").splitlines():
        if raw.startswith("diff --git "):
            parts = raw[len("diff --git "):].split(" b/", 1)
            old = _strip_prefix(parts[0])
            new = _strip_prefix("b/" + parts[1]) if len(parts) > 1 else old
            cur = DiffFile(old=old, new=new)
            files.append(cur)
            continue
        if cur is None:
            continue
        if raw.startswith("--- "):
            p = raw[4:].strip()
            cur.old = "/dev/null" if p == "/dev/null" else _strip_prefix(p)
            continue
        if raw.startswith("+++ "):
            p = raw[4:].strip()
            cur.new = "/dev/null" if p == "/dev/null" else _strip_prefix(p)
            continue
        if raw.startswith("new file mode"):
            cur.old = "/dev/null"
            continue
        if raw.startswith("deleted file mode"):
            cur.new = "/dev/null"
            continue
        if raw.startswith("rename from "):
            cur.old = raw[len("rename from "):]
            continue
        if raw.startswith("rename to "):
            cur.new = raw[len("rename to "):]
            continue
        if raw.startswith("Binary files") or raw.startswith("GIT binary patch"):
            cur.binary = True
            continue
        if raw.startswith(("index ", "similarity index", "old mode", "new mode", "dissimilarity")):
            continue
        if raw.startswith("@@"):
            kind = "hunk"
        elif raw.startswith("+"):
            kind = "add"
            cur.added += 1
        elif raw.startswith("-"):
            kind = "del"
            cur.removed += 1
        elif raw.startswith("\\"):
            kind = "meta"
        else:
            kind = "ctx"
        if len(cur.lines) >= max_lines_per_file or shown_total >= max_total_lines:
            cur.truncated = True
            cur.hidden_lines += 1
            any_trunc = True
            continue
        cur.lines.append(DiffLine(kind, raw))
        shown_total += 1
    return ParsedDiff(files=files, truncated=any_trunc,
                      total_added=sum(f.added for f in files),
                      total_removed=sum(f.removed for f in files))
