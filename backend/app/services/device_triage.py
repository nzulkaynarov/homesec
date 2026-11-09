"""Sorting of devices nobody has claimed yet.

Phones, watches and laptops can rotate their private Wi-Fi address, so one
physical device leaves many records with the same hostname. Those records form
one group here, and a single decision clears all of them."""

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..ai import tools
from ..models import Device, Person, kv_set, log_event

MAC_LIKE = re.compile(r"^([0-9a-f]{2}[:-]){5}[0-9a-f]{2}$", re.IGNORECASE)


def _label(device: Device) -> str:
    return (device.hostname or device.name or "").strip()


def _group_key(device: Device) -> str:
    """Only private addresses group: two real MACs are two real devices, even with one name."""
    label = _label(device)
    if device.mac_is_random and label and not MAC_LIKE.match(label):
        return label.casefold()
    return f"#{device.id}"


def _unsorted(db: Session) -> list[Device]:
    query = select(Device).where(Device.kind == "personal", Device.person_id.is_(None))
    return list(db.scalars(query.order_by(Device.id)))


def _groups(db: Session) -> dict[str, list[Device]]:
    groups: dict[str, list[Device]] = {}
    for device in _unsorted(db):
        groups.setdefault(_group_key(device), []).append(device)
    return groups


def _owned_with_label(db: Session, key: str) -> list[Device]:
    query = select(Device).where(Device.kind == "personal", Device.person_id.is_not(None))
    return [d for d in db.scalars(query.order_by(Device.id)) if _label(d).casefold() == key]


def list_groups(db: Session) -> list[dict]:
    """Biggest groups first, so the first taps clear the most records."""
    views = []
    for key, members in _groups(db).items():
        head = members[0]
        owners = {d.person_id: d.person.name for d in _owned_with_label(db, key) if d.person}
        views.append(
            {
                "id": head.id,
                "name": head.name or head.hostname or head.mac,
                "hostname": head.hostname,
                "mac": head.mac,
                "mac_random": head.mac_is_random,
                "ip": head.ip,
                "count": len(members),
                "last_seen": max(d.first_seen for d in members).isoformat(timespec="minutes"),
                "owners": [{"id": pid, "name": name} for pid, name in owners.items()],
            }
        )
    return sorted(views, key=lambda view: (-view["count"], view["id"]))


def sort_group(db: Session, device_id: int, person_id: int | None, kind: str | None) -> dict:
    """Collapse a group into one device and give it an owner or a kind, in one transaction."""
    group = next((m for m in _groups(db).values() if any(d.id == device_id for d in m)), None)
    if group is None:
        raise LookupError(device_id)
    person = None
    target = group[0]
    if person_id is not None:
        person = db.get(Person, person_id)
        if person is None:
            raise LookupError(person_id)
        same_name = [
            d for d in _owned_with_label(db, _group_key(target)) if d.person_id == person_id
        ]
        target = same_name[0] if same_name else target
    for device in group:
        tools._guard_self(device)
    for device in group:
        if device.id != target.id:
            tools.apply_merge(db, device, target)
    if person is not None:
        target.person = person
    else:
        target.kind = kind or target.kind
    db.commit()
    kv_set(db, "reconcile_asap", "1")
    destination = person.name if person else kind
    log_event(
        db, "luna_action", f"Разбор устройств: {target.name}, записей {len(group)} → {destination}"
    )
    return {
        "name": target.name,
        "count": len(group),
        "person": person.name if person else None,
        "kind": target.kind,
    }
