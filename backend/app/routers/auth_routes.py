from datetime import datetime, timedelta

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from ..auth import COOKIE, MAX_AGE, check_credentials, make_session_cookie
from ..templates_env import templates

router = APIRouter()

# Простой anti-brute-force: не больше _MAX_FAILS неудачных попыток с одного IP
# за _WINDOW. В памяти процесса (панель — один процесс); при рестарте сбрасывается,
# что для домашней сети приемлемо. Успешный вход обнуляет счётчик IP.
_MAX_FAILS = 8
_WINDOW = timedelta(minutes=15)
_fails: dict[str, list[datetime]] = {}


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def _rate_limited(ip: str, now: datetime) -> bool:
    recent = [t for t in _fails.get(ip, []) if now - t < _WINDOW]
    _fails[ip] = recent
    return len(recent) >= _MAX_FAILS


@router.get("/login")
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    ip = _client_ip(request)
    now = datetime.now()
    if _rate_limited(ip, now):
        ctx = {"error": "Слишком много попыток. Подождите 15 минут."}
        return templates.TemplateResponse(request, "login.html", ctx, status_code=429)
    if not check_credentials(username, password):
        _fails.setdefault(ip, []).append(now)
        ctx = {"error": "Неверный логин или пароль"}
        return templates.TemplateResponse(request, "login.html", ctx, status_code=401)
    _fails.pop(ip, None)  # успех — обнуляем счётчик
    resp = RedirectResponse("/", status_code=302)
    resp.set_cookie(COOKIE, make_session_cookie(), max_age=MAX_AGE, httponly=True,
                    samesite="lax", secure=request.url.scheme == "https")
    return resp


@router.post("/logout")
async def logout():
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie(COOKIE)
    return resp
