"""Password based auth for the admin panel.

Uses a signed (HMAC-SHA256) cookie so no extra dependency is needed.
Configure via .env:
    ADMIN_PASSWORD=your-strong-password
    ADMIN_SESSION_SECRET=long-random-string   # optional, defaults to BOT_TOKEN
    ADMIN_SESSION_HOURS=12                    # optional
"""

import base64
import hashlib
import hmac
import json
import time

from fastapi import HTTPException, Request, status

from app.database.connection import settings

COOKIE_NAME = "admin_session"


def _secret() -> bytes:
    raw = getattr(settings, "ADMIN_SESSION_SECRET", "") or settings.BOT_TOKEN
    return raw.encode()


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _b64d(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _sign(payload: bytes) -> str:
    return _b64e(hmac.new(_secret(), payload, hashlib.sha256).digest())


def password_is_set() -> bool:
    return bool(getattr(settings, "ADMIN_PASSWORD", ""))


def verify_password(candidate: str) -> bool:
    expected = getattr(settings, "ADMIN_PASSWORD", "") or ""
    if not expected:
        return False
    return hmac.compare_digest(
        hashlib.sha256(candidate.encode()).digest(),
        hashlib.sha256(expected.encode()).digest(),
    )


def create_session_token() -> str:
    hours = int(getattr(settings, "ADMIN_SESSION_HOURS", 12) or 12)
    payload = json.dumps({"exp": int(time.time()) + hours * 3600}).encode()
    body = _b64e(payload)
    return f"{body}.{_sign(payload)}"


def session_is_valid(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    body, sig = token.rsplit(".", 1)
    try:
        payload = _b64d(body)
    except Exception:
        return False
    if not hmac.compare_digest(sig, _sign(payload)):
        return False
    try:
        return int(json.loads(payload).get("exp", 0)) > int(time.time())
    except Exception:
        return False


def is_admin(request: Request) -> bool:
    return session_is_valid(request.cookies.get(COOKIE_NAME))


async def require_admin(request: Request) -> bool:
    """FastAPI dependency: 401 for API calls, redirect handled in routes."""
    if not is_admin(request):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Admin login required",
            headers={"Location": "/admin/login"},
        )
    return True


def set_session_cookie(response, token: str) -> None:
    hours = int(getattr(settings, "ADMIN_SESSION_HOURS", 12) or 12)
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=hours * 3600,
        httponly=True,
        samesite="lax",
        secure=str(settings.BASE_URL).startswith("https://"),
        path="/",
    )


def clear_session_cookie(response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")
