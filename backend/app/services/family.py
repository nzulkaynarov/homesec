"""Семейные функции: роли пользователей Telegram, радар присутствия «Кто дома»,
управление общими медиа-устройствами (ТВ/колонки), семейная память и учёт викторины Luna.
"""

import logging
import re
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import (
    Device,
    FamilyNote,
    Pause,
    Person,
    kv_get,
    kv_set,
    log_event,
)
from . import mikrotik

log = logging.getLogger("homesec.family")

ROLE_LABELS = {
    "owner": "Владелец сети",
    "admin": "Родитель",
    "kid": "Ребёнок",
    "guest": "Гость",
    "unauthorized": "Не авторизован",
}


def parent_chat_ids(db: Session) -> set[int]:
    """Chats that get alerts: the owner chats from settings plus every linked parent.

    Before this, only HS_TELEGRAM_CHAT_IDS were told anything, so a second parent
    approved in the bot never heard about new devices or a child's request."""
    linked = db.scalars(
        select(Person.telegram_id).where(
            Person.telegram_id.is_not(None), Person.bot_role.in_(("owner", "admin"))
        )
    )
    return set(settings.telegram_allowed_ids) | {int(t) for t in linked if t is not None}


def get_user_role(db: Session, telegram_id: int) -> tuple[str, Person | None]:
    """Определяет роль пользователя в семейном боте.
    Приоритет:
    1. settings.telegram_allowed_ids -> 'owner'
    2. Person.telegram_id -> person.bot_role ('owner' | 'admin' | 'kid' | 'guest')
    3. иначе -> ('unauthorized', None)
    """
    if telegram_id in settings.telegram_allowed_ids:
        # Корневой владелец из настроек
        person = db.scalar(select(Person).where(Person.telegram_id == telegram_id))
        return "owner", person

    person = db.scalar(select(Person).where(Person.telegram_id == telegram_id))
    if person is not None:
        role = person.bot_role if person.bot_role in ("owner", "admin", "kid", "guest") else "guest"
        return role, person

    return "unauthorized", None


def get_presence_status(db: Session) -> dict[str, Any]:
    """Собирает радар присутствия: кто из людей дома (по личным устройствам),
    статус общих медиа-устройств (ТВ/колонки) и умного дома (IoT)."""
    online_ips: set[str] = set()
    router_answered = True
    try:
        with mikrotik.api_session() as api:
            online_ips = mikrotik.get_online_ips(api)
    except Exception as e:
        router_answered = False
        log.warning("радар: ошибка чтения ARP с роутера: %s", e)

    people = list(db.scalars(select(Person).order_by(Person.id)))
    devices = list(db.scalars(select(Device)))

    # Устройства по владельцам
    people_status = []
    for p in people:
        my_devs = [d for d in devices if d.person_id == p.id and d.kind == "personal"]
        is_home = any(d.ip and d.ip in online_ips for d in my_devs)
        active_names = [d.name for d in my_devs if d.ip and d.ip in online_ips]
        people_status.append({
            "person": p,
            "is_home": is_home,
            "devices": my_devs,
            "active_devices": active_names,
        })

    # Общие медиа-устройства (ТВ, приставки, колонки)
    shared_devs = [d for d in devices if d.kind == "shared"]
    shared_status = []
    for d in shared_devs:
        is_online = bool(d.ip and d.ip in online_ips)
        shared_status.append({
            "device": d,
            "is_online": is_online,
            "blocked": d.blocked_manual,
        })

    # Умный дом (IoT)
    iot_devs = [d for d in devices if d.kind == "iot"]
    iot_online = sum(1 for d in iot_devs if d.ip and d.ip in online_ips)

    return {
        "router_answered": router_answered,
        "checked_at": datetime.now(),
        "people": people_status,
        "shared": shared_status,
        "iot_total": len(iot_devs),
        "iot_online": iot_online,
    }


def format_presence(status: dict[str, Any]) -> str:
    """Форматирует радар присутствия для Telegram."""
    lines = ["🏠 *Радар «Кто дома»*:\n"]
    if not status.get("router_answered", True):
        return lines[0] + "⚠️ Роутер не ответил, сейчас не могу сказать, кто дома."

    people = status.get("people", [])
    if people:
        lines.append("👥 *Семья:*")
        for p in people:
            person = p["person"]
            role_icon = "🎒" if person.role == "kid" else "👤"
            if p["is_home"]:
                dev_str = f" ({', '.join(p['active_devices'])})" if p["active_devices"] else ""
                lines.append(f"🟢 {role_icon} *{person.name}* — дома{dev_str}")
            else:
                lines.append(f"⚪ {role_icon} *{person.name}* — не в сети")
        lines.append("")

    shared = status.get("shared", [])
    if shared:
        lines.append("📺 *Общие устройства (ТВ и медиа):*")
        for s in shared:
            dev = s["device"]
            status_icon = "🟢" if s["is_online"] else "⚪"
            state = "онлайн" if s["is_online"] else "выкл"
            if s["blocked"]:
                state += ", ⛔ заблокирован"
            lines.append(f"{status_icon} *{dev.name}* — {state}")
        lines.append("")

    iot_total = status.get("iot_total", 0)
    if iot_total > 0:
        iot_online = status.get("iot_online", 0)
        lines.append(f"🤖 *Умный дом (IoT):* {iot_online} из {iot_total} устройств в сети.")

    checked_at = status.get("checked_at")
    if checked_at is not None:
        lines.append(f"🕐 Проверено в {checked_at:%H:%M:%S}")
    return "\n".join(lines).rstrip()


MORNING_HOUR = 7
SHARED_STATES = ("kid", "off", "adult")


def hours_until_morning(now: datetime | None = None) -> float:
    now = now or datetime.now()
    morning = now.replace(hour=MORNING_HOUR, minute=0, second=0, microsecond=0)
    if morning <= now:
        morning += timedelta(days=1)
    return (morning - now).total_seconds() / 3600


def shared_device_state(dev: Device, now: datetime | None = None) -> str:
    now = now or datetime.now()
    if dev.blocked_manual:
        return "off"
    if dev.adult_until is not None and dev.adult_until > now:
        return "adult"
    return "kid"


def set_shared_state(db: Session, dev: Device, state: str, hours: float | None = None) -> None:
    """Switch a shared device between kid mode, off, and open for adults until a set time."""
    if dev.kind != "shared":
        raise ValueError("Device is not shared")
    if state not in SHARED_STATES:
        raise ValueError(f"Unknown state: {state}")
    now = datetime.now()
    dev.blocked_manual = state == "off"
    dev.adult_until = now + timedelta(hours=hours or 1) if state == "adult" else None
    if state == "adult":
        for pause in db.scalars(
            select(Pause).where(Pause.target_type == "device", Pause.target == str(dev.id))
        ):
            db.delete(pause)
    db.commit()
    kv_set(db, "reconcile_asap", "1")
    log_event(db, "shared_state", f"{dev.name}: режим {state}")


def set_movie_mode(db: Session, hours: float | None = None) -> str:
    """Open every shared device for adults until morning or for the given hours."""
    shared_devs = list(db.scalars(select(Device).where(Device.kind == "shared")))
    if not shared_devs:
        return "В сети нет устройств с типом «Общее (ТВ/Колонка)»."
    duration = hours or hours_until_morning()
    for dev in shared_devs:
        set_shared_state(db, dev, "adult", duration)
    until = datetime.now() + timedelta(hours=duration)
    names = ", ".join(d.name for d in shared_devs)
    return f"🍿 *Режим для взрослых включён* до {until:%H:%M}.\n\nУстройства: {names}."


def save_family_note(db: Session, author: str, content: str) -> FamilyNote:
    """Сохраняет заметку в общую семейную память."""
    note = FamilyNote(author=author, content=content.strip())
    db.add(note)
    db.commit()
    log_event(db, "family_note", f"{author}: {note.content}")
    return note


def get_family_notes(db: Session, limit: int = 10) -> list[FamilyNote]:
    """Возвращает последние семейные заметки."""
    return list(
        db.scalars(
            select(FamilyNote)
            .order_by(FamilyNote.created.desc())
            .limit(limit)
        )
    )


def format_family_notes(notes: list[FamilyNote]) -> str:
    """Форматирует список заметок для Telegram."""
    if not notes:
        return "📝 В семейной памяти пока пусто.\n\nНапишите боту: «Запомни: размер ноги Миши 36»."

    lines = ["📝 *Семейная память и заметки:*\n"]
    for i, n in enumerate(notes, 1):
        dt = n.created.strftime("%d.%m %H:%M")
        auth = f" ({n.author})" if n.author else ""
        lines.append(f"{i}. {n.content}{auth} — _{dt}_")
    return "\n".join(lines)


def can_take_quiz_today(db: Session, person_id: int, now: datetime | None = None) -> bool:
    """Проверяет, не получал ли ребёнок уже бонус за викторину сегодня."""
    now = now or datetime.now()
    date = now.strftime("%Y-%m-%d")
    key = f"quiz_bonus_{date}_{person_id}"
    return kv_get(db, key, "") == "" and kv_get(db, f"quiz_attempt_{date}_{person_id}", "") == ""


def mark_quiz_attempted(db: Session, person_id: int, now: datetime | None = None) -> None:
    """Spends today's single attempt, so a wrong answer cannot be retried."""
    date = (now or datetime.now()).strftime("%Y-%m-%d")
    kv_set(db, f"quiz_attempt_{date}_{person_id}", "1")


QUIZ_BONUS_MINUTES = 15

SAFETY_PATTERN = re.compile(
    r"суицид|покончить с собой|убить себя|не хочу жить|порезать себя|"
    r"меня бьют|меня бьёт|меня бьет|меня трогает|домогает|меня травят|буллят|шантажиру",
    re.IGNORECASE,
)
SAFETY_REPLY = (
    "Спасибо, что написал мне об этом. Это важно, и ты не один 💛\n"
    "Пожалуйста, расскажи прямо сейчас родителям или другому взрослому, которому доверяешь. "
    "Если тебе угрожает опасность, позвони взрослым или в экстренную службу."
)


def needs_adult_attention(text: str) -> bool:
    """Keyword check for messages a parent should see; false positives are acceptable."""
    return bool(SAFETY_PATTERN.search(text))


def award_quiz_bonus(
    db: Session,
    person: Person,
    minutes: int = 15,
    topic: str = "Эрудиция",
    now: datetime | None = None,
) -> str:
    """Начисляет ребёнку бонусные минуты за успешный квиз."""
    now = now or datetime.now()
    date = now.strftime("%Y-%m-%d")
    key = f"quiz_bonus_{date}_{person.id}"
    kv_set(db, key, "1")

    from .time_grants import GrantError, grant  # time_grants imports house_rules, which imports us

    try:
        done = grant(db, person.id, minutes, f"Победа в викторине ({topic})", now)
    except GrantError as exc:
        done = str(exc)
    msg = f"🏆 {person.name} правильно ответил(а) на вопрос викторины ({topic}): {done}"
    log_event(db, "quiz_win", msg)
    return msg
