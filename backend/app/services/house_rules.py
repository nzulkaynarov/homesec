"""House rules for one person: bedtime, daily limits and pauses, plus the people list itself.

The enforcement core already understands rules, quotas and pauses aimed at a
person (all of their devices together); this module is what the bot's buttons
call, so parents can run the house without the LAN-only panel or the AI."""

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..models import (
    Device,
    Pause,
    Person,
    Quota,
    QuotaBonus,
    Rule,
    kv_set,
    log_event,
)
from .quota import QUOTA_CATEGORY_LABELS
from .safetext import clean_label

# Nights are named by the evening they start on: 0 = Monday, as in Rule.days.
# A bedtime from Sunday to Thursday evening covers the mornings before a weekday.
NIGHTS = {
    "school": ("6,0,1,2,3", "вс–чт", "перед будним днём"),
    "weekend": ("4,5", "пт–сб", "перед выходным"),
}
BEDTIME_PREFIX = "Отбой"
# "Internet" minutes count DNS activity, not screen time (docs/11-audit-2026-11.md),
# so the buttons only offer the categories that measure what they say.
LIMIT_CATEGORIES = ("games", "video", "social")
DEFAULT_WAKE = "07:00"
ROLES = {"kid": "Ребёнок", "adult": "Взрослый"}


class RuleError(ValueError):
    """A request the parent can fix; the text is shown to them as is."""


def _person(db: Session, person_id: int) -> Person:
    person = db.get(Person, person_id)
    if person is None:
        raise LookupError(person_id)
    return person


def _changed(db: Session, message: str) -> None:
    kv_set(db, "reconcile_asap", "1")
    log_event(db, "bot_action", message)


def _valid_time(value: str) -> str:
    try:
        return datetime.strptime(value, "%H:%M").strftime("%H:%M")
    except ValueError as exc:
        raise RuleError(f"Время «{value}» не похоже на ЧЧ:ММ") from exc


def _bedtime_rule(db: Session, person_id: int, night: str) -> Rule | None:
    days = NIGHTS[night][0]
    return db.scalar(
        select(Rule).where(
            Rule.target_type == "person", Rule.target == str(person_id), Rule.days == days
        )
    )


def bedtimes(db: Session, person_id: int) -> dict[str, Rule | None]:
    return {night: _bedtime_rule(db, person_id, night) for night in NIGHTS}


def set_bedtime(db: Session, person_id: int, night: str, start: str, end: str) -> str:
    person = _person(db, person_id)
    if night not in NIGHTS:
        raise RuleError("Неизвестный вариант ночей")
    start, end = _valid_time(start), _valid_time(end)
    if start == end:
        raise RuleError("Начало и конец отбоя совпадают")
    rule = _bedtime_rule(db, person_id, night)
    days, short, _ = NIGHTS[night]
    if rule is None:
        rule = Rule(target_type="person", target=str(person_id), days=days)
        db.add(rule)
    rule.name = f"{BEDTIME_PREFIX} {short}: {person.name}"
    rule.start_time, rule.end_time, rule.enabled = start, end, True
    db.commit()
    _changed(db, f"Отбой {short} для {person.name}: {start}–{end}")
    return f"Отбой {short}: {start}–{end}"


def clear_bedtime(db: Session, person_id: int, night: str) -> str:
    person = _person(db, person_id)
    rule = _bedtime_rule(db, person_id, night)
    short = NIGHTS[night][1]
    if rule is not None:
        db.delete(rule)
        db.commit()
        _changed(db, f"Отбой {short} для {person.name} выключен")
    return f"Отбой {short} выключен"


def limits(db: Session, person_id: int) -> dict[str, int]:
    rows = db.scalars(
        select(Quota).where(
            Quota.target_type == "person", Quota.target == str(person_id), Quota.enabled
        )
    )
    return {q.category: q.minutes_per_day for q in rows}


def set_limit(db: Session, person_id: int, category: str, minutes: int) -> str:
    """Minutes a day for all of the person's devices together; 0 removes the limit."""
    person = _person(db, person_id)
    if category not in LIMIT_CATEGORIES:
        raise RuleError("Неизвестная категория")
    if not 0 <= minutes <= 1440:
        raise RuleError("Лимит — от 0 до 1440 минут")
    label = QUOTA_CATEGORY_LABELS[category]
    rows = list(
        db.scalars(
            select(Quota).where(
                Quota.target_type == "person",
                Quota.target == str(person_id),
                Quota.category == category,
            )
        )
    )
    for extra in rows[1:]:
        db.delete(extra)
    quota = rows[0] if rows else None
    if minutes == 0:
        if quota is not None:
            db.delete(quota)
        result = f"{label}: без лимита"
    else:
        if quota is None:
            quota = Quota(target_type="person", target=str(person_id), category=category)
            db.add(quota)
        quota.name = f"{label} — {person.name}"
        quota.minutes_per_day, quota.enabled = minutes, True
        quota.days = "0,1,2,3,4,5,6"
        result = f"{label}: {minutes} мин в день"
    db.commit()
    _changed(db, f"Лимит для {person.name}: {result}")
    return result


def paused_until(db: Session, person_id: int, now: datetime | None = None) -> datetime | None:
    now = now or datetime.now()
    rows = db.scalars(
        select(Pause).where(
            Pause.target_type == "person", Pause.target == str(person_id), Pause.until > now
        )
    )
    return max((p.until for p in rows), default=None)


def wake_time(db: Session, person_id: int, now: datetime | None = None) -> datetime:
    """The next end of this person's bedtime, or 07:00 when none is set."""
    now = now or datetime.now()
    # The night that starts today decides tomorrow's wake-up; after midnight it is yesterday's.
    evening = now if now.hour >= 12 else now - timedelta(days=1)
    wake = DEFAULT_WAKE
    for rule in bedtimes(db, person_id).values():
        if rule is not None and str(evening.weekday()) in rule.days.split(","):
            wake = rule.end_time
    hour, minute = (int(part) for part in wake.split(":"))
    moment = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return moment if moment > now else moment + timedelta(days=1)


def pause(db: Session, person_id: int, minutes: int | None) -> str:
    """Pause all of the person's devices; None means until they wake up."""
    person = _person(db, person_id)
    now = datetime.now()
    until = wake_time(db, person_id, now) if minutes is None else now + timedelta(minutes=minutes)
    db.add(Pause(target_type="person", target=str(person_id), until=until, reason="бот"))
    db.commit()
    _changed(db, f"Пауза для {person.name} до {until:%H:%M}")
    return f"⏸ Пауза до {until:%H:%M}"


def resume(db: Session, person_id: int) -> str:
    person = _person(db, person_id)
    rows = list(
        db.scalars(
            select(Pause).where(Pause.target_type == "person", Pause.target == str(person_id))
        )
    )
    for row in rows:
        db.delete(row)
    db.commit()
    removed = len(rows)
    if removed:
        _changed(db, f"Пауза для {person.name} снята")
    return "▶️ Пауза снята" if removed else "Паузы не было"


def resume_all(db: Session) -> int:
    rows = list(db.scalars(select(Pause)))
    for row in rows:
        db.delete(row)
    db.commit()
    if rows:
        _changed(db, f"Сняты все паузы: {len(rows)}")
    return len(rows)


def _minutes(value: int) -> str:
    hours, rest = divmod(value, 60)
    if not hours:
        return f"{rest} мин"
    return f"{hours} ч {rest} мин" if rest else f"{hours} ч"


def summary(db: Session, person: Person, now: datetime | None = None) -> list[str]:
    """Short lines describing the person's rules, for the menu text."""
    nights = []
    for night, rule in bedtimes(db, person.id).items():
        short = NIGHTS[night][1]
        nights.append(f"{short} {rule.start_time}–{rule.end_time}" if rule else f"{short} нет")
    current = limits(db, person.id)
    limit_text = ", ".join(
        f"{QUOTA_CATEGORY_LABELS[c].split()[0].lower()} {_minutes(current[c])}"
        for c in LIMIT_CATEGORIES
        if c in current
    )
    lines = [f"🌙 Отбой: {'; '.join(nights)}", f"⏳ Лимиты: {limit_text or 'нет'}"]
    until = paused_until(db, person.id, now)
    if until is not None:
        lines.append(f"⏸ Пауза до {until:%H:%M}")
    return lines


def people(db: Session) -> list[Person]:
    """Children first, then adults, each alphabetically."""
    everyone = list(db.scalars(select(Person)))
    return sorted(everyone, key=lambda p: (p.role != "kid", p.name.casefold()))


def person_view(db: Session, person_id: int) -> dict[str, Any]:
    person = _person(db, person_id)
    count = len(list(db.scalars(select(Device.id).where(Device.person_id == person_id))))
    return {
        "id": person.id,
        "name": person.name,
        "role": person.role,
        "role_label": ROLES.get(person.role, person.role),
        "linked": person.telegram_id is not None,
        "owner": person.bot_role == "owner",
        "devices": count,
    }


def add_person(db: Session, name: str, role: str) -> str:
    clean = clean_label(name)
    if not clean:
        raise RuleError("Имя пустое, напишите его буквами.")
    if role not in ROLES:
        raise RuleError("Неизвестная роль")
    if db.scalar(select(Person).where(Person.name == clean)) is not None:
        raise RuleError(f"«{clean}» уже есть в списке.")
    bot_role = "kid" if role == "kid" else "guest"  # Telegram rights come with the access request
    db.add(Person(name=clean, role=role, bot_role=bot_role))
    db.commit()
    log_event(db, "bot_action", f"Добавлен человек: {clean} ({ROLES[role]})")
    return clean


def rename_person(db: Session, person_id: int, name: str) -> str:
    person = _person(db, person_id)
    clean = clean_label(name)
    if not clean:
        raise RuleError("Имя пустое, напишите его буквами.")
    taken = db.scalar(select(Person).where(Person.name == clean, Person.id != person_id))
    if taken is not None:
        raise RuleError(f"«{clean}» уже есть в списке.")
    old, person.name = person.name, clean
    db.commit()
    log_event(db, "bot_action", f"Переименован человек: {old} → {clean}")
    return clean


def set_role(db: Session, person_id: int, role: str) -> str:
    person = _person(db, person_id)
    if role not in ROLES:
        raise RuleError("Неизвестная роль")
    if person.bot_role == "owner" and role == "kid":
        raise RuleError("Владельца сети нельзя сделать ребёнком.")
    person.role = role
    if person.telegram_id is not None and person.bot_role != "owner":
        person.bot_role = "admin" if role == "adult" else "kid"
    db.commit()
    _changed(db, f"{person.name}: роль {ROLES[role]}")
    return ROLES[role]


def delete_person(db: Session, person_id: int) -> str:
    """Remove a person with their rules; their devices stay, without an owner."""
    person = _person(db, person_id)
    if person.bot_role == "owner":
        raise RuleError("Владельца сети удалить нельзя.")
    key = str(person_id)
    for device in db.scalars(select(Device).where(Device.person_id == person_id)):
        device.person = None
    for model in (Rule, Quota, Pause, QuotaBonus):
        db.execute(delete(model).where(model.target_type == "person", model.target == key))
    name = person.name
    db.delete(person)
    db.commit()
    _changed(db, f"Удалён человек: {name}")
    return name
