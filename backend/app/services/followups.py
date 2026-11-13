"""Time requests never hang: parents are reminded after an hour, and a request without an answer
closes after a day, so the child sees a result. Plus the parents' weekly summary of the network."""

from collections import Counter
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import BonusRequest, Device, EventLog, kv_get, kv_set, log_event
from . import bonus, quota

REMIND_AFTER = timedelta(hours=1)
REMINDED_PREFIX = "bonus_reminded:"
EXPIRED = "expired"
WEEKLY_KEY = "weekly_summary_sent"
WEEKLY_WEEKDAY = 6  # Sunday
WEEKLY_HOUR = 20
WEEK = timedelta(days=7)


def _who(db: Session, req: BonusRequest) -> str:
    device = db.get(Device, req.device_id)
    if device is None:
        return "Устройство"
    if device.person is not None:
        return f"{device.person.name} ({device.name})"
    return device.name


def _what(req: BonusRequest) -> str:
    if req.category == bonus.PAUSE_CATEGORY:
        return bonus.PAUSE_LABEL
    return quota.QUOTA_CATEGORY_LABELS.get(req.category, req.category)


def due_reminders(db: Session, now: datetime | None = None) -> list[tuple[BonusRequest, str]]:
    """Requests waiting longer than an hour that were not reminded yet, with the reminder text.

    Each is marked before it is returned, so a failed send never turns into a stream of repeats.
    """
    now = now or datetime.now()
    rows = db.scalars(
        select(BonusRequest).where(
            BonusRequest.status == "pending",
            BonusRequest.created <= now - REMIND_AFTER,
            BonusRequest.created > now - bonus.REQUEST_TTL,
        )
    )
    due = []
    for req in rows:
        key = f"{REMINDED_PREFIX}{req.id}"
        if kv_get(db, key):
            continue
        kv_set(db, key, now.isoformat(timespec="seconds"))
        waited = int((now - req.created).total_seconds() // 60)
        due.append((req, f"⏰ {_who(db, req)} ждёт ответа {waited} мин: «{_what(req)}»"))
    return due


def expire_stale(db: Session, now: datetime | None = None) -> int:
    """Close requests nobody answered within a day; the child's page and Luna then say so."""
    now = now or datetime.now()
    rows = list(
        db.scalars(
            select(BonusRequest).where(
                BonusRequest.status == "pending", BonusRequest.created <= now - bonus.REQUEST_TTL
            )
        )
    )
    for req in rows:
        bonus.resolve(db, req, EXPIRED)
        log_event(db, "bonus_expired", f"Запрос без ответа закрыт: {_who(db, req)} [req#{req.id}]")
    return len(rows)


def weekly_due(db: Session, now: datetime | None = None) -> bool:
    now = now or datetime.now()
    return (
        now.weekday() == WEEKLY_WEEKDAY
        and now.hour >= WEEKLY_HOUR
        and kv_get(db, WEEKLY_KEY) != now.date().isoformat()
    )


def mark_weekly_sent(db: Session, day: date) -> None:
    kv_set(db, WEEKLY_KEY, day.isoformat())


def weekly_summary(db: Session, now: datetime | None = None) -> str:
    """What the house rules did over the last seven days, in numbers a parent can act on."""
    now = now or datetime.now()
    since = now - WEEK
    events = Counter(
        kind for kind in db.scalars(select(EventLog.kind).where(EventLog.ts >= since))
    )
    pauses_set = sum(
        1
        for message in db.scalars(
            select(EventLog.message).where(EventLog.ts >= since, EventLog.kind == "bot_action")
        )
        if message.startswith("Пауза:")
    )
    requests = Counter(
        db.scalars(select(BonusRequest.status).where(BonusRequest.created >= since))
    )
    asked = sum(requests.values())
    lines = [
        f"📊 HomeSec за неделю, {since:%d.%m}–{now:%d.%m}",
        "",
        f"• Отключений по правилам, паузам и лимитам: {events['block'] + events['quota_block']}",
        f"• Пауз из бота: {pauses_set}",
    ]
    if asked:
        lines.append(
            f"• Просьб о времени: {asked} — одобрено {requests['approved']}, "
            f"отказано {requests['denied']}, без ответа {requests[EXPIRED] + requests['pending']}"
        )
    else:
        lines.append("• Просьб о времени не было")
    lines.append(f"• Новых устройств: {events['device_new']}")
    if events["ip_manual"]:
        lines.append(
            f"• Устройства с адресом, заданным вручную: {events['ip_manual']} раз "
            "(так иногда пытаются обойти правила)"
        )
    if events["error"]:
        lines.append(f"• Сбоев применения правил: {events['error']}")
    return "\n".join(lines)
