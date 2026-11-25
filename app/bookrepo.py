"""书稿仓库操作：为任务建分支和 worktree、提交、diff、合并、推送、清理。

约定：
- 主克隆在 BOOK_REPO_DIR，基线分支 master（BOOK_BASE_BRANCH 可改）。
- 每个任务分支 `agent/<id>-<slug>`；打回产生的子任务沿用父任务的分支。
- worktree 放在 DATA_DIR/worktrees/<task_id>。
- 合并用 `git merge --no-ff`；冲突时 `git merge --abort` 并抛 RepoError，master 保持原状。
- 所有改动引用（建分支、加/删 worktree、合并）的操作都在一把进程内锁里串行。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

GIT_NAME = "BookSmith"
GIT_EMAIL = "booksmith@localhost"

_lock = threading.RLock()


class RepoError(RuntimeError):
    pass


def slugify(title: str, fallback: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    s = s[:30].strip("-")
    return s or fallback


def branch_name(task_id: int, title: str, kind: str) -> str:
    return f"agent/{task_id}-{slugify(title, kind)}"


def git(args: list[str], cwd: Path | str, check: bool = True, timeout: int = 300) -> subprocess.CompletedProcess:
    cmd = ["git", "-c", "core.quotepath=false", "-c", f"user.name={GIT_NAME}",
           "-c", f"user.email={GIT_EMAIL}", *args]
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = env.get("GIT_TERMINAL_PROMPT") or "0"
    env["LC_ALL"] = env.get("LC_ALL") or "C.UTF-8"
    p = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=env, timeout=timeout)
    if check and p.returncode != 0:
        raise RepoError(f"git {' '.join(args)} 失败（{p.returncode}）：{(p.stderr or p.stdout).strip()}")
    return p


class BookRepo:
    def __init__(self, repo_dir: Path | str, worktrees_dir: Path | str,
                 base_branch: str = "master", remote: str = ""):
        self.repo = Path(repo_dir)
        self.worktrees = Path(worktrees_dir)
        self.base = base_branch
        self.remote = remote

    # ---------- 检查 ----------
    def check(self) -> None:
        if not (self.repo / ".git").exists():
            raise RepoError(f"书稿仓库不存在或不是 git 仓库：{self.repo}")
        p = git(["rev-parse", "--verify", "--quiet", f"refs/heads/{self.base}"], self.repo, check=False)
        if p.returncode != 0:
            raise RepoError(f"书稿仓库没有 {self.base} 分支：{self.repo}")

    def branch_exists(self, branch: str) -> bool:
        return git(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
                   self.repo, check=False).returncode == 0

    def worktree_path(self, task_id: int) -> Path:
        return self.worktrees / str(task_id)

    # ---------- worktree ----------
    def ensure_worktree(self, task_id: int, branch: str) -> Path:
        """为任务准备 worktree：分支不存在则从 master 新建；目录已存在且有效则复用。"""
        with _lock:
            self.check()
            path = self.worktree_path(task_id)
            if path.exists():
                p = git(["rev-parse", "--abbrev-ref", "HEAD"], path, check=False)
                if p.returncode == 0 and p.stdout.strip() == branch:
                    return path
                self._remove_worktree_dir(path)
            git(["worktree", "prune"], self.repo, check=False)
            self.worktrees.mkdir(parents=True, exist_ok=True)
            if self.branch_exists(branch):
                git(["worktree", "add", str(path), branch], self.repo)
            else:
                git(["worktree", "add", "-b", branch, str(path), self.base], self.repo)
            return path

    def _remove_worktree_dir(self, path: Path) -> None:
        git(["worktree", "remove", "--force", str(path)], self.repo, check=False)
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
        git(["worktree", "prune"], self.repo, check=False)

    def remove_worktree(self, task_id: int) -> None:
        with _lock:
            self._remove_worktree_dir(self.worktree_path(task_id))

    def delete_branch(self, branch: str, force: bool = False) -> None:
        with _lock:
            git(["branch", "-D" if force else "-d", branch], self.repo, check=False)

    # ---------- 提交 / diff ----------
    @staticmethod
    def commit_all(workdir: Path | str, message: str) -> bool:
        """把 worktree 里所有改动提交；没有改动返回 False。流水线可直接调用。"""
        git(["add", "-A"], workdir)
        if git(["diff", "--cached", "--quiet"], workdir, check=False).returncode == 0:
            return False
        git(["commit", "-q", "-m", message], workdir)
        return True

    def diff(self, branch: str) -> str:
        """分支相对 master（merge-base）的 unified diff。"""
        p = git(["diff", "--no-color", "--find-renames", f"{self.base}...{branch}"], self.repo)
        return p.stdout

    def ahead_count(self, branch: str) -> int:
        p = git(["rev-list", "--count", f"{self.base}..{branch}"], self.repo, check=False)
        try:
            return int(p.stdout.strip())
        except ValueError:
            return 0

    # ---------- 合并 / 推送 ----------
    def _current_branch(self, path: Path) -> str | None:
        p = git(["symbolic-ref", "--quiet", "--short", "HEAD"], path, check=False)
        return p.stdout.strip() if p.returncode == 0 else None

    def merge(self, branch: str, message: str) -> str:
        """把分支 --no-ff 合并进 master，返回合并后的 master 提交号。冲突则恢复原状并抛错。"""
        with _lock:
            self.check()
            if not self.branch_exists(branch):
                raise RepoError(f"分支不存在：{branch}")
            if self._current_branch(self.repo) == self.base:
                where = self.repo
                dirty = git(["status", "--porcelain", "--untracked-files=no"], where).stdout.strip()
                if dirty:
                    raise RepoError(f"主仓库 {self.base} 有未提交的改动，先处理再合并：\n{dirty}")
                temp = None
            else:
                # 主克隆没检出 master（或是别的分支），用临时 worktree 合并
                temp = self.worktrees / "_merge"
                if temp.exists():
                    self._remove_worktree_dir(temp)
                git(["worktree", "add", str(temp), self.base], self.repo)
                where = temp
            try:
                p = git(["merge", "--no-ff", "-m", message, branch], where, check=False)
                if p.returncode != 0:
                    git(["merge", "--abort"], where, check=False)
                    raise RepoError("合并冲突，master 保持不变。请让智能体在分支上基于最新 master 重做，"
                                    f"或手工解决。\n{(p.stdout + p.stderr).strip()}")
                return git(["rev-parse", "HEAD"], where).stdout.strip()
            finally:
                if temp is not None:
                    self._remove_worktree_dir(temp)

    def push(self) -> str:
        """推送 master 到 BOOK_GIT_REMOTE（远程名或 URL）。未配置时返回空串。"""
        if not self.remote:
            return ""
        with _lock:
            p = git(["push", self.remote, f"{self.base}:{self.base}"], self.repo, check=False, timeout=180)
            if p.returncode != 0:
                raise RepoError(f"推送失败：{(p.stderr or p.stdout).strip()}")
            return (p.stderr or p.stdout).strip()
