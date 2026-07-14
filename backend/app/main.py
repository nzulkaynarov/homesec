import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from . import scheduler
from .auth import auth_middleware
from .config import settings
from .db import Base, engine, session
from .migrations import run_migrations
from .models import GROUP_ADDRESS_LISTS, GroupPolicy
from .routers import auth_routes, dashboard, devices, people, rules

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


def _seed_policies() -> None:
    db = session()
    try:
        for group in GROUP_ADDRESS_LISTS:  # kid, guest, unknown
            if not db.scalar(select(GroupPolicy).where(GroupPolicy.group == group)):
                db.add(GroupPolicy(group=group))
        db.commit()
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(engine)
    run_migrations(engine)
    _seed_policies()
    if settings.scheduler_enabled:
        scheduler.start()
    yield
    scheduler.stop()


app = FastAPI(title="HomeSec", lifespan=lifespan)
app.middleware("http")(auth_middleware)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

app.include_router(auth_routes.router)
app.include_router(dashboard.router)
app.include_router(devices.router)
app.include_router(people.router)
app.include_router(rules.router)
