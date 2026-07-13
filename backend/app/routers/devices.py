import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import db as dbmod
from ..db import get_db
from ..models import Device, Person, log_event
from ..services import mikrotik
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


@router.get("/devices")
async def devices_page(request: Request, db: Session = Depends(get_db)):
    devices = list(db.scalars(select(Device).order_by(Device.first_seen.desc())))
    people = list(db.scalars(select(Person).order_by(Person.name)))
    online_ips: set[str] = set()
    try:
        with mikrotik.api_session() as api:
            online_ips = mikrotik.get_online_ips(api)
    except mikrotik.MikrotikError as e:
        log.warning("%s", e)
    return templates.TemplateResponse(request, "devices.html", {
        "active": "devices",
        "devices": devices,
        "people": people,
        "online_ips": online_ips,
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
            try:
                with mikrotik.api_session() as api:
                    mikrotik.make_lease_static(api, dev.mac, dev.ip, comment=f"hs: {dev.name}")
            except mikrotik.MikrotikError as e:
                log.warning("%s", e)
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
