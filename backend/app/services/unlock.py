"""Short windows a parent opens through a pause: internet for one person until a
moment, while the pause itself stays in place and resumes when the window ends.

A window never overrides bedtime or a manual block. It lets the person's devices
through pauses (their own, a device's or their group's) that existed when it
opened, and through the limits that were already used up when it opened
(enforcement reads the windows): a parent's "+15 min" during a pause opens the
internet for 15 minutes whatever had run out, while a limit that runs out later
still holds. A pause a parent sets later wins, and lifting the pause closes the
window, so the normal limits come back. Windows open only through
time_grants.grant. Stored in KVState so no schema change is needed:
"<until>;<opened>;<used-up limits, comma separated>"."""

from collections.abc import Iterable
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
    # The limits used up when the window opened; None for windows stored before they were kept,
    # which let every limit through as they did then.
    spent: frozenset[str] | None = None

    def lets_through(self, pause_created: datetime | None) -> bool:
        if self.opened is None or pause_created is None:
            return True
        return pause_created <= self.opened

    def lets_through_limit(self, category: str) -> bool:
        return self.spent is None or category in self.spent


def _key(person_id: int) -> str:
    return f"{PREFIX}{person_id}"


def _parse(raw: str | None, now: datetime) -> Window | None:
    if not raw:
        return None
    parts = raw.split(";")
    until_raw, opened_raw = parts[0], parts[1] if len(parts) > 1 else ""
    spent = frozenset(c for c in parts[2].split(",") if c) if len(parts) > 2 else None
    try:
        until = datetime.fromisoformat(until_raw)
        opened = datetime.fromisoformat(opened_raw) if opened_raw else None
    except ValueError:
        return None
    return Window(until, opened, spent) if until > now else None


def window(db: Session, person_id: int, now: datetime | None = None) -> Window | None:
    """The person's window open right now, or None."""
    return _parse(kv_get(db, _key(person_id)), now or datetime.now())


def open_until(db: Session, person_id: int, now: datetime | None = None) -> datetime | None:
    """End of the person's open window, or None when there is none right now."""
    found = window(db, person_id, now)
    return found.until if found else None


def open_window(
    db: Session, person_id: int, until: datetime, spent: Iterable[str] = ()
) -> datetime:
    """Open the person's internet through pauses until `until` (capped at 12 hours), and
    through the limits in `spent`, the ones used up now."""
    now = datetime.now()
    until = min(until, now + timedelta(minutes=MAX_MINUTES))
    stamp = ";".join(
        (
            until.isoformat(timespec="seconds"),
            now.isoformat(timespec="microseconds"),
            ",".join(sorted(set(spent))),
        )
    )
    kv_set(db, _key(person_id), stamp)
    return until


def close_window(db: Session, person_id: int) -> None:
    row = db.get(KVState, _key(person_id))
    if row is not None:
        db.delete(row)
        db.commit()


def close_all(db: Session) -> None:
    """Close every window: no pause is left to open."""
    rows = list(db.scalars(select(KVState).where(KVState.key.startswith(PREFIX))))
    for row in rows:
        db.delete(row)
    if rows:
        db.commit()


def open_windows(db: Session, now: datetime | None = None) -> dict[int, Window]:
    """{person id: window} for every window open now."""
    now = now or datetime.now()
    windows: dict[int, Window] = {}
    for row in db.scalars(select(KVState).where(KVState.key.startswith(PREFIX))):
        found = _parse(row.value, now)
        suffix = row.key.removeprefix(PREFIX)
        if found is not None and suffix.isdigit():
            windows[int(suffix)] = found
    return windows


def open_people(db: Session, now: datetime | None = None) -> set[int]:
    """Ids of people whose window is open now."""
    return set(open_windows(db, now))
