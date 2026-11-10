"""Every change to a device in one place, so the bot, the panel and the AI do the same thing."""

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..ai import tools
from ..models import DEVICE_KIND_LABELS, Device, Person, kv_set, log_event
from . import enforcement
from .device_links import purge_device_rows
from .safetext import clean_label

KINDS = ("shared", "iot", "infra")
STALE = "stale"
STALE_DAYS = 30
# Online devices are stamped every few minutes (enforcement.SEEN_STEP), so a
# stamp this fresh means the device is on the network now.
ONLINE_WINDOW = enforcement.SEEN_STEP + timedelta(minutes=2)
NO_OWNER = "none"


def section_of(device: Device) -> str:
    if device.kind in KINDS:
        return device.kind
    return f"p{device.person_id}" if device.person_id is not None else NO_OWNER


def is_this_pi(device: Device) -> bool:
    return bool(device.ip and device.ip in enforcement.get_self_ips())


def describe(device: Device) -> str:
    """The name, plus what the device calls itself and its address, to tell similar ones apart."""
    hostname = device.hostname.strip()
    name = f"{device.name} ({hostname})" if hostname and hostname != device.name else device.name
    return f"{name} · .{device.ip.rsplit('.', 1)[-1]}" if device.ip else name


def is_online(device: Device, now: datetime | None = None) -> bool:
    now = now or datetime.now()
    return device.last_seen is not None and now - device.last_seen <= ONLINE_WINDOW


def label(device: Device, now: datetime | None = None) -> str:
    """describe() with a green dot for a device on the network now."""
    return f"🟢 {describe(device)}" if is_online(device, now) else describe(device)


def seen_text(device: Device, now: datetime | None = None) -> str:
    now = now or datetime.now()
    if device.last_seen is None:
        return f"больше {STALE_DAYS} дней назад или ни разу"
    if is_online(device, now):
        return "сейчас в сети"
    ago = now - device.last_seen
    if ago < timedelta(hours=1):
        return f"{max(1, int(ago.total_seconds() // 60))} мин назад"
    if ago < timedelta(days=1):
        return f"{int(ago.total_seconds() // 3600)} ч назад"
    return f"{ago.days} дн назад"


def is_stale(device: Device, now: datetime | None = None) -> bool:
    now = now or datetime.now()
    seen = device.last_seen or device.first_seen
    return seen is not None and now - seen > timedelta(days=STALE_DAYS)


def stale(db: Session, now: datetime | None = None) -> list[Device]:
    """Devices not seen for a month, except the Pi this code runs on."""
    devices = db.scalars(select(Device).order_by(Device.name, Device.id))
    return [d for d in devices if is_stale(d, now) and not is_this_pi(d)]


def purgeable(db: Session, now: datetime | None = None) -> list[Device]:
    """Stale devices safe to drop in one go: nobody's, and not blocked on purpose.

    A person's device or a blocked one keeps its rules until a parent decides."""
    return [d for d in stale(db, now) if d.person_id is None and not d.blocked_manual]


def _device(db: Session, device_id: int) -> Device:
    device = db.get(Device, device_id)
    if device is None:
        raise LookupError(device_id)
    return device


def _changed(db: Session, message: str) -> None:
    kv_set(db, "reconcile_asap", "1")
    log_event(db, "bot_action", message)


def rename(db: Session, device_id: int, name: str) -> str:
    device = _device(db, device_id)
    new = clean_label(name)
    if not new:
        raise tools.ToolError("Имя пустое, напишите его буквами.")
    old, device.name = device.name, new
    db.commit()
    log_event(db, "bot_action", f"Переименовано: {old} → {new}")
    return new


def merge(db: Session, keep_id: int, fold_id: int) -> str:
    """Fold one record into another: its addresses, minutes and requests move, the record goes."""
    if keep_id == fold_id:
        raise tools.ToolError("Это одна и та же запись.")
    keep, fold = _device(db, keep_id), _device(db, fold_id)
    tools._guard_self(fold)
    folded = fold.name
    tools.apply_merge(db, fold, keep)
    db.commit()
    _changed(db, f"Объединено: {folded} → {keep.name}")
    return keep.name


def delete(db: Session, device_id: int) -> str:
    """Forget a device. If it comes back, it shows up as a new one."""
    device = _device(db, device_id)
    name = device.name
    purge_device_rows(db, device.id)
    db.delete(device)
    db.commit()
    _changed(db, f"Удалено устройство: {name}")
    return name


def delete_many(db: Session, device_ids: list[int]) -> int:
    devices = [d for d in (db.get(Device, i) for i in device_ids) if d is not None]
    for device in devices:
        purge_device_rows(db, device.id)
        db.delete(device)
    db.commit()
    if devices:
        _changed(db, f"Удалены давно не появлявшиеся устройства: {len(devices)}")
    return len(devices)


def set_blocked(db: Session, device_id: int, blocked: bool) -> str:
    _device(db, device_id)
    name = "block_device" if blocked else "unblock_device"
    return str(tools.run_tool(db, name, {"device_id": device_id}, source="bot"))


def set_speed(db: Session, device_id: int, limit: str) -> str:
    _device(db, device_id)
    args = {"device_id": device_id, "limit": limit}
    return str(tools.run_tool(db, "set_speed_limit", args, source="bot"))


def merge_candidates(db: Session, device_id: int, limit: int = 10) -> list[Device]:
    """Other records most likely to be the same device: same network name, then same owner."""
    device = _device(db, device_id)
    key = device.hostname.casefold()
    others = [
        d
        for d in db.scalars(select(Device).order_by(Device.name, Device.id))
        if d.id != device.id and d.kind != "infra" and not is_this_pi(d)
    ]
    others.sort(
        key=lambda d: (
            not (key and d.hostname.casefold() == key),
            not (device.person_id is not None and d.person_id == device.person_id),
        )
    )
    return others[:limit]


def overview(db: Session) -> list[dict[str, Any]]:
    members: dict[str, list[str]] = {}
    now = datetime.now()
    for device in db.scalars(select(Device).order_by(Device.name, Device.id)):
        members.setdefault(section_of(device), []).append(label(device, now))
    sections = [
        (f"p{p.id}", f"👤 {p.name}") for p in db.scalars(select(Person).order_by(Person.name))
    ]
    sections += [
        ("shared", "📺 Общие"),
        ("iot", "🔌 Умный дом"),
        ("infra", "🛠 Инфраструктура"),
        (NO_OWNER, "❓ Без владельца"),
    ]
    return [
        {
            "key": key,
            "label": label,
            "count": len(members.get(key, [])),
            "devices": members.get(key, []),
        }
        for key, label in sections
    ]


def devices_in(db: Session, section: str) -> list[Device]:
    if section == STALE:
        return stale(db)
    devices = db.scalars(select(Device).order_by(Device.name, Device.id))
    return [device for device in devices if section_of(device) == section]


def move(db: Session, device_id: int, destination: str) -> dict[str, str]:
    """Give a device to a person, a kind or nobody; the Pi running this code stays where it is."""
    device = db.get(Device, device_id)
    if device is None:
        raise LookupError(device_id)
    if is_this_pi(device):
        raise tools.ToolError("Это сама Pi, она остаётся инфраструктурой.")
    person = None
    if destination in KINDS:
        label = DEVICE_KIND_LABELS[destination]
    elif destination == NO_OWNER:
        label = "без владельца"
    else:
        person = db.get(Person, int(destination))
        if person is None:
            raise LookupError(destination)
        label = person.name
    device.kind = destination if destination in KINDS else "personal"
    device.person = person
    device.adult_until = None
    db.commit()
    kv_set(db, "reconcile_asap", "1")
    log_event(db, "luna_action", f"Смена владельца: {device.name} → {label}")
    return {"name": device.name, "label": label}
