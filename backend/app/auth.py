"""Простая сессионная авторизация для одного администратора из .env.
Подписанная cookie (itsdangerous), время жизни — 12 часов."""

import hmac
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from . import db as dbmod
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


def _is_public(path: str) -> bool:
    return path in ("/login", "/blocked", "/register", "/me", "/me/ask") \
        or path.startswith("/static/")


def _is_intercepted(request: Request) -> bool:
    """Запрос, завёрнутый NAT-перехватом (enable-block-page.rsc): браузер шёл
    на чужой сайт, поэтому Host — не адрес панели. Настоящий IP клиента при
    этом скрыт hairpin-masquerade (правило «hs: block page hairpin»), так что
    определить устройство по IP нельзя — вместо этого редиректим на прямой
    адрес панели: прямое соединение идёт мимо NAT и приходит с настоящим IP."""
    host = (request.headers.get("host") or "").rsplit(":", 1)[0].strip("[]").lower()
    if not host:
        return False
    own = {"localhost", "127.0.0.1", urlsplit(settings.panel_lan_url).hostname or ""}
    from .services.enforcement import get_self_ips  # не тянуть при импорте

    return host not in own and host not in get_self_ips()


async def auth_middleware(request: Request, call_next):
    """Всё, кроме публичных страниц, требует сессии. Неавторизованный запрос
    с ограниченного устройства (NAT-перехват HTTP, см. enable-block-page.rsc)
    уводится на «время вышло», остальные — на /login."""
    if _is_public(request.url.path) or is_authenticated(request):
        return await call_next(request)
    from .services.restrictions import restriction_for_ip  # не тянуть при импорте

    session = dbmod.session()
    try:
        restricted = restriction_for_ip(
            session, request.client.host if request.client else ""
        )
    except Exception:
        restricted = None
    finally:
        session.close()
    if restricted is not None:
        target = "/register" if restricted["kind"] == "unknown" else "/blocked"
        return RedirectResponse(target, status_code=302)
    if _is_intercepted(request):
        # Прямое соединение придёт с настоящим IP — middleware снова разведёт
        # его на /blocked или /register уже с персональной причиной.
        return RedirectResponse(settings.panel_lan_url.rstrip("/") + "/", status_code=302)
    return RedirectResponse("/login", status_code=302)
