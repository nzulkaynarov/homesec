"""Наблюдатель журнала событий: вычитывает новые записи device_new и отдаёт
их боту для уведомления. Курсор (id последнего обработанного события) хранится
в kv_state, поэтому переживает рестарты; при самом первом запуске история
пропускается, чтобы не заспамить чат старыми событиями."""

import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Device, EventLog, kv_get, kv_set

CURSOR_KEY = "bot_last_event_id"

_MAC_RE = re.compile(r"([0-9A-F]{2}(?::[0-9A-F]{2}){5})", re.IGNORECASE)


def collect_new_devices(db: Session) -> list[Device]:
    """Устройства из ещё не обработанных событий device_new. Сдвигает курсор."""
    max_id = db.scalar(select(func.max(EventLog.id))) or 0
    raw = kv_get(db, CURSOR_KEY, "")
    if raw == "":
        # первый запуск: историю не рассылаем
        kv_set(db, CURSOR_KEY, str(max_id))
        return []
    last = int(raw)
    events = db.scalars(
        select(EventLog)
        .where(EventLog.id > last, EventLog.kind == "device_new")
        .order_by(EventLog.id)
    )
    devices = []
    for e in events:
        m = _MAC_RE.search(e.message)
        if not m:
            continue
        dev = db.scalar(select(Device).where(Device.mac == m.group(1).upper()))
        if dev is not None:
            devices.append(dev)
    if max_id > last:
        kv_set(db, CURSOR_KEY, str(max_id))
    return devices
