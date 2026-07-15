import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from . import scheduler
from .auth import auth_middleware
from .config import settings
from .db import Base, engine, session
from .migrations import ensure_schema
from .models import GROUP_ADDRESS_LISTS, GroupPolicy, Quota
from .routers import auth_routes, dashboard, devices, people, portal, rules

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


def _seed_policies() -> None:
    db = session()
    try:
        for group in GROUP_ADDRESS_LISTS:  # kid, guest, unknown
            if not db.scalar(select(GroupPolicy).where(GroupPolicy.group == group)):
                db.add(GroupPolicy(group=group))
        # Образец квоты (выключен): включается в панели на странице правил
        if db.scalar(select(Quota)) is None:
            db.add(Quota(name="Игры детям (образец)", target_type="group", target="kid",
                         category="games", minutes_per_day=120, enabled=False))
        db.commit()
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_schema(engine, Base.metadata)
    _seed_policies()
    if settings.scheduler_enabled:
        scheduler.start()
    yield
    scheduler.stop()


app = FastAPI(title="HomeSec", lifespan=lifespan)

# Security-заголовки: панель за NAT и только для одного админа, но глубина
# защиты дешёвая. CSP запрещает подгрузку чужих ресурсов и вложение во фрейм
# (кликджекинг); 'unsafe-inline' оставлен из-за инлайновых стилей блок-страницы
# и onsubmit-подтверждений — без него они бы отвалились.
_CSP = ("default-src 'self'; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("Content-Security-Policy", _CSP)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    return resp


app.middleware("http")(auth_middleware)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

app.include_router(auth_routes.router)
app.include_router(portal.router)
app.include_router(dashboard.router)
app.include_router(devices.router)
app.include_router(people.router)
app.include_router(rules.router)
