import secrets
import time
from collections import defaultdict, deque

import bcrypt
from fastapi import HTTPException, Request

from app.config import get_settings


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode()[:72], bcrypt.gensalt(get_settings().bcrypt_rounds)).decode()


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode()[:72], hashed.encode())
    except ValueError:
        return False


class RateLimiter:
    """Small in-memory sliding-window limiter (single process; use a proxy/redis if you scale out)."""

    def __init__(self, max_calls: int, per_seconds: int):
        self.max, self.window = max_calls, per_seconds
        self.hits: dict[str, deque] = defaultdict(deque)

    def check(self, key: str) -> None:
        now = time.monotonic()
        q = self.hits[key]
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.max:
            raise HTTPException(429, "Too many requests. Please wait and try again.")
        q.append(now)

    def reset(self) -> None:
        self.hits.clear()


login_limiter = RateLimiter(8, 300)
simulator_limiter = RateLimiter(120, 60)


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "unknown")


def csrf_token(request: Request) -> str:
    tok = request.session.get("csrf")
    if not tok:
        tok = secrets.token_urlsafe(24)
        request.session["csrf"] = tok
    return tok


async def verify_csrf(request: Request) -> None:
    form = await request.form()
    sent = str(form.get("csrf_token", ""))
    expected = request.session.get("csrf", "")
    if not expected or not secrets.compare_digest(sent, expected):
        raise HTTPException(403, "Invalid or missing CSRF token. Reload the page and try again.")
