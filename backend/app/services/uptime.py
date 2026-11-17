"""Watch the devices the house depends on: network equipment always, anything else the
parents mark (a camera, say). One message when one drops off the network, one when it is
back. Presence comes from reconcile, which notes last_seen; without a recent successful
pass over the router nothing is judged, so a Pi that was itself down, or a router that does
not answer, never reads as every device gone at once."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..models import Device, kv_get, kv_set
from . import enforcement

# last_seen moves every SEEN_STEP while a device answers, so this is 5 to 10 minutes of silence.
OFFLINE_AFTER = timedelta(minutes=10)
DOWN_KEY = "uptime_down:{}"  # when the device was last seen, once its loss was announced


@dataclass(frozen=True)
class Change:
    device_id: int
    down: bool
    text: str
    since: str  # stored once the message is delivered; "" clears the announcement


def is_watched(device: Device) -> bool:
    return device.kind == "infra" or bool(device.watched)


def _name(device: Device) -> str:
    return f"«{device.name or device.hostname or device.mac}» ({device.ip})"


def _when(moment: datetime, now: datetime) -> str:
    return f"{moment:%H:%M}" if moment.date() == now.date() else f"{moment:%d.%m %H:%M}"


def _minutes(span: timedelta) -> str:
    minutes = max(1, int(span.total_seconds() // 60))
    if minutes < 120:
        return f"{minutes} мин"
    return f"{minutes // 60} ч {minutes % 60} мин"


def changes(db: Session, now: datetime | None = None) -> list[Change]:
    """Watched devices that went silent or came back since the last announcement."""
    now = now or datetime.now()
    fresh, _ = enforcement.enforcement_status(db, now)
    if not fresh:
        return []
    self_ips = enforcement.get_self_ips()
    found = db.scalars(
        select(Device).where(or_(Device.kind == "infra", Device.watched.is_(True)))
    )
    out = []
    for device in found:
        if not device.ip or device.ip in self_ips:
            continue  # without an address nothing is known; the Pi answering this is online
        announced = kv_get(db, DOWN_KEY.format(device.id))
        silent = device.last_seen is None or now - device.last_seen > OFFLINE_AFTER
        if silent and not announced:
            if device.last_seen is None:
                since, when = now.isoformat(timespec="seconds"), "давно"
            else:
                since = device.last_seen.isoformat(timespec="seconds")
                when = (
                    f"с {_when(device.last_seen, now)}, "
                    f"уже {_minutes(now - device.last_seen)}"
                )
            text = (
                f"🔌 {_name(device)} не в сети ({when}).\n"
                "Проверьте питание и кабель. Напишу, когда вернётся."
            )
            out.append(Change(device.id, True, text, since))
        elif not silent and announced:
            try:
                gone = f", не было {_minutes(now - datetime.fromisoformat(announced))}"
            except ValueError:
                gone = ""
            out.append(Change(device.id, False, f"✅ {_name(device)} снова в сети{gone}.", ""))
    return out


def record(db: Session, change: Change) -> None:
    """Remember what the parents were told, so each loss is announced once."""
    kv_set(db, DOWN_KEY.format(change.device_id), change.since)


def set_watched(db: Session, device_id: int, on: bool) -> str:
    device = db.get(Device, device_id)
    if device is None:
        raise LookupError(device_id)
    device.watched = on
    db.commit()
    if not on:
        kv_set(db, DOWN_KEY.format(device_id), "")
    if on:
        return f"Сообщу, если «{device.name}» пропадёт из сети больше чем на 10 минут."
    return f"Больше не слежу за связью «{device.name}»."
