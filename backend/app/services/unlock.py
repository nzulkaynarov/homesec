"""Short windows a parent opens through a pause: internet for one person until a
moment, while the pause itself stays in place and resumes when the window ends.

A window never overrides bedtime, a manual block or a spent internet quota;
it only lets the person's devices through pauses (their own, a device's or
their group's) that existed when it opened. A pause a parent sets later wins.
Stored in KVState so no schema change is needed: "<until>;<opened>"."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import KVState, kv_get, kv_set

PREFIX = "unlock:"
MAX_MINUTES = 12 * 60


@dataclass(frozen=True)
class Window:
    until: datetime
    opened: datetime | None  # None for windows stored before the opening time was kept

    def lets_through(self, pause_created: datetime | None) -> bool:
        if self.opened is None or pause_created is None:
            return True
        return pause_created <= self.opened


def _key(person_id: int) -> str:
    return f"{PREFIX}{person_id}"


def _parse(raw: str | None, now: datetime) -> Window | None:
    if not raw:
        return None
    until_raw, _, opened_raw = raw.partition(";")
    try:
        until = datetime.fromisoformat(until_raw)
        opened = datetime.fromisoformat(opened_raw) if opened_raw else None
    except ValueError:
        return None
    return Window(until, opened) if until > now else None


def open_until(db: Session, person_id: int, now: datetime | None = None) -> datetime | None:
    """End of the person's open window, or None when there is none right now."""
    window = _parse(kv_get(db, _key(person_id)), now or datetime.now())
    return window.until if window else None


def open_window(db: Session, person_id: int, until: datetime) -> datetime:
    """Open the person's internet through pauses until `until` (capped at 12 hours)."""
    now = datetime.now()
    until = min(until, now + timedelta(minutes=MAX_MINUTES))
    stamp = f"{until.isoformat(timespec='seconds')};{now.isoformat(timespec='microseconds')}"
    kv_set(db, _key(person_id), stamp)
    return until


def close_window(db: Session, person_id: int) -> None:
    row = db.get(KVState, _key(person_id))
    if row is not None:
        db.delete(row)
        db.commit()


def open_windows(db: Session, now: datetime | None = None) -> dict[int, Window]:
    """{person id: window} for every window open now."""
    now = now or datetime.now()
    windows: dict[int, Window] = {}
    for row in db.scalars(select(KVState).where(KVState.key.startswith(PREFIX))):
        window = _parse(row.value, now)
        suffix = row.key.removeprefix(PREFIX)
        if window is not None and suffix.isdigit():
            windows[int(suffix)] = window
    return windows


def open_people(db: Session, now: datetime | None = None) -> set[int]:
    """Ids of people whose window is open now."""
    return set(open_windows(db, now))
