"""Which devices a pause switches off right now: one answer for the router, the bot, the panel
and the AI.

A pause reaches a device from three places: the device itself, the person who owns it, or the
device's group. A parent's open window (services/unlock.py) lets the owner through the pauses
that existed when it opened; a pause set later still holds. Every screen used to collect these
on its own, and three of them ignored person pauses, so a child paused from the bot looked
online in the panel and to the AI.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Device, Pause
from . import unlock

DEVICE = "device"
PERSON = "person"
GROUP = "group"


@dataclass(frozen=True)
class DevicePause:
    source_until: dict[str, datetime]  # the latest end per source: DEVICE, PERSON, GROUP
    held_until: datetime | None  # end of the pauses no window lets through; None = open now
    open_until: datetime | None  # the owner's open window, if any

    @property
    def holds(self) -> bool:
        """The pause really keeps the device offline right now."""
        return self.held_until is not None

    @property
    def sources(self) -> frozenset[str]:
        return frozenset(self.source_until)


def _reaches(pause: Pause, device: Device) -> str | None:
    if pause.target_type == DEVICE and pause.target == str(device.id):
        return DEVICE
    if (
        pause.target_type == PERSON
        and device.person_id is not None
        and pause.target == str(device.person_id)
    ):
        return PERSON
    if pause.target_type == GROUP and pause.target == device.group:
        return GROUP
    return None


def device_pauses(
    db: Session, devices: Iterable[Device], now: datetime | None = None
) -> dict[int, DevicePause]:
    """Every device that some pause reaches now, with where the pause comes from."""
    now = now or datetime.now()
    rows = list(db.scalars(select(Pause).where(Pause.until > now)))
    if not rows:
        return {}
    windows = unlock.open_windows(db, now)
    found: dict[int, DevicePause] = {}
    for device in devices:
        window = windows.get(device.person_id) if device.person_id is not None else None
        ends: dict[str, datetime] = {}
        held: datetime | None = None
        for row in rows:
            source = _reaches(row, device)
            if source is None:
                continue
            ends[source] = max(ends.get(source, row.until), row.until)
            if window is None or not window.lets_through(row.created):
                held = row.until if held is None else max(held, row.until)
        if ends:
            found[device.id] = DevicePause(
                source_until=ends,
                held_until=held,
                open_until=window.until if window else None,
            )
    return found


def held_until(db: Session, device: Device, now: datetime | None = None) -> datetime | None:
    """End of the pause that keeps this device offline right now, or None."""
    state = device_pauses(db, [device], now).get(device.id)
    return state.held_until if state is not None else None
