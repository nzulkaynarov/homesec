import logging

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Device, EventLog
from ..services import adguard, mikrotik
from ..templates_env import templates

log = logging.getLogger("homesec.dashboard")
router = APIRouter()


@router.get("/")
async def dashboard(request: Request, db: Session = Depends(get_db)):
    devices = list(db.scalars(select(Device)))
    online_ips: set[str] = set()
    router_ok = False
    try:
        with mikrotik.api_session() as api:
            online_ips = mikrotik.get_online_ips(api)
            router_ok = True
    except mikrotik.MikrotikError as e:
        log.warning("%s", e)

    stats, adguard_ok = {}, False
    try:
        stats = adguard.get_stats()
        adguard_ok = True
    except adguard.AdGuardError as e:
        log.warning("%s", e)

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
    return templates.TemplateResponse(request, "events.html", {"active": "events", "events": events})
