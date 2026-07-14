import logging

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Device, EventLog
from ..services import adguard, mikrotik
from ..templates_env import templates

log = logging.getLogger("homesec.dashboard")
router = APIRouter()


def _router_snapshot() -> tuple[set[str], bool]:
    """Синхронный I/O к RouterOS — вызывать только через run_in_threadpool,
    иначе таймаут недоступного роутера подвешивает event loop для всех."""
    try:
        with mikrotik.api_session() as api:
            return mikrotik.get_online_ips(api), True
    except mikrotik.MikrotikError as e:
        log.warning("%s", e)
        return set(), False


def _adguard_snapshot() -> tuple[dict, bool]:
    try:
        return adguard.get_stats(), True
    except adguard.AdGuardError as e:
        log.warning("%s", e)
        return {}, False


@router.get("/")
async def dashboard(request: Request, db: Session = Depends(get_db)):
    devices = list(db.scalars(select(Device)))
    online_ips, router_ok = await run_in_threadpool(_router_snapshot)
    stats, adguard_ok = await run_in_threadpool(_adguard_snapshot)

    events = list(db.scalars(select(EventLog).order_by(EventLog.ts.desc()).limit(20)))
    return templates.TemplateResponse(request, "dashboard.html", {
        "active": "dashboard",
        "devices": devices,
        "online_count": sum(1 for d in devices if d.ip in online_ips),
        "blocked_count": sum(1 for d in devices if d.blocked_manual),
        "unknown_count": sum(1 for d in devices if d.group == "unknown"),
        "router_ok": router_ok,
        "adguard_ok": adguard_ok,
        "stats": stats,
        "events": events,
    })


@router.get("/events")
async def events_page(request: Request, db: Session = Depends(get_db)):
    events = list(db.scalars(select(EventLog).order_by(EventLog.ts.desc()).limit(200)))
    ctx = {"active": "events", "events": events}
    return templates.TemplateResponse(request, "events.html", ctx)
