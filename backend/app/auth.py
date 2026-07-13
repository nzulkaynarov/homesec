"""Простая сессионная авторизация для одного администратора из .env.
Подписанная cookie (itsdangerous), время жизни — 12 часов."""

import hmac

from fastapi import Request
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .config import settings

COOKIE = "hs_session"
MAX_AGE = 12 * 3600

_serializer = URLSafeTimedSerializer(settings.secret_key, salt="hs-auth")


def check_credentials(username: str, password: str) -> bool:
    return hmac.compare_digest(username, settings.admin_username) and hmac.compare_digest(
        password, settings.admin_password
    )


def make_session_cookie() -> str:
    return _serializer.dumps({"u": settings.admin_username})


def is_authenticated(request: Request) -> bool:
    token = request.cookies.get(COOKIE)
    if not token:
        return False
    try:
        _serializer.loads(token, max_age=MAX_AGE)
        return True
    except (BadSignature, SignatureExpired):
        return False


PUBLIC_PATHS = ("/login", "/static")


async def auth_middleware(request: Request, call_next):
    """Всё, кроме /login и /static, требует сессии."""
    if not request.url.path.startswith(PUBLIC_PATHS) and not is_authenticated(request):
        return RedirectResponse("/login", status_code=302)
    return await call_next(request)
