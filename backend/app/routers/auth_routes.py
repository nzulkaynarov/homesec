from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from ..auth import COOKIE, MAX_AGE, check_credentials, make_session_cookie
from ..templates_env import templates

router = APIRouter()


@router.get("/login")
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    if not check_credentials(username, password):
        return templates.TemplateResponse(request, "login.html", {"error": "Неверный логин или пароль"})
    resp = RedirectResponse("/", status_code=302)
    resp.set_cookie(COOKIE, make_session_cookie(), max_age=MAX_AGE, httponly=True, samesite="lax")
    return resp


@router.post("/logout")
async def logout():
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie(COOKIE)
    return resp
