"""调用 OpenCode（v1.19.x）执行一个"任务步骤"，把 JSON 事件流转成 ctx.log 进度。

用法：
    runner = OpenCodeRunner(ctx, usage)
    runner.prepare(["booksmith-book", "booksmith-maintain"])   # 技能复制进 worktree/.opencode/skills
    r = runner.run(prompt, title="维护#12 改稿")
    if r.ok: ...

设计要点（见 docs/04 与调研结论）：
- 每个步骤一个新会话（不用 -c/-s 续接）：DeepSeek 思考模式下带工具的长会话偶发
  "reasoning_content 必须回传" 的 400 错误，短会话 + 失败换新会话重试最稳。
- `opencode run --format json --dir <worktree> -m deepseek/deepseek-flash --title ...`，
  stdin 接 /dev/null（否则 opencode 会把 stdin 内容拼进消息）。
- HOME/XDG 目录隔离在 BOOKSMITH_OPENCODE_HOME 下，配置用 OPENCODE_CONFIG 指定，
  关掉自动更新、LSP 下载、读取 ~/.claude 的兼容逻辑；只把必要的环境变量传给子进程。
- 事件（v1.19.6 实测）：每行一个 JSON，type ∈ step_start / tool_use / text / reasoning /
  step_finish / error。step_finish.part 带 tokens{input,output,reasoning,cache{read,write}} 和 cost。
  tool_use 只在工具结束（completed / error）时发出。
- 取消：每 0.5 秒查一次 ctx.cancelled()，杀整个进程组；总超时与"无事件"超时同样杀进程。
- 原始事件流（已打码）存到 out_dir/logs/opencode-NN.jsonl，便于事后排查。
- 联网检索（web_search=True，只给 maintain / newchapter）：TAVILY_API_KEY 同样写成隔离 HOME 下的 0600 文件，
  不进子进程环境；模型只能通过白名单里的 `python3 /app/scripts/websearch.py search|extract ...` 调用。
  每个任务开始时重置 ~/.booksmith/websearch.json（次数上限 + 调用记录），收尾时读出来记进说明和 cost。
  不允许检索的任务：删掉 tavily.key，并用 OPENCODE_PERMISSION 把这两条放行规则改成 deny
  （OpenCode 合并配置时同名键保留原位置，所以后面的 "*>*" 等拒绝规则仍然生效）。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..base import Ctx, PipelineCancelled
from .common import (DEFAULT_EXCLUDES, Usage, _kill_tree, booksmith_home, data_dir, ensure_excludes,
                     env_path, int_env, mask, skills_dir, truncate)

WEBSEARCH_SCRIPT = "/app/scripts/websearch.py"
WEBSEARCH_RULES = (f"python3 {WEBSEARCH_SCRIPT} search *", f"python3 {WEBSEARCH_SCRIPT} extract *")

# 注意：DEEPSEEK_API_KEY 不放进子进程环境（模型的 bash 工具会继承环境，可从 /proc/self/environ 读到）；
# 改为写进隔离 HOME 下的 0600 文件，配置里用 {file:~/.booksmith/deepseek.key} 引用。
PASS_ENV = ("PATH", "LANG", "LC_ALL", "TZ", "TERM",
            "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
            "BOOKSMITH_BUILD_PREFIX", "NPM_CONFIG_REGISTRY", "TEXINPUTS")


@dataclass
class RunResult:
    ok: bool
    text: str = ""                       # 模型最后一条消息的文字（通常是给审核人的说明）
    all_text: list[str] = field(default_factory=list)
    session_id: str | None = None
    tokens: dict = field(default_factory=dict)
    cost: float = 0.0
    steps: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    error: str | None = None
    returncode: int | None = None
    seconds: float = 0.0
    attempts: int = 1
    events_path: Path | None = None
    edited_files: list[str] = field(default_factory=list)


class OpenCodeRunner:
    def __init__(self, ctx: Ctx, usage: Usage | None = None, *, model: str | None = None,
                 timeout: int | None = None, idle_timeout: int | None = None,
                 permission: dict | None = None, web_search: bool = False, web_limit: int | None = None):
        self.ctx = ctx
        self.usage = usage or Usage()
        self.workdir = Path(ctx.workdir)
        self.model = model or os.environ.get("BOOKSMITH_MODEL") or "deepseek/deepseek-flash"
        self.bin = os.environ.get("BOOKSMITH_OPENCODE_BIN") or "opencode"
        self.config = env_path("BOOKSMITH_OPENCODE_CONFIG", booksmith_home() / "opencode" / "opencode.json")
        self.home = env_path("BOOKSMITH_OPENCODE_HOME", data_dir() / "opencode")
        self.timeout = timeout or int_env("BOOKSMITH_AGENT_TIMEOUT", 1800)
        self.idle_timeout = idle_timeout or int_env("BOOKSMITH_AGENT_IDLE_TIMEOUT", 600)
        self.web_search = web_search
        self.web_limit = web_limit or int_env("BOOKSMITH_WEBSEARCH_LIMIT", 25)
        perm = dict(permission or {})          # 追加的权限规则（OPENCODE_PERMISSION，覆盖配置里的同名项）
        if not web_search:
            bash = dict(perm.get("bash") or {})
            for rule in WEBSEARCH_RULES:
                bash[rule] = "deny"
            perm["bash"] = bash
        self.permission = perm or None
        self.log_dir = Path(ctx.out_dir) / "logs"
        self._n = 0

    # ------------------------------------------------------------ 准备 worktree
    def prepare(self, skills: list[str]) -> None:
        """把需要的技能复制到 worktree/.opencode/skills/，并把 .opencode/ 和编译产物加进 info/exclude。"""
        src_root = skills_dir()
        dst_root = self.workdir / ".opencode" / "skills"
        if dst_root.exists():
            shutil.rmtree(dst_root, ignore_errors=True)
        dst_root.mkdir(parents=True, exist_ok=True)
        missing = []
        for name in skills:
            src = src_root / name
            if not (src / "SKILL.md").is_file():
                missing.append(name)
                continue
            shutil.copytree(src, dst_root / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        ensure_excludes(self.workdir, DEFAULT_EXCLUDES)
        self.reset_websearch()
        if missing:
            self.ctx.log("准备", f"找不到技能（已跳过）：{', '.join(missing)}（技能目录 {src_root}）")
        else:
            self.ctx.log("准备", f"已加载技能：{', '.join(skills)}")

    # ------------------------------------------------------------ 环境
    def _env(self) -> dict:
        env = {k: os.environ[k] for k in PASS_ENV if k in os.environ}
        env["PATH"] = env.get("PATH") or "/usr/local/bin:/usr/bin:/bin"
        env["LANG"] = env.get("LANG") or "C.UTF-8"
        h = self.home
        for sub in ("", ".config", ".local/share", ".cache", ".local/state"):
            (h / sub).mkdir(parents=True, exist_ok=True)
        env.update({
            "HOME": str(h),
            "XDG_CONFIG_HOME": str(h / ".config"),
            "XDG_DATA_HOME": str(h / ".local/share"),
            "XDG_CACHE_HOME": str(h / ".cache"),
            "XDG_STATE_HOME": str(h / ".local/state"),
            "OPENCODE_CONFIG": str(self.config),
            "OPENCODE_DISABLE_AUTOUPDATE": "1",
            "OPENCODE_DISABLE_LSP_DOWNLOAD": "1",
            "OPENCODE_DISABLE_CLAUDE_CODE": "1",
            "OPENCODE_DISABLE_DEFAULT_PLUGINS": "1",
            # 模型在 bash 里 git commit 需要身份（隔离的 HOME 里没有 ~/.gitconfig）
            "GIT_AUTHOR_NAME": "BookSmith", "GIT_AUTHOR_EMAIL": "booksmith@localhost",
            "GIT_COMMITTER_NAME": "BookSmith", "GIT_COMMITTER_EMAIL": "booksmith@localhost",
            "GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat", "PAGER": "cat",
        })
        if self.permission:
            env["OPENCODE_PERMISSION"] = json.dumps(self.permission)
        self._write_key(h)
        return env

    @staticmethod
    def _secret_dir(home: Path) -> Path:
        d = home / ".booksmith"
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
        return d

    @staticmethod
    def _write_secret(f: Path, key: str) -> None:
        if f.is_symlink():
            f.unlink()
        if not f.is_file() or f.read_text(encoding="utf-8").strip() != key:
            fd = os.open(str(f), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(key)
        try:
            os.chmod(f, 0o600)
        except OSError:
            pass

    def _write_key(self, home: Path) -> None:
        d = self._secret_dir(home)
        key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        if key:
            self._write_secret(d / "deepseek.key", key)
        tf = d / "tavily.key"
        tkey = os.environ.get("TAVILY_API_KEY", "").strip()
        if self.web_search and tkey:
            self._write_secret(tf, tkey)
        elif tf.exists() or tf.is_symlink():
            tf.unlink()            # 不允许检索的任务：连 key 文件都不留

    # ------------------------------------------------------------ 联网检索的次数与记录
    def _websearch_state(self) -> Path:
        return self._secret_dir(self.home) / "websearch.json"

    def reset_websearch(self) -> None:
        """每个任务开始时调用一次（prepare 里）：清零次数、写入上限。"""
        p = self._websearch_state()
        shutil.rmtree(p.parent / "extract-cache", ignore_errors=True)    # 页面缓存只在本任务内有效
        p.write_text(json.dumps({"limit": self.web_limit if self.web_search else 0, "used": 0, "log": []}),
                     encoding="utf-8")

    def websearch_stats(self) -> dict:
        try:
            st = json.loads(self._websearch_state().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            st = {}
        log = st.get("log") or []
        return {"enabled": self.web_search, "limit": st.get("limit", 0),
                "search": sum(1 for x in log if x.get("kind") == "search"),
                "extract": sum(1 for x in log if x.get("kind") == "extract"),
                "failed": sum(1 for x in log if not x.get("ok")),
                "log": [{"kind": x.get("kind"), "target": mask(str(x.get("target", ""))), "ok": bool(x.get("ok"))}
                        for x in log]}

    # ------------------------------------------------------------ 事件 → 进度
    def _describe_tool(self, part: dict, step: str) -> None:
        tool = part.get("tool", "?")
        st = part.get("state", {}) or {}
        inp = st.get("input", {}) or {}
        status = st.get("status")
        rel = lambda p: os.path.relpath(p, self.workdir) if p and os.path.isabs(p) else (p or "")
        if tool in ("read",):
            msg = f"读 {rel(inp.get('filePath'))}"
        elif tool in ("edit", "write", "patch", "multiedit"):
            f = rel(inp.get("filePath"))
            msg = f"{'写' if tool == 'write' else '改'} {f}"
            if status == "completed" and f:
                self._edited.add(f)
        elif tool == "bash" and (inp.get("command") or "").startswith(f"python3 {WEBSEARCH_SCRIPT} "):
            cmd = (inp.get("command") or "").split(WEBSEARCH_SCRIPT, 1)[1].strip()
            what = "读网页" if cmd.startswith("extract") else "联网检索"
            msg = f"{what} {truncate(cmd.split(' ', 1)[1] if ' ' in cmd else cmd, 120)}"
            ex = (st.get("metadata") or {}).get("exit")
            if ex not in (None, 0):
                msg += f"（失败，返回 {ex}）"
        elif tool == "bash":
            msg = f"运行 {truncate(inp.get('command', ''), 120)}"
            ex = (st.get("metadata") or {}).get("exit")
            if ex not in (None, 0):
                msg += f"（返回 {ex}）"
        elif tool in ("grep", "glob"):
            msg = f"搜索 {truncate(inp.get('pattern', ''), 80)}"
        elif tool == "list":
            msg = f"列目录 {rel(inp.get('path'))}"
        elif tool == "skill":
            msg = f"读技能 {inp.get('name', '')}"
        elif tool in ("todowrite", "todoread"):
            todos = inp.get("todos") or []
            msg = "更新待办：" + "；".join(truncate(t.get("content", ""), 30) for t in todos[:5]) if todos else "更新待办"
        else:
            msg = f"{tool} {truncate(json.dumps(inp, ensure_ascii=False), 100)}"
        if status == "error":
            err = truncate(str(st.get("error", "")), 160)
            msg += f" —— 未执行：{err}"
        self.ctx.log(step, mask(msg))

    # ------------------------------------------------------------ 单次运行
    def _run_once(self, prompt: str, title: str, files: list[Path], step: str) -> RunResult:
        self._n += 1
        self.log_dir.mkdir(parents=True, exist_ok=True)
        ev_path = self.log_dir / f"opencode-{self._n:02d}.jsonl"
        err_path = self.log_dir / f"opencode-{self._n:02d}.stderr.txt"
        cmd = [self.bin, "run", "--format", "json", "--dir", str(self.workdir), "-m", self.model,
               "--title", title]
        for f in files:
            cmd += ["-f", str(f)]
        cmd += ["--", prompt]
        kw = {"start_new_session": True} if os.name == "posix" else {}
        t0 = time.time()
        res = RunResult(ok=False, events_path=ev_path)
        tokens = {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0, "cache_write": 0}
        self._edited: set[str] = set()
        last_event = [time.time()]
        finish_reason = [None]
        cur_texts: dict[str, str] = {}
        errors: list[str] = []
        try:
            proc = subprocess.Popen(cmd, cwd=str(self.workdir), env=self._env(), stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kw)
        except FileNotFoundError:
            res.error = f"找不到 OpenCode 可执行文件：{self.bin}"
            return res

        stderr_chunks: list[bytes] = []

        def read_err():
            assert proc.stderr
            for line in proc.stderr:
                stderr_chunks.append(line)

        def read_out():
            assert proc.stdout
            with ev_path.open("w", encoding="utf-8") as evf:
                for raw in proc.stdout:
                    last_event[0] = time.time()
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    evf.write(mask(line) + "\n")
                    evf.flush()
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    try:
                        handle(ev)
                    except Exception as e:  # 解析失败不影响运行
                        self.ctx.log(step, f"（事件解析出错：{e}）")

        def handle(ev: dict) -> None:
            t = ev.get("type")
            part = ev.get("part") or {}
            if ev.get("sessionID") and not res.session_id:
                res.session_id = ev["sessionID"]
            if t == "step_start":
                res.steps += 1
                cur_texts.clear()
            elif t == "tool_use":
                res.tool_calls += 1
                if (part.get("state") or {}).get("status") == "error":
                    res.tool_errors += 1
                self._describe_tool(part, step)
            elif t == "text":
                txt = part.get("text") or ""
                cur_texts[part.get("id", "")] = txt
                if txt.strip():
                    res.all_text.append(txt)
            elif t == "step_finish":
                tk = part.get("tokens") or {}
                tokens["input"] += int(tk.get("input") or 0)
                tokens["output"] += int(tk.get("output") or 0)
                tokens["reasoning"] += int(tk.get("reasoning") or 0)
                c = tk.get("cache") or {}
                tokens["cache_read"] += int(c.get("read") or 0)
                tokens["cache_write"] += int(c.get("write") or 0)
                res.cost += float(part.get("cost") or 0)
                finish_reason[0] = part.get("reason")
                if cur_texts:
                    res.text = "\n".join(v for v in cur_texts.values() if v.strip())
            elif t == "error":
                err = ev.get("error") or part.get("error") or ev
                msg = err.get("data", {}).get("message") if isinstance(err, dict) else None
                msg = msg or (err.get("message") if isinstance(err, dict) else None) or json.dumps(err, ensure_ascii=False)
                errors.append(mask(str(msg)))
                self.ctx.log(step, f"模型调用出错：{truncate(mask(str(msg)), 300)}")

        th_o = threading.Thread(target=read_out, daemon=True)
        th_e = threading.Thread(target=read_err, daemon=True)
        th_o.start()
        th_e.start()
        killed = None
        try:
            while proc.poll() is None:
                if self.ctx.cancelled():
                    _kill_tree(proc)
                    raise PipelineCancelled()
                now = time.time()
                if now - t0 > self.timeout:
                    killed = f"超时（>{self.timeout}s）"
                    _kill_tree(proc)
                    break
                if now - last_event[0] > self.idle_timeout:
                    killed = f"{self.idle_timeout}s 没有任何进展"
                    _kill_tree(proc)
                    break
                time.sleep(0.5)
        finally:
            th_o.join(10)
            th_e.join(5)
            err_text = mask(b"".join(stderr_chunks).decode("utf-8", errors="replace"))
            err_path.write_text(err_text, encoding="utf-8")

        res.returncode = proc.returncode
        res.seconds = time.time() - t0
        res.tokens = tokens
        res.edited_files = sorted(self._edited)
        if killed:
            res.error = killed
        elif errors:
            res.error = errors[-1]
        elif proc.returncode != 0:
            res.error = f"opencode 返回码 {proc.returncode}：{truncate(err_text.strip()[-500:], 500)}"
        elif finish_reason[0] not in ("stop", "end_turn", None) and not res.text:
            res.error = f"会话没有正常结束（最后一步 reason={finish_reason[0]}）"
        elif res.steps == 0:
            res.error = "没有收到任何模型事件：" + truncate(err_text.strip()[-500:], 500)
        res.ok = res.error is None
        return res

    # ------------------------------------------------------------ 带重试
    def run(self, prompt: str, *, title: str, step: str = "智能体", files: list[Path] | None = None,
            retries: int = 2) -> RunResult:
        """新会话执行 prompt；失败（API 400、超时、无事件……）换新会话重试，最多 retries 次。"""
        files = [Path(f) for f in (files or [])]
        attempt = 0
        last: RunResult | None = None
        while True:
            attempt += 1
            p = prompt
            if attempt > 1 and last is not None:
                p = (f"（这是第 {attempt} 次尝试。上一次会话中途出错：{truncate(last.error or '', 300)}。"
                     "工作目录里可能已经有上一次留下的部分改动：先用 git status 和 git diff 看清现状，"
                     "在此基础上继续完成，不要重复已经做完的部分。）\n\n" + prompt)
            self.ctx.log(step, f"启动 OpenCode 会话（{self.model}，第 {attempt} 次）" if attempt > 1
                         else f"启动 OpenCode 会话（{self.model}）")
            r = self._run_once(p, title if attempt == 1 else f"{title}（重试{attempt - 1}）", files, step)
            r.attempts = attempt
            self.usage.add(r)
            tk = r.tokens
            self.ctx.log(step, f"会话结束：{'成功' if r.ok else '失败'}，{r.steps} 步、{r.tool_calls} 次工具调用，"
                               f"用时 {int(r.seconds)}s，词元 入{tk.get('input', 0):,}/缓存{tk.get('cache_read', 0):,}"
                               f"/出{tk.get('output', 0):,}" + ("" if r.ok else f"；原因：{truncate(r.error or '', 200)}"))
            if r.ok or attempt > retries:
                return r
            last = r
            time.sleep(min(10 * attempt, 30))
