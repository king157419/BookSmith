"""BookSmith 网页入口（FastAPI）。

BASE_PATH 方案：路由本身不带前缀（/tasks、/static/...），由最外层的 PrefixMiddleware
在路由之前把请求路径里的 BASE_PATH 前缀剥掉（有就剥，没有就原样），所以
"带前缀"（直连 :8100/booksmith/...，或 nginx proxy_pass 不带末尾斜杠）和
"已被剥掉前缀"（nginx `proxy_pass http://127.0.0.1:8100/;`）两种请求都能处理。
页面里生成的所有链接、重定向、静态资源 URL、cookie path 一律带 BASE_PATH 前缀。
"""
from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import re
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote, unquote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse, RedirectResponse,
                               Response, StreamingResponse)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.datastructures import FormData, UploadFile

from . import auth, pipelines
from .bookrepo import BookRepo, RepoError
from .config import Settings, load_dotenv
from .db import ACTIVE_STATUSES, DB, KINDS, TZ, parse_ts
from .render import parse_diff, render_markdown
from .worker import Worker

log = logging.getLogger("booksmith")

APP_DIR = Path(__file__).resolve().parent

STATUS_LABELS = {
    "queued": "排队中", "running": "进行中", "awaiting_outline": "待确认大纲",
    "awaiting_review": "待审核", "merged": "已合并", "rejected": "已打回",
    "failed": "失败", "cancelled": "已取消",
}
KIND_LABELS = {"maintain": "维护", "exercises": "习题", "slides": "课件", "newchapter": "出新"}
KIND_HINTS = {
    "maintain": "按需求修改已有内容：更新数据、修错、统一术语、按老师批注改。",
    "exercises": "根据章节出题，产出学生版（无答案）和教师版（含答案与解析）PDF。",
    "slides": "根据章节做一讲课件（PPTX + PDF 预览），可上传参考模板。",
    "newchapter": "检索资料写新的一节或一章：先出大纲给你确认，确认后再写正文。",
}
SLIDE_STYLES = ["简洁", "学术", "活泼", "按上传的模板"]
ROLE_LABELS = {"admin": "管理员", "member": "成员"}
CANCELLABLE = ("queued", "running", "awaiting_outline", "awaiting_review")
ALLOWED_UPLOAD_EXT = {".pdf", ".pptx", ".potx", ".ppt", ".docx", ".doc", ".png", ".jpg",
                      ".jpeg", ".txt", ".md", ".tex", ".zip"}


# =====================================================================
# BASE_PATH 前缀处理 + 安全响应头（纯 ASGI 中间件，不影响 SSE 流式输出）
# =====================================================================
class PrefixMiddleware:
    SECURITY_HEADERS = [
        (b"x-content-type-options", b"nosniff"),
        (b"x-frame-options", b"SAMEORIGIN"),
        (b"referrer-policy", b"same-origin"),
        (b"content-security-policy",
         b"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
         b"frame-src 'self'; frame-ancestors 'self'; object-src 'none'; base-uri 'self'; "
         b"form-action 'self'"),
    ]

    def __init__(self, app, base_path: str = ""):
        self.app = app
        self.base = base_path

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        base = self.base
        path: str = scope.get("path", "")
        if base and (path == base or path.startswith(base + "/")):
            scope = dict(scope)
            new = path[len(base):] or "/"
            scope["path"] = new
            raw = scope.get("raw_path")
            if isinstance(raw, (bytes, bytearray)) and raw.startswith(base.encode()):
                scope["raw_path"] = bytes(raw[len(base.encode()):]) or b"/"
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {k.lower() for k, _ in headers}
                for k, v in self.SECURITY_HEADERS:
                    if k not in present:
                        headers.append((k, v))
                message = dict(message)
                message["headers"] = headers
            await send(message)

        return await self.app(scope, receive, send_wrapper)


class LoginRequired(Exception):
    pass


# =====================================================================
# 应用工厂
# =====================================================================
def create_app(settings: Settings | None = None) -> FastAPI:
    if settings is None:
        load_dotenv()
        settings = Settings.from_env()
    BASE = settings.base_path

    db = DB(settings.db_path)
    repo = BookRepo(settings.book_repo_dir, settings.worktrees_dir,
                    settings.book_base_branch, settings.book_git_remote)
    worker = Worker(settings, db, repo)
    limiter = auth.LoginLimiter()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings.ensure_dirs()
        db.init()
        app.state.signer = auth.Signer(settings.resolve_secret())
        pipelines.load_pipelines(settings.pipelines)
        interrupted = db.fail_interrupted()
        if interrupted:
            log.warning("上次中断的任务已标记失败：%s", interrupted)
        try:
            repo.check()
        except RepoError as e:
            log.warning("%s（任务会失败，直到配置好 BOOK_REPO_DIR）", e)
        if db.count_users() == 0:
            app.state.setup_token = auth.new_setup_token()
            msg = (f"\n{'=' * 64}\nBookSmith 首次启动：还没有任何账号。\n"
                   f"一次性 setup token：{app.state.setup_token}\n"
                   f"打开 {BASE}/setup 输入它，设置管理员账号密码。\n{'=' * 64}")
            log.warning(msg)
        else:
            app.state.setup_token = None
        if settings.start_worker:
            worker.start()
        yield
        worker.stop()

    app = FastAPI(title="BookSmith", lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.state.settings = settings
    app.state.db = db
    app.state.repo = repo
    app.state.worker = worker
    app.state.limiter = limiter
    app.state.setup_token = None
    app.add_middleware(PrefixMiddleware, base_path=BASE)
    app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")

    templates = Jinja2Templates(directory=APP_DIR / "templates")
    env = templates.env
    env.globals.update(base=BASE, STATUS_LABELS=STATUS_LABELS, KIND_LABELS=KIND_LABELS,
                       KIND_HINTS=KIND_HINTS, ROLE_LABELS=ROLE_LABELS, KINDS=KINDS,
                       SLIDE_STYLES=SLIDE_STYLES)
    env.filters["markdown"] = render_markdown
    env.filters["ts"] = lambda s: (s or "")[:16].replace("T", " ")
    env.filters["filesize"] = _filesize

    # ------------------------------------------------------------- helpers
    def url(path: str = "/") -> str:
        return BASE + path

    def cookie_path() -> str:
        return BASE or "/"

    def is_https(request: Request) -> bool:
        return (request.url.scheme == "https"
                or request.headers.get("x-forwarded-proto", "").lower() == "https")

    def set_cookie(resp: Response, request: Request, key: str, value: str, max_age: int | None = None):
        resp.set_cookie(key, value, max_age=max_age, path=cookie_path(), httponly=True,
                        samesite="lax", secure=is_https(request))

    def current_user(request: Request) -> dict | None:
        if hasattr(request.state, "user"):
            return request.state.user
        user = None
        payload = request.app.state.signer.unsign(request.cookies.get(auth.SESSION_COOKIE))
        if payload and isinstance(payload.get("uid"), int):
            user = db.get_user(payload["uid"])
        request.state.user = user
        return user

    def require_user(request: Request) -> dict:
        user = current_user(request)
        if user is None:
            raise LoginRequired()
        return user

    def require_admin(request: Request) -> dict:
        user = require_user(request)
        if user["role"] != "admin":
            raise HTTPException(403, "只有管理员可以进行此操作。")
        return user

    def require_api_user(request: Request) -> dict:
        user = current_user(request)
        if user is None:
            raise HTTPException(401, "请先登录。")
        return user

    def can_view(user: dict, task: dict) -> bool:
        return user["role"] == "admin" or task["user_id"] == user["id"]

    def load_task(user: dict, task_id: int) -> dict:
        task = db.get_task(task_id)
        if task is None or not can_view(user, task):
            raise HTTPException(404, "任务不存在，或你没有权限查看。")
        return task

    def redirect(path: str, flash: str | None = None, request: Request | None = None,
                 level: str = "ok") -> RedirectResponse:
        resp = RedirectResponse(url(path), status_code=303)
        if flash and request is not None:
            set_cookie(resp, request, auth.FLASH_COOKIE, quote(f"{level}|{flash}"), max_age=60)
        return resp

    def render(request: Request, name: str, ctx: dict | None = None, status_code: int = 200) -> HTMLResponse:
        csrf = request.cookies.get(auth.CSRF_COOKIE)
        new_csrf = None
        if not csrf:
            csrf = new_csrf = auth.new_csrf_token()
        flash = None
        raw_flash = request.cookies.get(auth.FLASH_COOKIE)
        if raw_flash:
            level, _, text = unquote(raw_flash).partition("|")
            flash = {"level": level if level in ("ok", "error", "warn") else "ok", "text": text}
        context = {"user": current_user(request), "csrf": csrf, "flash": flash,
                   "path": request.url.path}
        context.update(ctx or {})
        resp = templates.TemplateResponse(request, name, context, status_code=status_code)
        if new_csrf:
            set_cookie(resp, request, auth.CSRF_COOKIE, new_csrf, max_age=30 * 24 * 3600)
        if raw_flash:
            resp.delete_cookie(auth.FLASH_COOKIE, path=cookie_path())
        resp.headers["Cache-Control"] = "no-store"
        return resp

    async def read_form(request: Request, max_bytes: int | None = None) -> FormData:
        limit = max_bytes or 1024 * 1024
        try:
            clen = int(request.headers.get("content-length") or 0)
        except ValueError:
            clen = 0
        if clen > limit + 1024 * 1024:
            raise HTTPException(413, f"上传内容太大（上限 {_filesize(limit)}）。")
        form = await request.form(max_files=20, max_fields=200)
        submitted = form.get("csrf") or request.headers.get("x-csrf-token")
        if not auth.csrf_ok(request.cookies.get(auth.CSRF_COOKIE), submitted):
            raise HTTPException(403, "安全校验（CSRF）失败，请刷新页面后重试。")
        return form

    def client_ip(request: Request) -> str:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()
        return request.headers.get("x-real-ip") or (request.client.host if request.client else "?")

    def login_response(request: Request, user_id: int, next_path: str) -> RedirectResponse:
        resp = RedirectResponse(next_path, status_code=303)
        set_cookie(resp, request, auth.SESSION_COOKIE,
                   auth.new_session_token(request.app.state.signer, user_id),
                   max_age=auth.SESSION_MAX_AGE)
        set_cookie(resp, request, auth.CSRF_COOKIE, auth.new_csrf_token(), max_age=30 * 24 * 3600)
        return resp

    def safe_next(n: str | None) -> str:
        default = url("/tasks")
        if not n or not n.startswith((BASE or "") + "/") or n.startswith("//") or "\\" in n:
            return default
        return n

    # --------------------------------------------------------- error pages
    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired):
        target = BASE + request.url.path
        if request.url.query:
            target += "?" + request.url.query
        return RedirectResponse(url("/login") + "?next=" + quote(target, safe=""), status_code=303)

    @app.exception_handler(HTTPException)
    async def _http_error(request: Request, exc: HTTPException):
        if request.url.path.startswith("/api/") or "text/html" not in request.headers.get("accept", "text/html"):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        return render(request, "error.html", {"code": exc.status_code, "message": exc.detail},
                      status_code=exc.status_code)

    @app.exception_handler(404)
    async def _not_found(request: Request, exc):
        detail = getattr(exc, "detail", None)
        if not detail or detail == "Not Found":
            detail = "页面不存在。"
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": detail}, status_code=404)
        return render(request, "error.html", {"code": 404, "message": detail}, status_code=404)

    # ------------------------------------------------------------- basics
    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "worker_task": worker.current_task_id}

    @app.get("/")
    async def index(request: Request):
        if db.count_users() == 0:
            return RedirectResponse(url("/setup"), status_code=303)
        if current_user(request):
            return RedirectResponse(url("/tasks"), status_code=303)
        return RedirectResponse(url("/login"), status_code=303)

    # --------------------------------------------------------------- setup
    @app.get("/setup")
    async def setup_page(request: Request):
        if db.count_users() > 0:
            return RedirectResponse(url("/login"), status_code=303)
        return render(request, "setup.html", {})

    @app.post("/setup")
    async def setup_submit(request: Request):
        form = await read_form(request)
        if db.count_users() > 0 or not request.app.state.setup_token:
            return redirect("/login", "系统已经初始化过了。", request, "warn")
        ip = client_ip(request)
        wait = limiter.retry_after("setup:" + ip)
        if wait:
            return render(request, "setup.html", {"error": f"尝试次数过多，请 {wait} 秒后再试。"}, 429)
        token = str(form.get("token", "")).strip()
        username = str(form.get("username", "")).strip()
        password = str(form.get("password", ""))
        password2 = str(form.get("password2", ""))
        if not secrets.compare_digest(token, request.app.state.setup_token):
            limiter.fail("setup:" + ip)
            return render(request, "setup.html", {"error": "setup token 不正确（看服务启动日志）。",
                                                  "username": username}, 400)
        err = auth.validate_new_account(username, password, password2)
        if err:
            return render(request, "setup.html", {"error": err, "username": username}, 400)
        uid = db.create_user(username, auth.hash_password(password), "admin", settings.default_quota)
        request.app.state.setup_token = None
        log.info("管理员账号已创建：%s", username)
        resp = login_response(request, uid, url("/tasks"))
        set_cookie(resp, request, auth.FLASH_COOKIE, quote("ok|管理员账号已创建。下一步：在「管理」里生成邀请码。"), 60)
        return resp

    # --------------------------------------------------------------- login
    @app.get("/login")
    async def login_page(request: Request, next: str | None = None):
        if db.count_users() == 0:
            return RedirectResponse(url("/setup"), status_code=303)
        if current_user(request):
            return RedirectResponse(safe_next(next), status_code=303)
        return render(request, "login.html", {"next": next or ""})

    @app.post("/login")
    async def login_submit(request: Request):
        form = await read_form(request)
        username = str(form.get("username", "")).strip()
        password = str(form.get("password", ""))
        next_path = safe_next(str(form.get("next", "")))
        keys = ("ip:" + client_ip(request), "user:" + username.lower())
        wait = limiter.retry_after(*keys)
        if wait:
            return render(request, "login.html", {"error": f"登录失败次数过多，请 {wait} 秒后再试。",
                                                  "username": username, "next": next_path}, 429)
        user = db.get_user_by_name(username) if username else None
        ok = auth.verify_password(password, user["pw_hash"] if user else auth._DUMMY_HASH)
        if not user or not ok:
            limiter.fail(*keys)
            return render(request, "login.html", {"error": "用户名或密码错误。", "username": username,
                                                  "next": next_path}, 400)
        limiter.reset(*keys)
        return login_response(request, user["id"], next_path)

    @app.post("/logout")
    async def logout(request: Request):
        await read_form(request)
        resp = RedirectResponse(url("/login"), status_code=303)
        resp.delete_cookie(auth.SESSION_COOKIE, path=cookie_path())
        return resp

    # ------------------------------------------------------------ register
    @app.get("/register")
    async def register_page(request: Request, code: str = ""):
        return render(request, "register.html", {"code": code})

    @app.post("/register")
    async def register_submit(request: Request):
        form = await read_form(request)
        code = str(form.get("code", "")).strip().upper()
        username = str(form.get("username", "")).strip()
        password = str(form.get("password", ""))
        password2 = str(form.get("password2", ""))
        ctx = {"code": code, "username": username}
        ip = client_ip(request)
        wait = limiter.retry_after("invite:" + ip)
        if wait:
            return render(request, "register.html", {**ctx, "error": f"尝试次数过多，请 {wait} 秒后再试。"}, 429)
        err = auth.validate_new_account(username, password, password2)
        if err:
            return render(request, "register.html", {**ctx, "error": err}, 400)
        if db.get_user_by_name(username):
            return render(request, "register.html", {**ctx, "error": "这个用户名已被占用。"}, 400)
        uid = db.register_with_invite(code, username, auth.hash_password(password), settings.default_quota)
        if uid is None:
            limiter.fail("invite:" + ip)
            return render(request, "register.html", {**ctx, "error": "邀请码无效、已被使用或已过期。"}, 400)
        resp = login_response(request, uid, url("/tasks"))
        set_cookie(resp, request, auth.FLASH_COOKIE, quote("ok|注册成功，欢迎使用 BookSmith。"), 60)
        return resp

    # --------------------------------------------------------------- tasks
    @app.get("/tasks")
    async def task_list(request: Request, status: str = "", scope: str = ""):
        user = require_user(request)
        show_all = user["role"] == "admin" and scope == "all"
        st = status if status in STATUS_LABELS else ""
        tasks = db.list_tasks(user_id=None if show_all else user["id"], status=st or None)
        used = db.tasks_created_today(user["id"])
        return render(request, "tasks.html", {
            "tasks": tasks, "status": st, "scope": "all" if show_all else "",
            "used_today": used, "quota": user["daily_quota"],
        })

    @app.get("/tasks/new")
    async def new_task_page(request: Request, kind: str = "maintain"):
        user = require_user(request)
        used = db.tasks_created_today(user["id"])
        return render(request, "new_task.html", {
            "form": {"kind": kind if kind in KINDS else "maintain", "count": 10},
            "used_today": used, "quota": user["daily_quota"],
            "max_upload": _filesize(settings.max_upload_bytes),
        })

    @app.post("/tasks/new")
    async def new_task_submit(request: Request):
        user = require_user(request)
        form = await read_form(request, settings.max_upload_bytes)
        values = {k: str(form.get(k, "")).strip() for k in ("kind", "title", "request", "chapter", "count", "style")}
        used = db.tasks_created_today(user["id"])
        page_ctx = {"form": values, "used_today": used, "quota": user["daily_quota"],
                    "max_upload": _filesize(settings.max_upload_bytes)}

        def fail(msg: str, code: int = 400):
            return render(request, "new_task.html", {**page_ctx, "error": msg}, code)

        kind = values["kind"]
        if kind not in KINDS:
            return fail("请选择一种能力。")
        title = values["title"]
        if not (1 <= len(title) <= 100):
            return fail("标题必填，最多 100 字。")
        req_text = values["request"]
        if len(req_text) > 20000:
            return fail("需求描述太长（最多 20000 字）。")
        if not req_text:
            return fail("请写需求描述：想改什么、改成什么样。")
        params: dict = {}
        if values["chapter"]:
            try:
                ch = int(values["chapter"])
                assert 0 <= ch <= 99
                params["chapter"] = ch
            except (ValueError, AssertionError):
                return fail("章节号应为 0–99 的整数。")
        elif kind in ("exercises", "slides"):
            return fail("出习题和课件需要填写章节号。")
        if kind == "exercises":
            try:
                cnt = int(values["count"] or 10)
                assert 1 <= cnt <= 50
                params["count"] = cnt
            except (ValueError, AssertionError):
                return fail("题目数量应为 1–50。")
        if kind == "slides":
            style = values["style"] or SLIDE_STYLES[0]
            if style not in SLIDE_STYLES:
                return fail("课件风格无效。")
            params["style"] = style
        if used >= user["daily_quota"]:
            return fail(f"今天的任务额度已用完（{used}/{user['daily_quota']}），明天再来或联系管理员。", 429)

        uploads = [f for f in form.getlist("attachments") if isinstance(f, UploadFile) and f.filename]
        total = 0
        for f in uploads:
            ext = Path(f.filename).suffix.lower()
            if ext not in ALLOWED_UPLOAD_EXT:
                return fail(f"不支持的附件类型：{f.filename}（允许 {' '.join(sorted(ALLOWED_UPLOAD_EXT))}）")
            total += f.size or 0
        if total > settings.max_upload_bytes:
            return fail(f"附件总大小超过 {_filesize(settings.max_upload_bytes)}。", 413)
        if uploads:
            up_dir = settings.data_dir / "uploads" / secrets.token_hex(8)
            up_dir.mkdir(parents=True, exist_ok=True)
            atts, used_names = [], set()
            for f in uploads:
                name = _safe_filename(f.filename, used_names)
                dest = up_dir / name
                with dest.open("wb") as out:
                    while chunk := await f.read(1024 * 1024):
                        out.write(chunk)
                atts.append({"name": name, "path": str(dest.resolve()), "size": dest.stat().st_size})
            params["attachments"] = atts

        tid = db.create_task(user_id=user["id"], kind=kind, title=title, request=req_text, params=params)
        db.add_event(tid, "提交", f"{user['username']} 提交了任务")
        worker.wake()
        return redirect(f"/tasks/{tid}", "任务已提交，正在排队。", request)

    @app.get("/tasks/{task_id}")
    async def task_detail(request: Request, task_id: int):
        user = require_user(request)
        task = load_task(user, task_id)
        artifacts = db.list_artifacts(task_id)
        diff_art = next((a for a in artifacts if a["kind"] == "diff"), None)
        parsed = None
        if diff_art and Path(diff_art["path"]).is_file():
            parsed = parse_diff(Path(diff_art["path"]).read_text(encoding="utf-8", errors="replace"))
        pdfs = [a for a in artifacts if a["kind"] == "pdf"]
        events = db.events_after(task_id, 0, limit=2000)
        parent = db.get_task(task["parent_id"]) if task.get("parent_id") else None
        children = db.child_tasks(task_id)
        merge_info = next((e["message"] for e in reversed(events) if e["step"] == "合并"), None)
        return render(request, "task_detail.html", {
            "task": task, "artifacts": artifacts, "diff": parsed, "diff_art": diff_art,
            "pdfs": pdfs, "events": events, "parent": parent, "children": children,
            "last_event_id": events[-1]["id"] if events else 0,
            "queue_pos": db.queue_position(task_id) if task["status"] == "queued" else None,
            "is_owner": task["user_id"] == user["id"],
            "can_review": user["role"] == "admin" and task["status"] == "awaiting_review",
            "can_reject": task["status"] == "awaiting_review",
            "can_cancel": task["status"] in CANCELLABLE,
            "merge_info": merge_info,
            "attachments": task["params"].get("attachments", []),
        })

    @app.post("/tasks/{task_id}/cancel")
    async def task_cancel(request: Request, task_id: int):
        user = require_user(request)
        await read_form(request)
        task = load_task(user, task_id)
        prev = task["status"]
        if prev not in CANCELLABLE or not db.update_task(task_id, expect_status=CANCELLABLE, status="cancelled"):
            return redirect(f"/tasks/{task_id}", "当前状态不能取消。", request, "error")
        db.add_event(task_id, "取消", f"{user['username']} 取消了任务", "warn")
        if prev != "running":  # 运行中的由 worker 收尾
            repo.remove_worktree(task_id)
        return redirect(f"/tasks/{task_id}", "任务已取消。", request)

    @app.get("/tasks/{task_id}/outline")
    async def outline_page(request: Request, task_id: int):
        user = require_user(request)
        task = load_task(user, task_id)
        if task["status"] != "awaiting_outline":
            return redirect(f"/tasks/{task_id}")
        return render(request, "outline.html", {"task": task,
                                                "outline": task.get("outline") or task.get("summary") or ""})

    @app.post("/tasks/{task_id}/outline")
    async def outline_submit(request: Request, task_id: int):
        user = require_user(request)
        form = await read_form(request)
        task = load_task(user, task_id)
        outline = str(form.get("outline", "")).strip()
        if not outline or len(outline) > 20000:
            return render(request, "outline.html", {"task": task, "outline": outline,
                                                    "error": "大纲不能为空，最多 20000 字。"}, 400)
        if not db.update_task(task_id, expect_status="awaiting_outline", outline=outline, status="queued"):
            return redirect(f"/tasks/{task_id}", "任务不在待确认大纲状态。", request, "error")
        db.add_event(task_id, "大纲", f"{user['username']} 确认了大纲，重新排队写正文")
        worker.wake()
        return redirect(f"/tasks/{task_id}", "大纲已确认，任务重新排队。", request)

    @app.post("/tasks/{task_id}/approve")
    async def task_approve(request: Request, task_id: int):
        user = require_admin(request)
        await read_form(request)
        task = load_task(user, task_id)
        if task["status"] != "awaiting_review" or not task.get("branch"):
            return redirect(f"/tasks/{task_id}", "只有待审核的任务可以通过。", request, "error")
        msg = (f"BookSmith: 合并任务 #{task_id} {task['title']}\n\n"
               f"类型：{KIND_LABELS.get(task['kind'], task['kind'])}；提交人：{task.get('username')}；"
               f"审核通过：{user['username']}")
        try:
            sha = await asyncio.to_thread(repo.merge, task["branch"], msg)
        except RepoError as e:
            db.add_event(task_id, "合并", f"合并失败：{e}", "error")
            return redirect(f"/tasks/{task_id}", f"合并失败：{e}", request, "error")
        db.update_task(task_id, expect_status="awaiting_review", status="merged")
        db.add_event(task_id, "合并", f"{user['username']} 审核通过，已合并进 {settings.book_base_branch}：{sha[:12]}")
        note = "已合并进 master。"
        if settings.book_git_remote:
            try:
                await asyncio.to_thread(repo.push)
                db.add_event(task_id, "推送", f"已推送到 {settings.book_git_remote}")
                note += " 已推送到远程。"
            except RepoError as e:
                db.add_event(task_id, "推送", str(e), "error")
                note += f" 但推送失败：{e}"
        repo.remove_worktree(task_id)
        repo.delete_branch(task["branch"])
        return redirect(f"/tasks/{task_id}", note, request, "ok" if "失败" not in note else "warn")

    @app.post("/tasks/{task_id}/reject")
    async def task_reject(request: Request, task_id: int):
        user = require_user(request)
        form = await read_form(request)
        task = load_task(user, task_id)
        feedback = str(form.get("feedback", "")).strip()
        if task["status"] != "awaiting_review":
            return redirect(f"/tasks/{task_id}", "只有待审核的任务可以打回。", request, "error")
        if not feedback or len(feedback) > 20000:
            return redirect(f"/tasks/{task_id}", "请填写修改意见（最多 20000 字）。", request, "error")
        if user["role"] != "admin":
            used = db.tasks_created_today(user["id"])
            if used >= user["daily_quota"]:
                return redirect(f"/tasks/{task_id}", "今天的任务额度已用完，不能再提意见重做。", request, "error")
        if not db.update_task(task_id, expect_status="awaiting_review", status="rejected", feedback=feedback):
            return redirect(f"/tasks/{task_id}", "任务状态已变化，请刷新。", request, "error")
        child = db.create_task(user_id=task["user_id"], kind=task["kind"], title=task["title"],
                               request=task["request"], params=task["params"], parent_id=task_id,
                               branch=task["branch"], outline=task.get("outline"), feedback=feedback)
        db.add_event(task_id, "打回", f"{user['username']} 打回，意见已转为子任务 #{child}", "warn")
        db.add_event(child, "提交", f"由任务 #{task_id} 打回生成，在同一分支 {task['branch']} 上继续修改")
        repo.remove_worktree(task_id)
        worker.wake()
        return redirect(f"/tasks/{child}", "已打回，修改意见已作为新任务排队。", request)

    # ---------------------------------------------------------- artifacts
    def _send_file(path: Path, name: str, download: bool) -> FileResponse:
        media = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if name.endswith(".diff"):
            media = "text/plain; charset=utf-8"
        return FileResponse(path, media_type=media, filename=name,
                            content_disposition_type="attachment" if download else "inline",
                            headers={"Cache-Control": "private, no-store"})

    @app.get("/tasks/{task_id}/artifacts/{artifact_id}")
    async def artifact_get(request: Request, task_id: int, artifact_id: int, download: int = 0):
        user = require_api_user(request)
        load_task(user, task_id)
        art = db.get_artifact(artifact_id)
        if art is None or art["task_id"] != task_id:
            raise HTTPException(404, "产物不存在。")
        path = Path(art["path"]).resolve()
        allowed_root = (settings.tasks_dir / str(task_id)).resolve()
        if allowed_root not in path.parents or not path.is_file():
            raise HTTPException(404, "产物文件不存在。")
        return _send_file(path, art["name"], bool(download))

    @app.get("/tasks/{task_id}/inputs/{index}")
    async def input_get(request: Request, task_id: int, index: int):
        user = require_api_user(request)
        task = load_task(user, task_id)
        atts = task["params"].get("attachments", [])
        if not (0 <= index < len(atts)):
            raise HTTPException(404, "附件不存在。")
        path = Path(atts[index]["path"]).resolve()
        if (settings.data_dir / "uploads").resolve() not in path.parents or not path.is_file():
            raise HTTPException(404, "附件文件不存在。")
        return _send_file(path, atts[index]["name"], True)

    # ---------------------------------------------------------------- API
    @app.get("/api/tasks/{task_id}")
    async def api_task(request: Request, task_id: int):
        user = require_api_user(request)
        task = load_task(user, task_id)
        return {
            "id": task["id"], "kind": task["kind"], "title": task["title"], "status": task["status"],
            "status_label": STATUS_LABELS.get(task["status"]), "branch": task["branch"],
            "parent_id": task["parent_id"], "summary": task["summary"],
            "artifacts": [{"id": a["id"], "name": a["name"], "kind": a["kind"],
                           "url": url(f"/tasks/{task_id}/artifacts/{a['id']}")}
                          for a in db.list_artifacts(task_id)],
            "children": [c["id"] for c in db.child_tasks(task_id)],
        }

    @app.get("/api/tasks/{task_id}/events")
    async def api_events(request: Request, task_id: int, after: int = 0):
        user = require_api_user(request)
        load_task(user, task_id)
        try:
            last = int(request.headers.get("last-event-id") or after or 0)
        except ValueError:
            last = 0

        async def stream():
            nonlocal last
            yield "retry: 3000\n\n"
            last_status = None
            ticks = 0
            while True:
                if await request.is_disconnected():
                    break
                evs = db.events_after(task_id, last)
                for e in evs:
                    last = e["id"]
                    payload = {"id": e["id"], "ts": e["ts"], "step": e["step"],
                               "level": e["level"], "message": e["message"]}
                    yield f"id: {e['id']}\nevent: log\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                status = db.get_status(task_id)
                if status != last_status:
                    last_status = status
                    info = {"status": status, "label": STATUS_LABELS.get(status, status)}
                    if status == "queued":
                        info["queue_pos"] = db.queue_position(task_id)
                    yield f"event: status\ndata: {json.dumps(info, ensure_ascii=False)}\n\n"
                if status not in ACTIVE_STATUSES and not evs:
                    yield f"event: done\ndata: {json.dumps({'status': status})}\n\n"
                    break
                ticks += 1
                if ticks % 15 == 0:
                    yield ": ping\n\n"
                await asyncio.sleep(1)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"})

    # --------------------------------------------------------------- admin
    @app.get("/admin")
    async def admin_page(request: Request):
        require_admin(request)
        invites = db.list_invites()
        nowdt = datetime.now(TZ)
        for inv in invites:
            exp = parse_ts(inv["expires_at"])
            inv["state"] = ("used" if inv["used_by"] else
                            "expired" if exp and exp < nowdt else "active")
        return render(request, "admin.html", {"users": db.list_users(), "invites": invites,
                                              "new_code": request.query_params.get("new")})

    @app.post("/admin/users/{user_id}")
    async def admin_update_user(request: Request, user_id: int):
        admin = require_admin(request)
        form = await read_form(request)
        target = db.get_user(user_id)
        if target is None:
            raise HTTPException(404, "用户不存在。")
        role = str(form.get("role", target["role"]))
        if role not in ROLE_LABELS:
            return redirect("/admin", "角色无效。", request, "error")
        try:
            quota = int(str(form.get("daily_quota", target["daily_quota"])))
            assert 0 <= quota <= 1000
        except (ValueError, AssertionError):
            return redirect("/admin", "额度应为 0–1000 的整数。", request, "error")
        if target["role"] == "admin" and role != "admin" and db.count_admins() <= 1:
            return redirect("/admin", "至少要保留一个管理员。", request, "error")
        db.update_user(user_id, role=role, daily_quota=quota)
        log.info("%s 修改了用户 %s：role=%s quota=%s", admin["username"], target["username"], role, quota)
        return redirect("/admin", f"已更新 {target['username']}。", request)

    @app.post("/admin/invites")
    async def admin_create_invite(request: Request):
        admin = require_admin(request)
        form = await read_form(request)
        role = str(form.get("role", "member"))
        if role not in ROLE_LABELS:
            role = "member"
        try:
            days = int(str(form.get("days", "7")))
        except ValueError:
            days = 7
        expires = None
        if days > 0:
            expires = (datetime.now(TZ) + timedelta(days=min(days, 365))).isoformat(timespec="seconds")
        code = auth.new_invite_code()
        db.create_invite(code, role, admin["id"], expires)
        return redirect(f"/admin?new={quote(code)}#invites", f"邀请码已生成：{code}", request)

    @app.post("/admin/invites/{code}/delete")
    async def admin_delete_invite(request: Request, code: str):
        require_admin(request)
        await read_form(request)
        ok = db.delete_invite(code)
        return redirect("/admin#invites", "邀请码已作废。" if ok else "邀请码不存在或已被使用。",
                        request, "ok" if ok else "error")

    return app


def _filesize(n) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def _safe_filename(name: str, used: set) -> str:
    base = Path(name.replace("\\", "/")).name
    base = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "_", base).strip().lstrip(".") or "file"
    stem, ext = Path(base).stem[:80], Path(base).suffix[:10]
    candidate, i = f"{stem}{ext}", 1
    while candidate.lower() in used:
        i += 1
        candidate = f"{stem}-{i}{ext}"
    used.add(candidate.lower())
    return candidate


def _lazy_app():
    """uvicorn app.main:app 时才读环境变量、建应用。"""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return create_app()


def __getattr__(name):  # PEP 562：`from app.main import app` / `uvicorn app.main:app`
    if name == "app":
        a = _lazy_app()
        globals()["app"] = a
        return a
    raise AttributeError(name)
