"""真实流水线共用的小工具：路径与环境、秘密打码、编译、git、DeepSeek 余额、说明拼装。

环境变量（都有默认值，容器里一般不用改）：

| 变量 | 默认 | 含义 |
|---|---|---|
| BOOKSMITH_HOME | 本仓库根目录（容器里 /app） | 找 opencode/、skills/ 的基准 |
| BOOKSMITH_SKILLS_DIR | /skills 存在则用它，否则 $BOOKSMITH_HOME/skills | 技能来源（compose 只读挂载） |
| BOOKSMITH_OPENCODE_CONFIG | $BOOKSMITH_HOME/opencode/opencode.json | OpenCode 配置 |
| BOOKSMITH_OPENCODE_BIN | opencode | OpenCode 可执行文件 |
| BOOKSMITH_OPENCODE_HOME | $DATA_DIR/opencode | OpenCode 的 HOME/XDG 目录（会话库、日志、缓存） |
| BOOKSMITH_MODEL | deepseek/deepseek-flash | provider/model |
| BOOKSMITH_BUILD_PREFIX | 空 | 编译命令前缀。容器里为空（直接 sh tools/build_linux.sh）；
|                        |    | 在宿主机调试时可设成 `docker run --rm -v {workdir}:/book -w /book --user 1000:1000 -e HOME=/tmp booksmith-base` |
| BOOKSMITH_PPTMASTER_DIR | /opt/ppt-master | PPT Master 仓库位置 |
| BOOKSMITH_AGENT_TIMEOUT | 1800 | 单次 OpenCode 运行的超时（秒） |
| BOOKSMITH_BUILD_TIMEOUT | 1800 | 单次编译的超时（秒） |
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from ..base import Ctx, PipelineCancelled

REPO_ROOT = Path(__file__).resolve().parents[3]          # BookSmith/

# ---------------------------------------------------------------- 路径
def env_path(name: str, default: Path) -> Path:
    v = os.environ.get(name, "").strip()
    return Path(v) if v else default


def booksmith_home() -> Path:
    return env_path("BOOKSMITH_HOME", REPO_ROOT)


def skills_dir() -> Path:
    v = os.environ.get("BOOKSMITH_SKILLS_DIR", "").strip()
    if v:
        return Path(v)
    if Path("/skills").is_dir():
        return Path("/skills")
    return booksmith_home() / "skills"


def data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR", "/data"))


def pptmaster_dir() -> Path:
    return env_path("BOOKSMITH_PPTMASTER_DIR", Path("/opt/ppt-master"))


def int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


# ---------------------------------------------------------------- 秘密打码
_SECRET_RE = re.compile(r"(sk-|tvly-|ghp_|github_pat_)[A-Za-z0-9_\-]{6,}")


def mask(text: str) -> str:
    """把 sk-xxxx、tvly-xxxx 等疑似密钥打码；再把当前进程里的 DEEPSEEK_API_KEY / TAVILY_API_KEY 原文替换掉。"""
    if not text:
        return text
    text = _SECRET_RE.sub(lambda m: m.group(1) + "***", text)
    for name in ("DEEPSEEK_API_KEY", "TAVILY_API_KEY"):
        key = os.environ.get(name, "")
        if len(key) >= 8:
            text = text.replace(key, "***")
    return text


# ---------------------------------------------------------------- 取消检查
def check_cancel(ctx: Ctx) -> None:
    if ctx.cancelled():
        raise PipelineCancelled()


# ---------------------------------------------------------------- 任务参数
def task_params(ctx: Ctx) -> dict:
    p = ctx.task.get("params")
    if isinstance(p, dict):
        return p
    try:
        return json.loads(ctx.task.get("params_json") or "{}")
    except (TypeError, ValueError):
        return {}


def task_chapter(ctx: Ctx) -> int | None:
    v = task_params(ctx).get("chapter")
    if v in (None, ""):
        # 退而求其次：从需求里找“第 N 章”
        m = re.search(r"第\s*([1-9]\d?)\s*章", (ctx.task.get("request") or "") + (ctx.task.get("title") or ""))
        return int(m.group(1)) if m else None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- 子进程（可取消、可超时）
@dataclass
class ProcResult:
    rc: int
    output: str
    seconds: float
    timed_out: bool = False


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        pass


def run_proc(cmd: list[str] | str, cwd: Path, ctx: Ctx | None = None, timeout: int = 1800,
             env: dict | None = None, shell: bool = False) -> ProcResult:
    """运行命令，合并 stdout/stderr；每秒检查一次取消；超时杀整个进程组。"""
    t0 = time.time()
    kw = {"start_new_session": True} if os.name == "posix" else {}
    proc = subprocess.Popen(cmd, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, env=env, shell=shell, **kw)
    chunks: list[bytes] = []

    import threading

    def reader():
        assert proc.stdout
        for line in proc.stdout:
            chunks.append(line)

    th = threading.Thread(target=reader, daemon=True)
    th.start()
    timed_out = False
    while proc.poll() is None:
        if ctx is not None and ctx.cancelled():
            _kill_tree(proc)
            th.join(2)
            raise PipelineCancelled()
        if time.time() - t0 > timeout:
            timed_out = True
            _kill_tree(proc)
            break
        time.sleep(0.5)
    th.join(5)
    out = b"".join(chunks).decode("utf-8", errors="replace")
    return ProcResult(proc.returncode if proc.returncode is not None else -9, mask(out),
                      time.time() - t0, timed_out)


# ---------------------------------------------------------------- 编译
@dataclass
class BuildResult:
    ok: bool              # 返回码 0
    rc: int
    output: str           # 完整输出（已打码）
    summary: str          # 错误摘要段落
    pdf: Path | None
    seconds: float
    log_path: Path | None = None
    target: str = ""

    @property
    def usable(self) -> bool:
        """有 PDF（rc 0 或 2：生成了 PDF 但有缺字/未定义引用）。"""
        return self.pdf is not None and self.pdf.is_file()


def build_cmd(workdir: Path, args: list[str]) -> list[str]:
    prefix = os.environ.get("BOOKSMITH_BUILD_PREFIX", "").strip()
    base = ["sh", "tools/build_linux.sh", *args]
    if not prefix:
        return base
    return shlex.split(prefix.replace("{workdir}", str(workdir))) + base


def extract_summary(output: str) -> str:
    i = output.find("错误摘要")
    if i >= 0:
        j = output.rfind("\n", 0, i)
        return output[j + 1:].strip()[-4000:]
    return output.strip()[-3000:]


def build(ctx: Ctx, workdir: Path, *, chapter: int | None = None, file: str | None = None,
          timeout: int | None = None) -> BuildResult:
    """chapter=None 且 file=None → 全书；chapter=N → 单章；file=路径 → 独立文档（习题等）。"""
    timeout = timeout or int_env("BOOKSMITH_BUILD_TIMEOUT", 1800)
    if file:
        args, target = ["--file", file], file
        stem = Path(file)
        pdf = workdir / stem.with_suffix(".pdf")
        log = workdir / stem.with_suffix(".log")
    elif chapter is not None:
        args, target = ["--chapter", str(chapter)], f"第 {chapter} 章"
        pdf = workdir / "_build" / f"ch{chapter}" / f"ch{chapter}.pdf"
        log = workdir / "_build" / f"ch{chapter}" / f"ch{chapter}.log"
    else:
        args, target = [], "全书"
        pdf = workdir / "main.pdf"
        log = workdir / "main.log"
    if pdf.exists():
        try:
            pdf.unlink()
        except OSError:
            pass
    r = run_proc(build_cmd(workdir, args), workdir, ctx, timeout=timeout)
    out = r.output
    if r.timed_out:
        out += f"\n编译超时（>{timeout}s），已终止。"
    # 脚本最后一行通常写出 PDF 路径；--file 的输出位置以脚本为准
    m = re.search(r"PDF：(\S+\.pdf)", out)
    if m and not pdf.is_file():
        cand = Path(m.group(1))
        parts = cand.parts
        # 容器里打印的是 /book/...，宿主机上要映射回 workdir：依次去掉前缀试
        for k in range(len(parts)):
            c = cand if k == 0 and cand.is_absolute() else workdir / Path(*parts[k:])
            if c.is_file():
                pdf, log = c, c.with_suffix(".log")
                break
    try:
        logs = Path(ctx.out_dir) / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        n = len(list(logs.glob("build-*.txt"))) + 1
        tag = re.sub(r"[^0-9A-Za-z]+", "_", file or (f"ch{chapter}" if chapter is not None else "full")).strip("_")
        (logs / f"build-{n:02d}-{tag}.txt").write_text(out, encoding="utf-8")
    except OSError:
        pass
    return BuildResult(ok=(r.rc == 0 and not r.timed_out), rc=r.rc, output=out,
                       summary=extract_summary(out), pdf=pdf if pdf.is_file() else None,
                       seconds=r.seconds, log_path=log if log.is_file() else None, target=target)


# ---------------------------------------------------------------- git
def git(workdir: Path, *args: str, check: bool = False) -> subprocess.CompletedProcess:
    from ...bookrepo import git as _git
    return _git(list(args), workdir, check=check)


def base_branch() -> str:
    return os.environ.get("BOOK_BASE_BRANCH", "master").strip() or "master"


def merge_base(workdir: Path) -> str:
    p = git(workdir, "merge-base", "HEAD", base_branch())
    return p.stdout.strip() if p.returncode == 0 else base_branch()


def head(workdir: Path) -> str:
    return git(workdir, "rev-parse", "HEAD").stdout.strip()


def commit_all(workdir: Path, message: str) -> bool:
    from ...bookrepo import BookRepo
    return BookRepo.commit_all(workdir, message)


def ensure_excludes(workdir: Path, patterns: list[str]) -> None:
    """把模式写进仓库的 info/exclude（worktree 共享主仓库的这个文件），不进提交、不改 .gitignore。"""
    p = git(workdir, "rev-parse", "--git-path", "info/exclude")
    if p.returncode != 0:
        return
    path = Path(p.stdout.strip())
    if not path.is_absolute():
        path = workdir / path
    path.parent.mkdir(parents=True, exist_ok=True)
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    have = set(old.splitlines())
    add = [x for x in patterns if x not in have]
    if add:
        with path.open("a", encoding="utf-8", newline="\n") as f:
            if old and not old.endswith("\n"):
                f.write("\n")
            f.write("# BookSmith（流水线自动添加，勿提交）\n" if "# BookSmith" not in old else "")
            f.write("\n".join(add) + "\n")


# 编译产物、技能副本等，绝不进书稿提交
DEFAULT_EXCLUDES = [
    ".opencode/", "_build/", "*.aux", "*.log", "*.toc", "*.out", "*.synctex.gz", "*.idx", "*.ind",
    "*.ilg", "*.fls", "*.fdb_latexmk", "*.xdv", "main.pdf", "exercises/**/*.pdf",
    "slides/**/*.pptx", "slides/**/*.pdf", "slides/**/_work/", "chapters/*/build/",
]


# ---------------------------------------------------------------- DeepSeek 余额（估算花费）
def deepseek_balance() -> float | None:
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        return None
    try:
        req = urllib.request.Request("https://api.deepseek.com/user/balance",
                                     headers={"Authorization": f"Bearer {key}", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8"))
        for b in data.get("balance_infos", []):
            if b.get("currency") == "CNY":
                return float(b.get("total_balance"))
    except Exception:
        return None
    return None


# ---------------------------------------------------------------- 产物
def copy_artifact(src: Path, out_dir: Path, name: str, kind: str) -> dict | None:
    if not src or not Path(src).is_file():
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    dst = out_dir / name
    if Path(src).resolve() != dst.resolve():
        shutil.copyfile(src, dst)
    return {"name": name, "path": str(dst), "kind": kind}


def write_artifact(out_dir: Path, name: str, text: str, kind: str = "md") -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / name
    p.write_text(mask(text), encoding="utf-8")
    return {"name": name, "path": str(p), "kind": kind}


def fmt_seconds(s: float) -> str:
    s = int(s)
    return f"{s // 60} 分 {s % 60} 秒" if s >= 60 else f"{s} 秒"


def truncate(s: str, n: int) -> str:
    s = s or ""
    return s if len(s) <= n else s[: n - 1] + "…"


@dataclass
class Usage:
    """累计一个任务里所有 OpenCode 运行的词元与花费。"""
    runs: int = 0
    input: int = 0
    output: int = 0
    reasoning: int = 0
    cache_read: int = 0
    cost_usd: float = 0.0
    balance_before: float | None = None
    balance_after: float | None = None
    seconds: float = 0.0
    web: dict | None = None            # 联网检索统计（OpenCodeRunner.websearch_stats()），None = 本任务不允许检索

    def add(self, r) -> None:
        self.runs += 1
        t = r.tokens
        self.input += t.get("input", 0)
        self.output += t.get("output", 0)
        self.reasoning += t.get("reasoning", 0)
        self.cache_read += t.get("cache_read", 0)
        self.cost_usd += r.cost
        self.seconds += r.seconds

    def spent_cny(self) -> float | None:
        if self.balance_before is None or self.balance_after is None:
            return None
        return round(self.balance_before - self.balance_after, 2)

    def markdown(self) -> str:
        spent = self.spent_cny()
        lines = [
            f"- 模型调用：{self.runs} 次会话，用时 {fmt_seconds(self.seconds)}",
            f"- 词元：输入 {self.input:,}（缓存命中 {self.cache_read:,}）、输出 {self.output:,}（其中思考 {self.reasoning:,}）",
        ]
        if spent is not None:
            lines.append(f"- DeepSeek 余额变化：{spent:.2f} 元（{self.balance_before:.2f} → {self.balance_after:.2f}；"
                         "余额按 0.01 元取整，并行的其他调用也会算进来）")
        elif self.cost_usd:
            lines.append(f"- OpenCode 估算花费：约 {self.cost_usd:.4f} 美元")
        w = self.web
        if w is not None:
            n = w.get("search", 0) + w.get("extract", 0)
            lines.append(f"- 联网检索（Tavily）：{n} 次（检索 {w.get('search', 0)}、读网页 {w.get('extract', 0)}"
                         + (f"，其中失败 {w['failed']}" if w.get("failed") else "") + f"；本任务上限 {w.get('limit', 0)}）")
            for x in (w.get("log") or [])[:40]:
                tag = "检索" if x.get("kind") == "search" else "读网页"
                lines.append(f"  - {tag}：{truncate(str(x.get('target', '')), 160)}" + ("" if x.get("ok") else "（失败）"))
        return "\n".join(lines)

    def as_dict(self) -> dict:
        return {"runs": self.runs, "input": self.input, "output": self.output, "reasoning": self.reasoning,
                "cache_read": self.cache_read, "cost_usd": round(self.cost_usd, 5),
                "spent_cny": self.spent_cny(), "seconds": round(self.seconds),
                **({"web_search": self.web.get("search", 0), "web_extract": self.web.get("extract", 0),
                    "web_failed": self.web.get("failed", 0)} if self.web is not None else {})}
