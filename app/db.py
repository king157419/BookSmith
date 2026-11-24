"""SQLite 存储（标准库 sqlite3）。表结构见 docs/04-架构与接口.md「任务模型」。

每次操作开一个短连接（WAL 模式），worker 线程和请求线程互不干扰。
时间统一存北京时间 ISO 字符串（UTC+8），每日额度也按北京时间算。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

TZ = timezone(timedelta(hours=8))

KINDS = ("maintain", "exercises", "slides", "newchapter")
STATUSES = (
    "queued", "running", "awaiting_outline", "awaiting_review",
    "merged", "rejected", "failed", "cancelled",
)
ACTIVE_STATUSES = ("queued", "running")
FINAL_STATUSES = ("merged", "rejected", "failed", "cancelled")
ARTIFACT_KINDS = ("pdf", "pptx", "diff", "md", "log", "png")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    pw_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('admin','member')),
    daily_quota INTEGER NOT NULL DEFAULT 10,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS invites(
    code TEXT PRIMARY KEY,
    role TEXT NOT NULL CHECK(role IN ('admin','member')),
    created_by INTEGER REFERENCES users(id),
    used_by INTEGER REFERENCES users(id),
    expires_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    request TEXT NOT NULL DEFAULT '',
    params_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'queued',
    branch TEXT,
    parent_id INTEGER REFERENCES tasks(id),
    outline TEXT,
    feedback TEXT,
    summary TEXT,
    cost_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_user ON tasks(user_id, created_at);
CREATE TABLE IF NOT EXISTS task_events(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    ts TEXT NOT NULL,
    step TEXT NOT NULL DEFAULT '',
    level TEXT NOT NULL DEFAULT 'info',
    message TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_events_task ON task_events(task_id, id);
CREATE TABLE IF NOT EXISTS artifacts(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    name TEXT NOT NULL,
    path TEXT NOT NULL,
    kind TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_artifacts_task ON artifacts(task_id);
"""


def now() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def today_start() -> str:
    d = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    return d.isoformat(timespec="seconds")


def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt


def task_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    try:
        d["params"] = json.loads(d.get("params_json") or "{}")
    except json.JSONDecodeError:
        d["params"] = {}
    return d


class DB:
    def __init__(self, path: Path | str):
        self.path = str(path)

    @contextmanager
    def conn(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        try:
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def init(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(SCHEMA)

    # ---------------- users ----------------
    def count_users(self) -> int:
        with self.conn() as c:
            return c.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def count_admins(self) -> int:
        with self.conn() as c:
            return c.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0]

    def create_user(self, username: str, pw_hash: str, role: str, daily_quota: int = 10) -> int:
        with self.conn() as c:
            cur = c.execute(
                "INSERT INTO users(username,pw_hash,role,daily_quota,created_at) VALUES(?,?,?,?,?)",
                (username, pw_hash, role, daily_quota, now()),
            )
            return int(cur.lastrowid)

    def get_user(self, user_id: int) -> dict | None:
        with self.conn() as c:
            r = c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            return dict(r) if r else None

    def get_user_by_name(self, username: str) -> dict | None:
        with self.conn() as c:
            r = c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
            return dict(r) if r else None

    def list_users(self) -> list[dict]:
        today = today_start()
        with self.conn() as c:
            rows = c.execute(
                """SELECT u.*,
                   (SELECT COUNT(*) FROM tasks t WHERE t.user_id=u.id AND t.created_at>=?) AS used_today,
                   (SELECT COUNT(*) FROM tasks t WHERE t.user_id=u.id) AS total_tasks
                   FROM users u ORDER BY u.id""",
                (today,),
            ).fetchall()
            return [dict(r) for r in rows]

    def update_user(self, user_id: int, *, role: str | None = None, daily_quota: int | None = None) -> None:
        with self.conn() as c:
            if role is not None:
                c.execute("UPDATE users SET role=? WHERE id=?", (role, user_id))
            if daily_quota is not None:
                c.execute("UPDATE users SET daily_quota=? WHERE id=?", (daily_quota, user_id))

    def tasks_created_today(self, user_id: int) -> int:
        with self.conn() as c:
            return c.execute(
                "SELECT COUNT(*) FROM tasks WHERE user_id=? AND created_at>=?",
                (user_id, today_start()),
            ).fetchone()[0]

    # ---------------- invites ----------------
    def create_invite(self, code: str, role: str, created_by: int, expires_at: str | None) -> None:
        with self.conn() as c:
            c.execute(
                "INSERT INTO invites(code,role,created_by,used_by,expires_at,created_at) VALUES(?,?,?,NULL,?,?)",
                (code, role, created_by, expires_at, now()),
            )

    def get_invite(self, code: str) -> dict | None:
        with self.conn() as c:
            r = c.execute("SELECT * FROM invites WHERE code=?", (code,)).fetchone()
            return dict(r) if r else None

    def list_invites(self) -> list[dict]:
        with self.conn() as c:
            rows = c.execute(
                """SELECT i.*, cu.username AS creator_name, uu.username AS used_by_name
                   FROM invites i
                   LEFT JOIN users cu ON cu.id=i.created_by
                   LEFT JOIN users uu ON uu.id=i.used_by
                   ORDER BY i.created_at DESC"""
            ).fetchall()
            return [dict(r) for r in rows]

    def delete_invite(self, code: str) -> bool:
        with self.conn() as c:
            cur = c.execute("DELETE FROM invites WHERE code=? AND used_by IS NULL", (code,))
            return cur.rowcount > 0

    def register_with_invite(self, code: str, username: str, pw_hash: str, daily_quota: int) -> int | None:
        """原子地消耗邀请码并建用户；邀请码无效/已用/过期返回 None。"""
        with self.conn() as c:
            c.execute("BEGIN IMMEDIATE")
            inv = c.execute("SELECT * FROM invites WHERE code=?", (code,)).fetchone()
            if inv is None or inv["used_by"] is not None:
                return None
            exp = parse_ts(inv["expires_at"])
            if exp is not None and exp < datetime.now(TZ):
                return None
            cur = c.execute(
                "INSERT INTO users(username,pw_hash,role,daily_quota,created_at) VALUES(?,?,?,?,?)",
                (username, pw_hash, inv["role"], daily_quota, now()),
            )
            uid = int(cur.lastrowid)
            c.execute("UPDATE invites SET used_by=? WHERE code=?", (uid, code))
            return uid

    # ---------------- tasks ----------------
    def create_task(self, *, user_id: int, kind: str, title: str, request: str,
                    params: dict | None = None, parent_id: int | None = None,
                    branch: str | None = None, outline: str | None = None,
                    feedback: str | None = None) -> int:
        ts = now()
        with self.conn() as c:
            cur = c.execute(
                """INSERT INTO tasks(user_id,kind,title,request,params_json,status,branch,parent_id,
                   outline,feedback,created_at,updated_at)
                   VALUES(?,?,?,?,?,'queued',?,?,?,?,?,?)""",
                (user_id, kind, title, request, json.dumps(params or {}, ensure_ascii=False),
                 branch, parent_id, outline, feedback, ts, ts),
            )
            return int(cur.lastrowid)

    def get_task(self, task_id: int) -> dict | None:
        with self.conn() as c:
            r = c.execute(
                """SELECT t.*, u.username AS username FROM tasks t
                   LEFT JOIN users u ON u.id=t.user_id WHERE t.id=?""",
                (task_id,),
            ).fetchone()
            return task_to_dict(r)

    def list_tasks(self, *, user_id: int | None = None, status: str | None = None,
                   limit: int = 200) -> list[dict]:
        sql = "SELECT t.*, u.username AS username FROM tasks t LEFT JOIN users u ON u.id=t.user_id WHERE 1=1"
        args: list[Any] = []
        if user_id is not None:
            sql += " AND t.user_id=?"
            args.append(user_id)
        if status:
            sql += " AND t.status=?"
            args.append(status)
        sql += " ORDER BY t.id DESC LIMIT ?"
        args.append(limit)
        with self.conn() as c:
            return [task_to_dict(r) for r in c.execute(sql, args).fetchall()]

    def child_tasks(self, task_id: int) -> list[dict]:
        with self.conn() as c:
            rows = c.execute("SELECT * FROM tasks WHERE parent_id=? ORDER BY id", (task_id,)).fetchall()
            return [task_to_dict(r) for r in rows]

    def queue_position(self, task_id: int) -> int:
        with self.conn() as c:
            return c.execute(
                "SELECT COUNT(*) FROM tasks WHERE status='queued' AND id<=?", (task_id,)
            ).fetchone()[0]

    def update_task(self, task_id: int, *, expect_status: str | tuple[str, ...] | None = None,
                    **fields: Any) -> bool:
        """更新字段；给了 expect_status 时只在当前状态匹配时更新（防止覆盖并发的取消等）。"""
        if "params" in fields:
            fields["params_json"] = json.dumps(fields.pop("params"), ensure_ascii=False)
        fields["updated_at"] = now()
        cols = ", ".join(f"{k}=?" for k in fields)
        args: list[Any] = list(fields.values()) + [task_id]
        sql = f"UPDATE tasks SET {cols} WHERE id=?"
        if expect_status is not None:
            exp = (expect_status,) if isinstance(expect_status, str) else tuple(expect_status)
            sql += f" AND status IN ({','.join('?' * len(exp))})"
            args.extend(exp)
        with self.conn() as c:
            return c.execute(sql, args).rowcount > 0

    def claim_next_queued(self) -> dict | None:
        """取最早的 queued 任务并原子地改成 running。"""
        with self.conn() as c:
            c.execute("BEGIN IMMEDIATE")
            r = c.execute("SELECT id FROM tasks WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
            if r is None:
                return None
            c.execute("UPDATE tasks SET status='running', updated_at=? WHERE id=?", (now(), r["id"]))
        return self.get_task(r["id"])

    def get_status(self, task_id: int) -> str | None:
        with self.conn() as c:
            r = c.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
            return r["status"] if r else None

    def fail_interrupted(self) -> list[int]:
        """启动时把上次没跑完的 running 任务标成失败（进程被杀/重启）。"""
        with self.conn() as c:
            ids = [r[0] for r in c.execute("SELECT id FROM tasks WHERE status='running'").fetchall()]
            for tid in ids:
                c.execute("UPDATE tasks SET status='failed', updated_at=? WHERE id=?", (now(), tid))
                c.execute(
                    "INSERT INTO task_events(task_id,ts,step,level,message) VALUES(?,?,?,?,?)",
                    (tid, now(), "worker", "error", "服务重启，任务被中断，请重新提交。"),
                )
            return ids

    # ---------------- events ----------------
    def add_event(self, task_id: int, step: str, message: str, level: str = "info") -> int:
        with self.conn() as c:
            cur = c.execute(
                "INSERT INTO task_events(task_id,ts,step,level,message) VALUES(?,?,?,?,?)",
                (task_id, now(), step, level, message),
            )
            return int(cur.lastrowid)

    def events_after(self, task_id: int, after_id: int = 0, limit: int = 500) -> list[dict]:
        with self.conn() as c:
            rows = c.execute(
                "SELECT * FROM task_events WHERE task_id=? AND id>? ORDER BY id LIMIT ?",
                (task_id, after_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    # ---------------- artifacts ----------------
    def add_artifact(self, task_id: int, name: str, path: str, kind: str) -> int:
        with self.conn() as c:
            cur = c.execute(
                "INSERT INTO artifacts(task_id,name,path,kind,created_at) VALUES(?,?,?,?,?)",
                (task_id, name, path, kind, now()),
            )
            return int(cur.lastrowid)

    def list_artifacts(self, task_id: int) -> list[dict]:
        with self.conn() as c:
            rows = c.execute("SELECT * FROM artifacts WHERE task_id=? ORDER BY id", (task_id,)).fetchall()
            return [dict(r) for r in rows]

    def get_artifact(self, artifact_id: int) -> dict | None:
        with self.conn() as c:
            r = c.execute("SELECT * FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
            return dict(r) if r else None
