from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

BASE = "/booksmith"


def git(cwd: Path, *args: str) -> str:
    p = subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@t", *args], cwd=cwd,
                       capture_output=True, text=True, encoding="utf-8", check=True)
    return p.stdout


@pytest.fixture
def book_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "book"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "checkout", "-q", "-b", "master")
    (repo / "main.tex").write_text("\\documentclass{book}\n% 书稿\n", encoding="utf-8")
    (repo / "chapters").mkdir()
    (repo / "chapters" / "ch01.tex").write_text("第一章\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    return repo


@pytest.fixture
def settings(tmp_path: Path, book_repo: Path, monkeypatch) -> Settings:
    monkeypatch.setenv("BOOKSMITH_FAKE_DELAY", "0")
    return Settings(base_path=BASE, data_dir=tmp_path / "data", book_repo_dir=book_repo,
                    secret_key="test-secret", pipelines="fake", start_worker=False)


class Client:
    """包一层 TestClient：自动带 CSRF、默认不跟随重定向。"""

    def __init__(self, tc: TestClient, app):
        self.tc = tc
        self.app = app

    @property
    def db(self):
        return self.app.state.db

    @property
    def worker(self):
        return self.app.state.worker

    def csrf(self) -> str:
        tok = self.tc.cookies.get("bs_csrf")
        if not tok:
            self.tc.get(BASE + "/login")
            tok = self.tc.cookies.get("bs_csrf")
        return tok

    def get(self, path: str, **kw):
        kw.setdefault("follow_redirects", False)
        return self.tc.get(BASE + path if path.startswith("/") else path, **kw)

    def post(self, path: str, data: dict | None = None, csrf: bool = True, **kw):
        kw.setdefault("follow_redirects", False)
        data = dict(data or {})
        if csrf:
            data.setdefault("csrf", self.csrf())
        return self.tc.post(BASE + path, data=data, **kw)

    def setup_admin(self, username="admin", password="admin-pass-123") -> None:
        token = self.app.state.setup_token
        r = self.post("/setup", {"token": token, "username": username,
                                 "password": password, "password2": password})
        assert r.status_code == 303, r.text

    def login(self, username, password) -> None:
        r = self.post("/login", {"username": username, "password": password})
        assert r.status_code == 303, r.text

    def logout(self) -> None:
        self.post("/logout")

    def make_invite(self, role="member", days="7") -> str:
        r = self.post("/admin/invites", {"role": role, "days": days})
        assert r.status_code == 303
        m = re.search(r"new=([A-Z0-9%-]+)", r.headers["location"])
        return m.group(1).replace("%2D", "-")

    def new_task(self, kind="maintain", title="Fix table 1-6", request="把表 1-6 换成最新型号",
                 files=None, **params):
        data = {"kind": kind, "title": title, "request": request, **{k: str(v) for k, v in params.items()}}
        return self.post("/tasks/new", data, files=files)


@pytest.fixture
def app(settings):
    return create_app(settings)


@pytest.fixture
def client(app):
    with TestClient(app) as tc:
        yield Client(tc, app)


@pytest.fixture
def admin_client(client):
    client.setup_admin()
    return client


def other_client(app) -> TestClient:
    return TestClient(app)
