"""运行配置：全部来自环境变量（见 docs/04-架构与接口.md 与 .env.example）。"""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path


def load_dotenv(path: str | os.PathLike = ".env") -> None:
    """极简 .env 读取：只设置尚未存在的变量。不引入额外依赖。"""
    p = Path(path)
    if not p.is_file():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def normalize_base_path(value: str | None) -> str:
    """'/booksmith/' -> '/booksmith'；'' 或 '/' -> ''（挂在根路径）。"""
    v = (value if value is not None else "/booksmith").strip()
    if not v or v == "/":
        return ""
    if not v.startswith("/"):
        v = "/" + v
    return v.rstrip("/")


@dataclass
class Settings:
    base_path: str = "/booksmith"
    port: int = 8100
    data_dir: Path = Path("/data")
    book_repo_dir: Path = Path("/book")
    book_git_remote: str = ""
    book_base_branch: str = "master"
    secret_key: str = ""
    pipelines: str = "fake"          # fake | real
    start_worker: bool = True        # 测试时关掉，手动调用 worker.run_once()
    max_upload_bytes: int = 50 * 1024 * 1024
    default_quota: int = 10
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ
        return cls(
            base_path=env.get("BASE_PATH") or cls.base_path,
            port=int(env.get("PORT") or cls.port),
            data_dir=Path(env.get("DATA_DIR", cls.data_dir)).absolute(),
            book_repo_dir=Path(env.get("BOOK_REPO_DIR") or cls.book_repo_dir),
            book_git_remote=env.get("BOOK_GIT_REMOTE", "").strip(),
            book_base_branch=env.get("BOOK_BASE_BRANCH", "master").strip() or "master",
            secret_key=env.get("SECRET_KEY", ""),
            pipelines=env.get("BOOKSMITH_PIPELINES", "fake").strip() or "fake",
            start_worker=env.get("BOOKSMITH_WORKER", "1") not in ("0", "false", "no"),
        )

    def __post_init__(self) -> None:
        self.base_path = normalize_base_path(self.base_path)
        self.data_dir = Path(self.data_dir)
        self.book_repo_dir = Path(self.book_repo_dir)

    # ---- 派生路径 ----
    @property
    def db_path(self) -> Path:
        return self.data_dir / "booksmith.sqlite3"

    @property
    def tasks_dir(self) -> Path:
        return self.data_dir / "tasks"

    @property
    def worktrees_dir(self) -> Path:
        return self.data_dir / "worktrees"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.tasks_dir, self.worktrees_dir):
            d.mkdir(parents=True, exist_ok=True)

    def resolve_secret(self) -> str:
        """SECRET_KEY 为空时，生成一个并保存在 DATA_DIR/secret_key（仅适合开发/单机）。"""
        if self.secret_key:
            return self.secret_key
        f = self.data_dir / "secret_key"
        if f.is_file():
            self.secret_key = f.read_text(encoding="utf-8").strip()
        else:
            self.secret_key = secrets.token_urlsafe(48)
            f.write_text(self.secret_key, encoding="utf-8")
            try:
                os.chmod(f, 0o600)
            except OSError:
                pass
        return self.secret_key
