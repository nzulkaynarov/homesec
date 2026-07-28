import logging
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Device, EventLog, Person, active_pauses
from ..services import adguard, mikrotik, quota
from ..templates_env import templates

log = logging.getLogger("homesec.dashboard")
router = APIRouter()

# Порядок секций консоли: дети — сверху (главные для родителя), потом взрослые,
# гости и устройства без владельца.
_ROLE_ORDER = {"kid": 0, "adult": 1, "guest": 2}


def _pause_map(db: Session, devices: list[Device]) -> dict[int, dict[str, datetime]]:
    """{device_id: {"device": до…, "group": до…}} — источник паузы важен:
    кнопка «Снять паузу» на карточке устройства снимает только личную паузу,
    групповую снимают отдельной кнопкой в шапке секции. Раньше источники были
    слиты, и родитель жал кнопку, которая для групповой паузы ничего не
    делала — страница просто перезагружалась без изменений."""
    out: dict[int, dict[str, datetime]] = {}
    for p in active_pauses(db):
        for d in devices:
            if p.target_type == "device" and p.target == str(d.id):
                source = "device"
            elif p.target_type == "group" and p.target == d.group:
                source = "group"
            else:
                continue
            current = out.setdefault(d.id, {})
            if source not in current or p.until > current[source]:
                current[source] = p.until
    return out


def _group_paused(db: Session) -> set[str]:
    return {p.target for p in active_pauses(db) if p.target_type == "group"}


def _console(db: Session, devices: list[Device], online_ips: set[str]) -> list[dict]:
    """Родительская консоль: карточки по людям (дети сверху) + устройства без
    владельца. Каждое устройство — с онлайн-статусом, паузой и полосами
    экранного времени (квоты)."""
    progress = quota.progress(db, devices)
    pauses = _pause_map(db, devices)
    people = list(db.scalars(select(Person)))
    people.sort(key=lambda p: (_ROLE_ORDER.get(p.role, 9), p.name.lower()))

    def card_for(dev: Device) -> dict:
        sources = pauses.get(dev.id, {})
        personal = sources.get("device")
        group_until = sources.get("group")
        return {
            "dev": dev,
            "online": dev.ip in online_ips,
            "paused_until": max(filter(None, (personal, group_until)), default=None),
            # Пауза пришла только от группы — личной кнопкой её не снять.
            "group_pause_only": group_until is not None and personal is None,
            "quota": progress.get(dev.id, []),
        }

    cards: list[dict] = []
    seen: set[int] = set()
    for person in people:
        devs = [d for d in devices if d.person_id == person.id]
        for d in devs:
            seen.add(d.id)
        cards.append({
            "kind": "person",
            "person": person,
            "group": person.role,
            "devices": [card_for(d) for d in devs],
        })
    orphans = [d for d in devices if d.id not in seen]
    if orphans:
        cards.append({
            "kind": "orphans",
            "person": None,
            "group": "unknown",
            "devices": [card_for(d) for d in orphans],
        })
    return cards


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
        "console": _console(db, devices, online_ips),
        "group_paused": _group_paused(db),
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
