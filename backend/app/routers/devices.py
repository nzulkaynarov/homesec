import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import db as dbmod
from ..db import get_db
from ..models import (
    GROUP_LABELS,
    Device,
    Pause,
    Person,
    QuotaBonus,
    active_pauses,
    log_event,
)
from ..services import mikrotik, quota
from ..services.enforcement import reconcile
from ..services.quota import QUOTA_CATEGORIES
from ..templates_env import templates

log = logging.getLogger("homesec.devices")
router = APIRouter()

# Пресеты паузы для кнопок в один тап (минуты). "morning" считается отдельно.
PAUSE_PRESETS = {"30": 30, "60": 60, "180": 180}
MORNING_HOUR = 7  # «до утра» = ближайшие 07:00


def _pause_until(preset: str, now: datetime | None = None) -> datetime:
    """Момент окончания паузы по пресету. 'morning' → ближайшие 07:00."""
    now = now or datetime.now()
    if preset == "morning":
        target = now.replace(hour=MORNING_HOUR, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target
    minutes = PAUSE_PRESETS.get(preset, 60)
    return now + timedelta(minutes=minutes)


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


# ---------- быстрые действия родителя (с дашборда) ----------
# Пауза и бонус — те же сущности, что создаёт бот; здесь панель даёт их в один
# тап. redirect_to позволяет вернуть родителя на дашборд, откуда он нажал.

@router.post("/devices/{device_id}/pause")
async def pause_device(
    device_id: int,
    tasks: BackgroundTasks,
    preset: str = Form("60"),
    redirect_to: str = Form("/"),
    db: Session = Depends(get_db),
):
    dev = db.get(Device, device_id)
    if dev:
        until = _pause_until(preset)
        db.add(Pause(target_type="device", target=str(dev.id), until=until,
                     reason="пауза с дашборда"))
        db.commit()
        log_event(db, "pause",
                  f"Пауза: {dev.name} до {until.strftime('%d.%m %H:%M')}")
    tasks.add_task(_reconcile_bg)
    return RedirectResponse(_safe_redirect(redirect_to), status_code=302)


@router.post("/devices/{device_id}/unpause")
async def unpause_device(
    device_id: int,
    tasks: BackgroundTasks,
    redirect_to: str = Form("/"),
    db: Session = Depends(get_db),
):
    dev = db.get(Device, device_id)
    if dev:
        for p in list(db.scalars(select(Pause).where(
                Pause.target_type == "device", Pause.target == str(dev.id)))):
            db.delete(p)
        db.commit()
        log_event(db, "unpause", f"Пауза снята: {dev.name}")
    tasks.add_task(_reconcile_bg)
    return RedirectResponse(_safe_redirect(redirect_to), status_code=302)


@router.post("/devices/{device_id}/bonus")
async def bonus_device(
    device_id: int,
    tasks: BackgroundTasks,
    category: str = Form("internet"),
    minutes: int = Form(30),
    redirect_to: str = Form("/"),
    db: Session = Depends(get_db),
):
    dev = db.get(Device, device_id)
    if dev and category in QUOTA_CATEGORIES:
        minutes = max(1, min(int(minutes), 600))
        db.add(QuotaBonus(target_type="device", target=str(dev.id),
                          date=datetime.now().strftime("%Y-%m-%d"),
                          category=category, minutes=minutes,
                          comment="бонус с дашборда"))
        db.commit()
        log_event(db, "bonus", f"Бонус +{minutes} мин ({category}): {dev.name}")
    tasks.add_task(_reconcile_bg)
    return RedirectResponse(_safe_redirect(redirect_to), status_code=302)


@router.post("/groups/{group}/pause")
async def pause_group(
    group: str,
    tasks: BackgroundTasks,
    preset: str = Form("60"),
    redirect_to: str = Form("/"),
    db: Session = Depends(get_db),
):
    if group in GROUP_LABELS:
        until = _pause_until(preset)
        db.add(Pause(target_type="group", target=group, until=until,
                     reason="пауза группы с дашборда"))
        db.commit()
        log_event(db, "pause",
                  f"Пауза группы «{GROUP_LABELS[group]}» до {until.strftime('%d.%m %H:%M')}")
    tasks.add_task(_reconcile_bg)
    return RedirectResponse(_safe_redirect(redirect_to), status_code=302)


@router.post("/groups/{group}/unpause")
async def unpause_group(
    group: str,
    tasks: BackgroundTasks,
    redirect_to: str = Form("/"),
    db: Session = Depends(get_db),
):
    for p in list(db.scalars(select(Pause).where(
            Pause.target_type == "group", Pause.target == group))):
        db.delete(p)
    db.commit()
    if group in GROUP_LABELS:
        log_event(db, "unpause", f"Пауза группы «{GROUP_LABELS[group]}» снята")
    tasks.add_task(_reconcile_bg)
    return RedirectResponse(_safe_redirect(redirect_to), status_code=302)


def _safe_redirect(target: str) -> str:
    """Только локальные пути — защита от open-redirect через redirect_to."""
    return target if target.startswith("/") and not target.startswith("//") else "/"
