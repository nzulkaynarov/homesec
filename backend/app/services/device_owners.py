"""Who or what each device belongs to, and moving it elsewhere."""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..ai import tools
from ..models import DEVICE_KIND_LABELS, Device, Person, kv_set, log_event
from . import enforcement

KINDS = ("shared", "iot", "infra")
NO_OWNER = "none"


def section_of(device: Device) -> str:
    if device.kind in KINDS:
        return device.kind
    return f"p{device.person_id}" if device.person_id is not None else NO_OWNER


def is_this_pi(device: Device) -> bool:
    return bool(device.ip and device.ip in enforcement.get_self_ips())


def overview(db: Session) -> list[dict[str, Any]]:
    devices = list(db.scalars(select(Device)))
    counts: dict[str, int] = {}
    for device in devices:
        counts[section_of(device)] = counts.get(section_of(device), 0) + 1
    people = [
        {"key": f"p{p.id}", "label": f"👤 {p.name}", "count": counts.get(f"p{p.id}", 0)}
        for p in db.scalars(select(Person).order_by(Person.name))
    ]
    other = [
        {"key": "shared", "label": "📺 Общие", "count": counts.get("shared", 0)},
        {"key": "iot", "label": "🔌 Умный дом", "count": counts.get("iot", 0)},
        {"key": "infra", "label": "🛠 Инфраструктура", "count": counts.get("infra", 0)},
        {"key": NO_OWNER, "label": "❓ Без владельца", "count": counts.get(NO_OWNER, 0)},
    ]
    return people + other


def devices_in(db: Session, section: str) -> list[Device]:
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
