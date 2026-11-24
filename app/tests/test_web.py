from __future__ import annotations

import io
from pathlib import Path

from fastapi.testclient import TestClient

from app.render import parse_diff, render_markdown

from .conftest import BASE, Client, git


def member(client: Client, username="teacher", password="teacher-pass-1") -> Client:
    """管理员发邀请码，另一个浏览器用它注册，返回该成员的 Client。"""
    code = client.make_invite()
    m = Client(TestClient(client.app), client.app)
    r = m.post("/register", {"code": code, "username": username,
                             "password": password, "password2": password})
    assert r.status_code == 303, r.text
    return m


# ---------------------------------------------------------------- setup
def test_setup_flow(client: Client):
    r = client.get("/")
    assert r.status_code == 303 and r.headers["location"] == BASE + "/setup"
    token = client.app.state.setup_token
    assert token

    r = client.post("/setup", {"token": "wrong", "username": "admin",
                               "password": "admin-pass-123", "password2": "admin-pass-123"})
    assert r.status_code == 400 and "token" in r.text

    r = client.post("/setup", {"token": token, "username": "admin",
                               "password": "short", "password2": "short"})
    assert r.status_code == 400

    client.setup_admin()
    assert client.app.state.setup_token is None
    assert client.db.get_user_by_name("admin")["role"] == "admin"
    r = client.get("/tasks")
    assert r.status_code == 200 and "我的任务" in r.text

    # 已初始化后 /setup 不再可用
    assert client.get("/setup").status_code == 303
    r = client.post("/setup", {"token": token, "username": "x2", "password": "p" * 10, "password2": "p" * 10})
    assert r.status_code == 303
    assert client.db.count_users() == 1


# --------------------------------------------------------------- invites
def test_invite_register(admin_client: Client):
    m = member(admin_client)
    r = m.get("/tasks")
    assert r.status_code == 200 and "teacher" in r.text
    assert admin_client.db.get_user_by_name("teacher")["role"] == "member"
    # 成员不能进管理页
    assert m.get("/admin").status_code == 403

    # 邀请码只能用一次
    code = admin_client.make_invite()
    a = Client(TestClient(admin_client.app), admin_client.app)
    assert a.post("/register", {"code": code, "username": "u1", "password": "pppppppp1",
                                "password2": "pppppppp1"}).status_code == 303
    b = Client(TestClient(admin_client.app), admin_client.app)
    r = b.post("/register", {"code": code, "username": "u2", "password": "pppppppp1", "password2": "pppppppp1"})
    assert r.status_code == 400 and "邀请码无效" in r.text

    # 过期的邀请码
    admin_client.db.create_invite("OLD0-OLD0-OLD0", "member", 1, "2000-01-01T00:00:00+08:00")
    r = b.post("/register", {"code": "OLD0-OLD0-OLD0", "username": "u3",
                             "password": "pppppppp1", "password2": "pppppppp1"})
    assert r.status_code == 400

    # 管理员邀请码 → 注册即为管理员
    code = admin_client.make_invite(role="admin")
    c = Client(TestClient(admin_client.app), admin_client.app)
    c.post("/register", {"code": code, "username": "boss2", "password": "pppppppp1", "password2": "pppppppp1"})
    assert admin_client.db.get_user_by_name("boss2")["role"] == "admin"


# ------------------------------------------------------------ login/CSRF
def test_login_logout_csrf(admin_client: Client):
    c = admin_client
    c.logout()
    assert c.get("/tasks").status_code == 303           # 未登录 → 跳登录
    assert "next=" in c.get("/tasks").headers["location"]

    # 没有 CSRF 字段 → 403
    r = c.post("/login", {"username": "admin", "password": "admin-pass-123"}, csrf=False)
    assert r.status_code == 403
    # CSRF 值不匹配 → 403
    r = c.post("/login", {"username": "admin", "password": "admin-pass-123", "csrf": "forged"})
    assert r.status_code == 403

    r = c.post("/login", {"username": "admin", "password": "wrong-password"})
    assert r.status_code == 400 and "用户名或密码错误" in r.text
    c.login("admin", "admin-pass-123")
    assert c.get("/tasks").status_code == 200

    # 登录后改状态的请求同样要 CSRF
    r = c.post("/tasks/new", {"kind": "maintain", "title": "x", "request": "y"}, csrf=False)
    assert r.status_code == 403

    # cookie 属性
    c.logout()
    r = c.post("/login", {"username": "admin", "password": "admin-pass-123"})
    sc = r.headers.get_list("set-cookie")
    sess = next(h for h in sc if h.startswith("bs_session="))
    assert "HttpOnly" in sess and "samesite=lax" in sess.lower() and f"Path={BASE}" in sess


def test_login_rate_limit(admin_client: Client):
    c = admin_client
    c.logout()
    for _ in range(5):
        assert c.post("/login", {"username": "admin", "password": "bad-password"}).status_code == 400
    r = c.post("/login", {"username": "admin", "password": "admin-pass-123"})
    assert r.status_code == 429 and "次数过多" in r.text


def test_tampered_session_rejected(admin_client: Client):
    c = admin_client
    tok = c.tc.cookies.get("bs_session")
    c.tc.cookies.set("bs_session", tok[:-2] + ("AA" if not tok.endswith("AA") else "BB"), path=BASE)
    assert c.get("/tasks").status_code == 303


# ----------------------------------------------------------------- quota
def test_daily_quota(admin_client: Client):
    m = member(admin_client)
    uid = admin_client.db.get_user_by_name("teacher")["id"]
    r = admin_client.post(f"/admin/users/{uid}", {"role": "member", "daily_quota": "1"})
    assert r.status_code == 303
    assert m.new_task().status_code == 303
    r = m.new_task(title="second")
    assert r.status_code == 429 and "额度已用完" in r.text
    assert len(admin_client.db.list_tasks(user_id=uid)) == 1


def test_cannot_demote_last_admin(admin_client: Client):
    uid = admin_client.db.get_user_by_name("admin")["id"]
    admin_client.post(f"/admin/users/{uid}", {"role": "member", "daily_quota": "10"})
    assert admin_client.db.get_user(uid)["role"] == "admin"


# --------------------------------------------- 全流程：建任务 → 跑 → 合并
def test_task_to_merge(admin_client: Client, book_repo: Path):
    c = admin_client
    m = member(c)
    files = [("attachments", ("批注.pdf", io.BytesIO(b"%PDF-1.4 fake"), "application/pdf"))]
    r = m.new_task(files=files, chapter=1)
    assert r.status_code == 303
    tid = int(r.headers["location"].rsplit("/", 1)[1])
    t = c.db.get_task(tid)
    assert t["status"] == "queued" and t["params"]["attachments"][0]["name"] == "批注.pdf"

    assert c.worker.run_once() is True
    t = c.db.get_task(tid)
    assert t["status"] == "awaiting_review", c.db.events_after(tid)
    assert t["branch"].startswith(f"agent/{tid}-")
    kinds = {a["kind"] for a in c.db.list_artifacts(tid)}
    assert {"pdf", "diff"} <= kinds
    assert "booksmith-fake" not in git(book_repo, "ls-tree", "-r", "--name-only", "master")

    # 审核页：说明、diff、PDF iframe
    page = m.get(f"/tasks/{tid}").text
    assert "智能体说明" in page and "改动对比" in page and f"booksmith-fake/task-{tid}.md" in page
    assert "<iframe" in page and "dl-add" in page
    # 附件可下载
    assert m.get(f"/tasks/{tid}/inputs/0").content == b"%PDF-1.4 fake"
    # PDF 产物带鉴权可访问
    pdf = next(a for a in c.db.list_artifacts(tid) if a["kind"] == "pdf")
    r = m.get(f"/tasks/{tid}/artifacts/{pdf['id']}")
    assert r.status_code == 200 and r.content.startswith(b"%PDF") and "inline" in r.headers["content-disposition"]

    # 成员不能通过
    assert m.post(f"/tasks/{tid}/approve").status_code == 403
    assert c.db.get_task(tid)["status"] == "awaiting_review"

    r = c.post(f"/tasks/{tid}/approve")
    assert r.status_code == 303
    assert c.db.get_task(tid)["status"] == "merged"
    files_on_master = git(book_repo, "ls-tree", "-r", "--name-only", "master")
    assert f"booksmith-fake/task-{tid}.md" in files_on_master
    assert f"合并任务 #{tid}" in git(book_repo, "log", "--merges", "-1", "--format=%B", "master")
    assert not (c.app.state.settings.worktrees_dir / str(tid)).exists()
    assert "已合并进 master" in c.get(f"/tasks/{tid}").text or "已合并" in c.get(f"/tasks/{tid}").text


def test_merge_conflict_keeps_master(admin_client: Client, book_repo: Path):
    c = admin_client
    tid = int(c.new_task().headers["location"].rsplit("/", 1)[1])
    c.worker.run_once()
    # master 上抢先改了同一个文件 → 合并冲突
    p = book_repo / "booksmith-fake" / f"task-{tid}.md"
    p.parent.mkdir(exist_ok=True)
    p.write_text("conflicting line\n", encoding="utf-8")
    git(book_repo, "add", "-A")
    git(book_repo, "commit", "-q", "-m", "conflict")
    before = git(book_repo, "rev-parse", "master")
    r = c.post(f"/tasks/{tid}/approve")
    assert r.status_code == 303
    assert c.db.get_task(tid)["status"] == "awaiting_review"
    assert git(book_repo, "rev-parse", "master") == before
    assert git(book_repo, "status", "--porcelain") == ""


def test_merge_when_main_clone_not_on_master(admin_client: Client, book_repo: Path):
    c = admin_client
    git(book_repo, "checkout", "-q", "-b", "drafts")
    tid = int(c.new_task().headers["location"].rsplit("/", 1)[1])
    c.worker.run_once()
    c.post(f"/tasks/{tid}/approve")
    assert c.db.get_task(tid)["status"] == "merged"
    assert f"booksmith-fake/task-{tid}.md" in git(book_repo, "ls-tree", "-r", "--name-only", "master")
    assert git(book_repo, "symbolic-ref", "--short", "HEAD").strip() == "drafts"
    assert len(git(book_repo, "worktree", "list").strip().splitlines()) == 1


# ------------------------------------------------------------ 打回 → 子任务
def test_reject_creates_child_on_same_branch(admin_client: Client, book_repo: Path):
    c = admin_client
    m = member(c)
    tid = int(m.new_task().headers["location"].rsplit("/", 1)[1])
    c.worker.run_once()
    branch = c.db.get_task(tid)["branch"]

    r = m.post(f"/tasks/{tid}/reject", {"feedback": "表格少了一列显存"})
    assert r.status_code == 303
    child_id = int(r.headers["location"].rsplit("/", 1)[1])
    parent, child = c.db.get_task(tid), c.db.get_task(child_id)
    assert parent["status"] == "rejected" and parent["feedback"] == "表格少了一列显存"
    assert child["parent_id"] == tid and child["branch"] == branch
    assert child["feedback"] == "表格少了一列显存" and child["status"] == "queued"
    assert child["user_id"] == parent["user_id"]

    c.worker.run_once()
    child = c.db.get_task(child_id)
    assert child["status"] == "awaiting_review"
    assert child["branch"] == branch
    # 两轮的提交都在同一分支上
    assert int(git(book_repo, "rev-list", "--count", f"master..{branch}").strip()) == 2

    c.post(f"/tasks/{child_id}/approve")
    assert c.db.get_task(child_id)["status"] == "merged"
    content = git(book_repo, "show", f"master:booksmith-fake/task-{tid}.md")
    assert "表格少了一列显存" in content


# -------------------------------------------------------- newchapter 大纲闸门
def test_newchapter_outline_gate(admin_client: Client, book_repo: Path):
    c = admin_client
    tid = int(c.new_task(kind="newchapter", title="GraphRAG", request="第 5 章加一节 GraphRAG",
                         chapter=5).headers["location"].rsplit("/", 1)[1])
    c.worker.run_once()
    t = c.db.get_task(tid)
    assert t["status"] == "awaiting_outline"
    assert "核心概念" in t["summary"]
    assert not any(a["kind"] == "diff" for a in c.db.list_artifacts(tid))
    assert int(git(book_repo, "rev-list", "--count", f"master..{t['branch']}").strip()) == 0
    # 大纲未确认时不能通过
    assert c.post(f"/tasks/{tid}/approve").status_code == 303
    assert c.db.get_task(tid)["status"] == "awaiting_outline"
    # worker 不会再取它
    assert c.worker.run_once() is False

    page = c.get(f"/tasks/{tid}/outline")
    assert page.status_code == 200 and "确认大纲" in page.text
    r = c.post(f"/tasks/{tid}/outline", {"outline": "# GraphRAG\n\n1. 我改过的第一节\n2. 第二节"})
    assert r.status_code == 303
    t = c.db.get_task(tid)
    assert t["status"] == "queued" and "我改过的第一节" in t["outline"]

    c.worker.run_once()
    t = c.db.get_task(tid)
    assert t["status"] == "awaiting_review"
    tex = git(book_repo, "show", f"{t['branch']}:chapters/fake-task-{tid}.tex")
    assert "我改过的第一节" in tex


def test_other_kinds_produce_artifacts(admin_client: Client):
    c = admin_client
    r1 = c.new_task(kind="exercises", title="Ch3 exam", request="出 5 道题", chapter=3, count=5)
    r2 = c.new_task(kind="slides", title="Ch2 slides", request="做一讲课件", chapter=2, style="学术")
    assert r1.status_code == 303 and r2.status_code == 303
    c.worker.run_once()
    c.worker.run_once()
    t1 = int(r1.headers["location"].rsplit("/", 1)[1])
    t2 = int(r2.headers["location"].rsplit("/", 1)[1])
    names1 = {a["name"] for a in c.db.list_artifacts(t1)}
    assert {"学生版.pdf", "教师版.pdf", "changes.diff"} <= names1
    assert c.db.get_task(t2)["status"] == "awaiting_review"
    # 习题必须填章节
    r = c.new_task(kind="exercises", title="no chapter", request="x")
    assert r.status_code == 400


def test_cancel_queued_task(admin_client: Client):
    c = admin_client
    tid = int(c.new_task().headers["location"].rsplit("/", 1)[1])
    assert c.post(f"/tasks/{tid}/cancel").status_code == 303
    assert c.db.get_task(tid)["status"] == "cancelled"
    assert c.worker.run_once() is False


def test_cancel_while_running(admin_client: Client):
    c = admin_client
    tid = int(c.new_task().headers["location"].rsplit("/", 1)[1])
    # 模拟：流水线跑到一半时用户取消
    from app import pipelines
    real = pipelines.get_pipeline("maintain")

    class Cancelling:
        kind = "maintain"

        def run(self, ctx):
            c.db.update_task(tid, status="cancelled")
            return real.run(ctx)

    pipelines.register(Cancelling())
    c.worker.run_once()
    assert c.db.get_task(tid)["status"] == "cancelled"
    pipelines.register(real)


def test_pipeline_exception_marks_failed(admin_client: Client):
    from app import pipelines

    class Boom:
        kind = "slides"

        def run(self, ctx):
            raise RuntimeError("模型挂了")

    pipelines.register(Boom())
    c = admin_client
    tid = int(c.new_task(kind="slides", chapter=1).headers["location"].rsplit("/", 1)[1])
    c.worker.run_once()
    t = c.db.get_task(tid)
    assert t["status"] == "failed" and "模型挂了" in t["summary"]


# ---------------------------------------------------------------- 权限
def test_artifact_requires_login(admin_client: Client):
    c = admin_client
    tid = int(c.new_task().headers["location"].rsplit("/", 1)[1])
    c.worker.run_once()
    aid = c.db.list_artifacts(tid)[0]["id"]
    anon = Client(TestClient(c.app), c.app)
    assert anon.get(f"/tasks/{tid}/artifacts/{aid}").status_code == 401
    assert anon.get(f"/api/tasks/{tid}/events").status_code == 401
    assert anon.get(f"/api/tasks/{tid}").status_code == 401
    # 别的成员看不到
    m = member(c)
    assert m.get(f"/tasks/{tid}/artifacts/{aid}").status_code == 404
    assert m.get(f"/tasks/{tid}").status_code == 404
    # 产物 id 和任务 id 不匹配
    tid2 = int(m.new_task().headers["location"].rsplit("/", 1)[1])
    assert m.get(f"/tasks/{tid2}/artifacts/{aid}").status_code == 404


# ---------------------------------------------------------- BASE_PATH
def test_base_path_both_forms(admin_client: Client):
    c = admin_client
    raw = c.tc
    for p in ("/login", BASE + "/login"):
        r = raw.get(p, follow_redirects=False)
        assert r.status_code in (200, 303), p
    anon = TestClient(c.app)
    for p in ("/login", BASE + "/login"):
        r = anon.get(p)
        assert r.status_code == 200
        assert f'href="{BASE}/static/style.css"' in r.text
        assert f'action="{BASE}/login"' in r.text
    for p in ("/static/style.css", BASE + "/static/style.css"):
        assert anon.get(p).status_code == 200
    assert anon.get(BASE, follow_redirects=False).headers["location"].startswith(BASE + "/")
    assert anon.get("/healthz").json()["ok"] is True
    # 已被 nginx 剥掉前缀的请求（cookie 由浏览器按外部路径 /booksmith 发送）
    session = c.tc.cookies.get("bs_session")
    r = anon.get("/tasks", headers={"Cookie": f"bs_session={session}"}, follow_redirects=False)
    assert r.status_code == 200 and "我的任务" in r.text
    # 重定向带前缀
    r = anon.get("/tasks", follow_redirects=False)
    assert r.headers["location"].startswith(BASE + "/login")
    # 安全响应头
    assert "frame-ancestors 'self'" in r.headers["content-security-policy"]


def test_root_base_path(tmp_path, book_repo, monkeypatch):
    from app.config import Settings
    from app.main import create_app
    monkeypatch.setenv("BOOKSMITH_FAKE_DELAY", "0")
    s = Settings(base_path="/", data_dir=tmp_path / "d2", book_repo_dir=book_repo,
                 secret_key="x", start_worker=False)
    with TestClient(create_app(s)) as tc:
        r = tc.get("/login", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/setup"
        assert 'href="/static/style.css"' in tc.get("/setup").text


# --------------------------------------------------------------- SSE
def test_sse_stream(admin_client: Client):
    c = admin_client
    tid = int(c.new_task().headers["location"].rsplit("/", 1)[1])
    c.worker.run_once()
    with c.tc.stream("GET", f"{BASE}/api/tasks/{tid}/events") as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        body = "".join(r.iter_text())
    assert "event: log" in body and "event: status" in body and "event: done" in body
    assert "awaiting_review" in body
    # Last-Event-ID 续传
    last = c.db.events_after(tid)[-1]["id"]
    with c.tc.stream("GET", f"{BASE}/api/tasks/{tid}/events", headers={"Last-Event-ID": str(last)}) as r:
        body2 = "".join(r.iter_text())
    assert "event: log" not in body2 and "event: done" in body2
    j = c.get(f"/api/tasks/{tid}").json()
    assert j["status"] == "awaiting_review" and j["artifacts"]


def test_worker_thread_runs_tasks(admin_client: Client):
    import time
    c = admin_client
    tid = int(c.new_task().headers["location"].rsplit("/", 1)[1])
    c.worker.start()
    try:
        for _ in range(100):
            if c.db.get_status(tid) == "awaiting_review":
                break
            time.sleep(0.1)
    finally:
        c.worker.stop()
    assert c.db.get_status(tid) == "awaiting_review"


# --------------------------------------------------------------- 渲染
def test_markdown_is_safe():
    html = str(render_markdown("<script>alert(1)</script>\n\n[x](javascript:alert(1))\n\n**粗**\n\n| a | b |\n|---|---|\n| 1 | 2 |"))
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert 'href="javascript:' not in html
    assert "<strong>粗</strong>" in html and "<table>" in html


def test_parse_diff_and_truncation():
    d = ("diff --git a/x.tex b/x.tex\nindex 1..2 100644\n--- a/x.tex\n+++ b/x.tex\n@@ -1,2 +1,2 @@\n"
         " same\n-old\n+new\n"
         "diff --git a/n.md b/n.md\nnew file mode 100644\n--- /dev/null\n+++ b/n.md\n@@ -0,0 +1 @@\n+hi\n")
    p = parse_diff(d)
    assert [f.name for f in p.files] == ["x.tex", "n.md"]
    assert p.files[0].added == 1 and p.files[0].removed == 1 and p.files[1].status == "新增"
    big = "diff --git a/b b/b\n--- a/b\n+++ b/b\n@@ -0,0 +1,2000 @@\n" + "+l\n" * 2000
    p = parse_diff(big, max_lines_per_file=100)
    assert p.truncated and p.files[0].added == 2000 and len(p.files[0].lines) == 100


def test_upload_limits(admin_client: Client):
    c = admin_client
    c.app.state.settings.max_upload_bytes = 1000
    files = [("attachments", ("big.pdf", io.BytesIO(b"x" * 2000), "application/pdf"))]
    r = c.new_task(files=files)
    assert r.status_code == 413
    files = [("attachments", ("evil.exe", io.BytesIO(b"x"), "application/octet-stream"))]
    r = c.new_task(files=files)
    assert r.status_code == 400 and "不支持的附件类型" in r.text
