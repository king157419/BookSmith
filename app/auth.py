"""账号、密码哈希、签名会话 cookie、CSRF、登录限速、setup token、邀请码。

会话：cookie `bs_session` = base64url(JSON{uid, iat}) + "." + HMAC-SHA256 签名，
HttpOnly + SameSite=Lax，path 为 BASE_PATH，有效期 14 天。服务端无状态。

CSRF：双提交 cookie。每个浏览器有一个随机的 `bs_csrf` cookie（HttpOnly），
页面把同一个值放进表单隐藏字段 `csrf`（或请求头 X-CSRF-Token），
所有改状态的 POST 都要求两者一致（hmac.compare_digest）。登录成功时轮换。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from collections import defaultdict, deque

SESSION_COOKIE = "bs_session"
CSRF_COOKIE = "bs_csrf"
FLASH_COOKIE = "bs_flash"
SESSION_MAX_AGE = 14 * 24 * 3600

USERNAME_RE = re.compile(r"^[\w.\-]{2,32}$", re.UNICODE)
MIN_PASSWORD = 8


# ---------------- 密码 ----------------
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R,
                        p=_SCRYPT_P, maxmem=64 * 1024 * 1024, dklen=32)
    return "scrypt${}${}${}${}${}".format(
        _SCRYPT_N, _SCRYPT_R, _SCRYPT_P,
        base64.b64encode(salt).decode(), base64.b64encode(dk).decode(),
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_b64, dk_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(dk_b64)
        dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=int(n), r=int(r), p=int(p),
                            maxmem=64 * 1024 * 1024, dklen=len(expected))
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False


# 用户不存在时也做一次哈希，避免靠响应时间枚举用户名
_DUMMY_HASH = hash_password("booksmith-dummy-password")


def validate_new_account(username: str, password: str, password2: str) -> str | None:
    if not USERNAME_RE.match(username or ""):
        return "用户名需 2–32 个字符，只能包含字母、数字、汉字、下划线、点或短横线。"
    if len(password or "") < MIN_PASSWORD:
        return f"密码至少 {MIN_PASSWORD} 位。"
    if password != password2:
        return "两次输入的密码不一致。"
    return None


# ---------------- 签名 ----------------
def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class Signer:
    def __init__(self, secret: str):
        self.key = hashlib.sha256(("booksmith-session:" + secret).encode()).digest()

    def sign(self, payload: dict) -> str:
        body = _b64e(json.dumps(payload, separators=(",", ":")).encode())
        sig = _b64e(hmac.new(self.key, body.encode(), hashlib.sha256).digest())
        return f"{body}.{sig}"

    def unsign(self, token: str | None, max_age: int = SESSION_MAX_AGE) -> dict | None:
        if not token or "." not in token:
            return None
        body, sig = token.rsplit(".", 1)
        expected = _b64e(hmac.new(self.key, body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            return None
        try:
            payload = json.loads(_b64d(body))
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        if time.time() - float(payload.get("iat", 0)) > max_age:
            return None
        return payload


def new_session_token(signer: Signer, user_id: int) -> str:
    return signer.sign({"uid": user_id, "iat": int(time.time())})


def new_csrf_token() -> str:
    return secrets.token_urlsafe(24)


def csrf_ok(cookie_value: str | None, submitted: str | None) -> bool:
    if not cookie_value or not submitted:
        return False
    return hmac.compare_digest(cookie_value, submitted)


# ---------------- 登录限速 ----------------
class LoginLimiter:
    """同一用户名或同一 IP 在 window 秒内失败 max_failures 次后，锁到最早一次失败过期为止。"""

    def __init__(self, max_failures: int = 5, window: int = 600):
        self.max_failures = max_failures
        self.window = window
        self._fails: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> deque:
        q = self._fails[key]
        while q and now - q[0] > self.window:
            q.popleft()
        return q

    def retry_after(self, *keys: str) -> int:
        now = time.time()
        wait = 0
        with self._lock:
            for k in keys:
                q = self._prune(k, now)
                if len(q) >= self.max_failures:
                    wait = max(wait, int(self.window - (now - q[0])) + 1)
        return wait

    def fail(self, *keys: str) -> None:
        now = time.time()
        with self._lock:
            for k in keys:
                self._prune(k, now).append(now)

    def reset(self, *keys: str) -> None:
        with self._lock:
            for k in keys:
                self._fails.pop(k, None)


# ---------------- setup token / 邀请码 ----------------
def new_setup_token() -> str:
    return secrets.token_urlsafe(18)


def new_invite_code() -> str:
    # 去掉易混字符，方便老师手抄
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    raw = "".join(secrets.choice(alphabet) for _ in range(12))
    return f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"
