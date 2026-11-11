"""Short windows a parent opens through a pause: internet for one person until a
moment, while the pause itself stays in place and resumes when the window ends.

A window never overrides bedtime, a manual block or a spent internet quota;
it only lets the person's devices through pauses (their own, a device's or
their group's). Stored in KVState so no schema change is needed."""

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import KVState, kv_get, kv_set

PREFIX = "unlock:"
MAX_MINUTES = 12 * 60


def _key(person_id: int) -> str:
    return f"{PREFIX}{person_id}"


def open_until(db: Session, person_id: int, now: datetime | None = None) -> datetime | None:
    """End of the person's open window, or None when there is none right now."""
    now = now or datetime.now()
    raw = kv_get(db, _key(person_id))
    try:
        until = datetime.fromisoformat(raw) if raw else None
    except ValueError:
        return None
    return until if until is not None and until > now else None


def open_window(db: Session, person_id: int, until: datetime) -> datetime:
    """Open the person's internet through pauses until `until` (capped at 12 hours)."""
    until = min(until, datetime.now() + timedelta(minutes=MAX_MINUTES))
    kv_set(db, _key(person_id), until.isoformat(timespec="seconds"))
    return until


def close_window(db: Session, person_id: int) -> None:
    row = db.get(KVState, _key(person_id))
    if row is not None:
        db.delete(row)
        db.commit()


def open_people(db: Session, now: datetime | None = None) -> set[int]:
    """Ids of people whose window is open now."""
    now = now or datetime.now()
    people = set()
    for row in db.scalars(select(KVState).where(KVState.key.startswith(PREFIX))):
        try:
            if datetime.fromisoformat(row.value) > now:
                people.add(int(row.key.removeprefix(PREFIX)))
        except ValueError:
            continue
    return people
