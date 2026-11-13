"""Give a child more time: one answer for every place a parent says "yes".

"More time" used to mean "+N minutes of the internet quota" everywhere, but nobody has an internet
quota, so a child read "approved" and nothing changed. Now the grant acts on what actually holds
the child back: a pause opens for the minutes, limits for games, video and social media grow by
them, and at night nothing is added (the owner decided on 2026-11-11 that bedtime is not opened).
"""

from datetime import datetime

from sqlalchemy.orm import Session

from ..models import Person, QuotaBonus, kv_set, log_event
from . import house_rules
from .quota import QUOTA_CATEGORY_LABELS

WHOLE_DAY = None  # "until the end of the day / the pause"
DAY_MINUTES = 720


class GrantError(ValueError):
    """The text is safe to show to the parent."""


def grant(
    db: Session, person_id: int, minutes: int | None, comment: str, now: datetime | None = None
) -> str:
    """Apply the time and return what really changed, in words for the parent and the child."""
    now = now or datetime.now()
    person = db.get(Person, person_id)
    if person is None:
        raise GrantError("Такого человека больше нет.")
    bedtime = house_rules.bedtime_until(db, person_id, now)
    if bedtime is not None:
        raise GrantError(f"Сейчас ночной режим до {bedtime:%H:%M}, ночью время не добавляется.")
    if house_rules.paused_until(db, person_id, now) is not None:
        until = house_rules.open_through_pause(db, person_id, minutes)
        return f"интернет открыт до {until:%H:%M}, потом пауза продолжится"
    limits = house_rules.limits(db, person_id)
    if not limits:
        return "ограничений сейчас нет, интернет и так открыт"
    added = DAY_MINUTES if minutes is None else minutes
    day = now.strftime("%Y-%m-%d")
    for category in limits:
        db.add(
            QuotaBonus(
                target_type="person",
                target=str(person_id),
                date=day,
                category=category,
                minutes=added,
                comment=comment,
            )
        )
    db.commit()
    labels = ", ".join(QUOTA_CATEGORY_LABELS.get(c, c).lower() for c in limits)
    log_event(db, "bonus_granted", f"{person.name}: +{added} мин ({labels}) — {comment}")
    kv_set(db, "reconcile_asap", "1")
    if minutes is None:
        return f"лимиты на сегодня сняты: {labels}"
    return f"+{added} мин к лимитам: {labels}"
