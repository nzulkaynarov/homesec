"""Наблюдатель журнала событий: вычитывает новые записи, требующие
уведомления (новые устройства, заявки с портала, блокировки по квоте),
и отдаёт их боту. Курсор
(id последнего обработанного события) хранится в kv_state, поэтому переживает
рестарты; при самом первом запуске история пропускается, чтобы не заспамить
чат старыми событиями."""

import re
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import BonusRequest, Device, EventLog, kv_get, kv_set

CURSOR_KEY = "bot_last_event_id"
NOTIFY_KINDS = ("device_new", "register_request", "device_maybe_same",
                "quota_block", "bonus_request")

_MAC_RE = re.compile(r"([0-9A-F]{2}(?::[0-9A-F]{2}){5})", re.IGNORECASE)
_REQ_RE = re.compile(r"req#(\d+)")


@dataclass
class Notification:
    kind: str  # device_new | register_request | device_maybe_same | quota_block | bonus_request
    device: Device
    message: str  # исходный текст события
    extra_device: Device | None = None  # для maybe_same: устройство-оригинал
    request_id: int | None = None  # для bonus_request: id заявки на бонус


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
        req_id = None
        dev = None
        if e.kind == "bonus_request":
            # Номер заявки — машинная метка «[req#N]» в КОНЦЕ сообщения, а до
            # неё идёт текст ребёнка. Берём последнее вхождение и, главное,
            # устройство определяем по самой заявке в БД, а не по MAC из
            # текста: и то и другое ребёнок мог бы подделать через имя
            # устройства или поле «почему» (см. services/safetext.py).
            found = _REQ_RE.findall(e.message)
            if not found:
                continue  # без id заявки одобрять нечего
            req_id = int(found[-1])
            req = db.get(BonusRequest, req_id)
            if req is None:
                continue
            dev = db.get(Device, req.device_id)
            if dev is None:
                continue  # устройство удалили, пока заявка ждала
        macs = [m.upper() for m in _MAC_RE.findall(e.message)]
        if dev is None:
            if not macs:
                continue
            dev = db.scalar(select(Device).where(Device.mac == macs[0]))
            if dev is None:
                continue
        extra = None
        if e.kind == "device_maybe_same" and len(macs) > 1:
            extra = db.scalar(select(Device).where(Device.mac == macs[1]))
            if extra is None:
                continue  # оригинал уже объединили/удалили
        out.append(Notification(kind=e.kind, device=dev, message=e.message,
                                extra_device=extra, request_id=req_id))
    if max_id > last:
        kv_set(db, CURSOR_KEY, str(max_id))
    return out
