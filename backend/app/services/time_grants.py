"""Give a child more time: one answer for every place a parent says "yes".

"More time" used to mean "+N minutes of the internet quota" everywhere, but nobody has an internet
quota, so a child read "approved" and nothing changed. Now the grant acts on what actually holds
the child back: a pause opens for the minutes, and the limits already used up open with it for the
same minutes (enforcement lets a parent's window through them, services/unlock.py), every limit
on the child's devices grows by them (their own, their group's or a device's: a person's bonus
counts toward each of them), and at night nothing is added (the owner decided on 2026-11-11 that
bedtime is not opened).
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Device, Person, QuotaBonus, kv_set, log_event
from . import bonus, house_rules, quota
from .quota import QUOTA_CATEGORY_LABELS

WHOLE_DAY = None  # "until the end of the day / the pause"
DAY_MINUTES = 720


class GrantError(ValueError):
    """The text is safe to show to the parent."""


@dataclass(frozen=True)
class Granted:
    text: str  # what really changed, in words for the parent
    minutes: int  # what the child got; 0 when there was nothing to add to


def devices_of(db: Session, person_id: int) -> list[Device]:
    return list(db.scalars(select(Device).where(Device.person_id == person_id)))


def limit_categories(db: Session, person_id: int, now: datetime) -> list[str]:
    """Every limit a grant reaches: the person's own and any group or device limit on their
    devices today (the panel sets only those), in the panel's order."""
    found = set(house_rules.limits(db, person_id))
    for rows in quota.progress(db, devices_of(db, person_id), now).values():
        found.update(row["category"] for row in rows)
    return [category for category in quota.QUOTA_CATEGORIES if category in found]


def spent_categories(db: Session, person_id: int, now: datetime) -> list[str]:
    """The limits used up today on any of the person's devices, in the panel's order."""
    found: set[str] = set()
    for categories in quota.exhausted(db, devices_of(db, person_id), now).values():
        found |= categories
    return [category for category in quota.QUOTA_CATEGORIES if category in found]


def blocked_names(db: Session, person_id: int) -> list[str]:
    """The person's devices a parent switched off by hand: no grant opens them."""
    return [d.name for d in devices_of(db, person_id) if d.blocked_manual]


def _person(db: Session, person_id: int) -> Person:
    person = db.get(Person, person_id)
    if person is None:
        raise GrantError("Такого человека больше нет.")
    return person


def _no_night(db: Session, person_id: int, now: datetime) -> None:
    bedtime = house_rules.bedtime_until(db, person_id, now)
    if bedtime is not None:
        raise GrantError(f"Сейчас ночной режим до {bedtime:%H:%M}, ночью время не добавляется.")


def _add_to_limits(
    db: Session, person: Person, minutes: int | None, comment: str, now: datetime
) -> Granted | None:
    categories = limit_categories(db, person.id, now)
    if not categories:
        return None
    added = DAY_MINUTES if minutes is None else minutes
    day = now.strftime("%Y-%m-%d")
    for category in categories:
        db.add(
            QuotaBonus(
                target_type="person",
                target=str(person.id),
                date=day,
                category=category,
                minutes=added,
                comment=comment,
            )
        )
    db.commit()
    labels = ", ".join(QUOTA_CATEGORY_LABELS.get(c, c).lower() for c in categories)
    log_event(db, "bonus_granted", f"{person.name}: +{added} мин ({labels}) — {comment}")
    kv_set(db, "reconcile_asap", "1")
    if minutes is None:
        return Granted(f"лимиты на сегодня сняты: {labels}", added)
    return Granted(f"+{added} мин к лимитам: {labels}", added)


def _through_pause(
    db: Session, person_id: int, minutes: int | None, paused: datetime, now: datetime
) -> str:
    """Open a window through the pause; the limits used up now let the child through for the
    same minutes (a limit that runs out later still holds), and the text says so, and what holds
    the child again once the window closes."""
    spent = spent_categories(db, person_id, now)
    until = house_rules.open_through_pause(db, person_id, minutes, spent)
    ends_with_pause = until >= paused  # then only a used-up limit holds the child again
    text = f"интернет открыт до {until:%H:%M}" + (", до конца паузы" if ends_with_pause else "")
    if spent:
        labels = ", ".join(QUOTA_CATEGORY_LABELS.get(c, c).lower() for c in spent)
        text += f", исчерпанные лимиты ({labels}) до этого времени тоже не мешают"
    if not ends_with_pause:
        text += ("; " if spent else ", ") + "потом пауза продолжится"
    elif spent:
        text += "; потом лимиты снова действуют"
    blocked = blocked_names(db, person_id)
    if blocked:
        names = ", ".join(f"«{name}»" for name in blocked)
        text += f"; выключено вручную: {names}, время это не изменит"
    return text


def grant(
    db: Session, person_id: int, minutes: int | None, comment: str, now: datetime | None = None
) -> Granted:
    """Apply the time and say what really changed, in words for the parent and the child."""
    now = now or datetime.now()
    person = _person(db, person_id)
    _no_night(db, person_id, now)
    paused = house_rules.paused_until(db, person_id, now)
    given = DAY_MINUTES if minutes is None else minutes
    if paused is not None:
        return Granted(_through_pause(db, person_id, minutes, paused, now), given)
    granted = _add_to_limits(db, person, minutes, comment, now)
    if granted is not None:
        return granted
    blocked = blocked_names(db, person_id)
    if blocked:
        names = ", ".join(f"«{name}»" for name in blocked)
        return Granted(f"паузы и лимитов нет; выключено вручную: {names}, время это не изменит", 0)
    return Granted("ограничений сейчас нет, интернет и так открыт", 0)


def give(
    db: Session, person_id: int, minutes: int | None, comment: str, now: datetime | None = None
) -> Granted:
    """A parent's "more time" said without a request's buttons (the AI, /bonus, «⏳ Квоты»): the
    same grant, and the child's request still waiting is answered with it."""
    granted = grant(db, person_id, minutes, comment, now)
    bonus.approve_waiting(db, person_id, granted.minutes, now)
    return granted


def grant_limits(
    db: Session, person_id: int, minutes: int, comment: str, now: datetime | None = None
) -> Granted:
    """The buttons under a limit alert: the minutes go to the limits, also during a pause, which
    stays as it is. Nothing is added at night."""
    now = now or datetime.now()
    person = _person(db, person_id)
    _no_night(db, person_id, now)
    granted = _add_to_limits(db, person, minutes, comment, now)
    if granted is None:
        raise GrantError("Лимитов сегодня уже нет, добавлять время не к чему.")
    paused = house_rules.paused_until(db, person_id, now)
    if paused is not None:
        return Granted(f"{granted.text}; пауза до {paused:%H:%M} остаётся", granted.minutes)
    return granted
