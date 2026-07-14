"""Наблюдатель журнала событий: вычитывает новые записи, требующие
уведомления (новые устройства, заявки с портала), и отдаёт их боту. Курсор
(id последнего обработанного события) хранится в kv_state, поэтому переживает
рестарты; при самом первом запуске история пропускается, чтобы не заспамить
чат старыми событиями."""

import re
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Device, EventLog, kv_get, kv_set

CURSOR_KEY = "bot_last_event_id"
NOTIFY_KINDS = ("device_new", "register_request")

_MAC_RE = re.compile(r"([0-9A-F]{2}(?::[0-9A-F]{2}){5})", re.IGNORECASE)


@dataclass
class Notification:
    kind: str  # device_new | register_request
    device: Device
    message: str  # исходный текст события


def collect_notifications(db: Session) -> list[Notification]:
    """Уведомления из ещё не обработанных событий. Сдвигает курсор."""
    max_id = db.scalar(select(func.max(EventLog.id))) or 0
    raw = kv_get(db, CURSOR_KEY, "")
    if raw == "":
        # первый запуск: историю не рассылаем
        kv_set(db, CURSOR_KEY, str(max_id))
        return []
    last = int(raw)
    events = db.scalars(
        select(EventLog)
        .where(EventLog.id > last, EventLog.kind.in_(NOTIFY_KINDS))
        .order_by(EventLog.id)
    )
    out = []
    for e in events:
        m = _MAC_RE.search(e.message)
        if not m:
            continue
        dev = db.scalar(select(Device).where(Device.mac == m.group(1).upper()))
        if dev is not None:
            out.append(Notification(kind=e.kind, device=dev, message=e.message))
    if max_id > last:
        kv_set(db, CURSOR_KEY, str(max_id))
    return out
