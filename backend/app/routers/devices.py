import logging
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import db as dbmod
from ..db import get_db
from ..models import Device, Person, active_pauses, log_event
from ..services import mikrotik, quota
from ..services.enforcement import reconcile
from ..templates_env import templates

log = logging.getLogger("homesec.devices")
router = APIRouter()


def _reconcile_bg() -> None:
    session = dbmod.session()
    try:
        reconcile(session)
    except Exception:
        log.exception("background reconcile failed")
    finally:
        session.close()


def _online_ips() -> set[str]:
    """Синхронный I/O к RouterOS — вызывать через run_in_threadpool."""
    try:
        with mikrotik.api_session() as api:
            return mikrotik.get_online_ips(api)
    except mikrotik.MikrotikError as e:
        log.warning("%s", e)
        return set()


def _pin_lease(mac: str, ip: str, name: str) -> None:
    try:
        with mikrotik.api_session() as api:
            mikrotik.make_lease_static(api, mac, ip, comment=f"hs: {name}")
    except mikrotik.MikrotikError as e:
        log.warning("%s", e)


@router.get("/devices")
async def devices_page(request: Request, db: Session = Depends(get_db)):
    devices = list(db.scalars(select(Device).order_by(Device.first_seen.desc())))
    people = list(db.scalars(select(Person).order_by(Person.name)))
    online_ips = await run_in_threadpool(_online_ips)
    pause_until: dict[int, datetime] = {}
    for p in active_pauses(db):
        for d in devices:
            hit = (p.target_type == "device" and p.target == str(d.id)) or (
                p.target_type == "group" and p.target == d.group
            )
            if hit and (d.id not in pause_until or p.until > pause_until[d.id]):
                pause_until[d.id] = p.until
    return templates.TemplateResponse(request, "devices.html", {
        "active": "devices",
        "devices": devices,
        "people": people,
        "online_ips": online_ips,
        "pause_until": pause_until,
        "quota_progress": quota.progress(db, devices),
    })


@router.post("/devices/scan")
async def scan_devices(tasks: BackgroundTasks):
    tasks.add_task(_reconcile_bg)
    return RedirectResponse("/devices", status_code=302)


@router.post("/devices/{device_id}/update")
async def update_device(
    device_id: int,
    tasks: BackgroundTasks,
    name: str = Form(""),
    person_id: str = Form(""),
    speed_limit: str = Form(""),
    db: Session = Depends(get_db),
):
    dev = db.get(Device, device_id)
    if dev:
        dev.name = name.strip() or dev.mac
        dev.person_id = int(person_id) if person_id else None
        dev.speed_limit = speed_limit.strip()
        db.commit()
        # Закрепляем IP за устройством, чтобы правила по IP не «переехали»
        if dev.ip:
            await run_in_threadpool(_pin_lease, dev.mac, dev.ip, dev.name)
        log_event(db, "device_update", f"Устройство обновлено: {dev.name} ({dev.mac})")
    tasks.add_task(_reconcile_bg)
    return RedirectResponse("/devices", status_code=302)


@router.post("/devices/{device_id}/block")
async def block_device(device_id: int, tasks: BackgroundTasks, db: Session = Depends(get_db)):
    dev = db.get(Device, device_id)
    if dev:
        dev.blocked_manual = True
        db.commit()
        log_event(db, "block", f"Ручная блокировка: {dev.name} ({dev.ip})")
    tasks.add_task(_reconcile_bg)
    return RedirectResponse("/devices", status_code=302)


@router.post("/devices/{device_id}/unblock")
async def unblock_device(device_id: int, tasks: BackgroundTasks, db: Session = Depends(get_db)):
    dev = db.get(Device, device_id)
    if dev:
        dev.blocked_manual = False
        db.commit()
        log_event(db, "unblock", f"Разблокировано: {dev.name} ({dev.ip})")
    tasks.add_task(_reconcile_bg)
    return RedirectResponse("/devices", status_code=302)


@router.post("/devices/{device_id}/delete")
async def delete_device(device_id: int, tasks: BackgroundTasks, db: Session = Depends(get_db)):
    dev = db.get(Device, device_id)
    if dev:
        db.delete(dev)
        db.commit()
    tasks.add_task(_reconcile_bg)
    return RedirectResponse("/devices", status_code=302)
