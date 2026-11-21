"""What a parent confirms when the assistant proposes a change: each tool in plain words, who it
is for, what happens and until when. Never raw arguments, ids, addresses or English codes."""

from collections.abc import Callable
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import DEVICE_KIND_LABELS, Device, Person
from ..services import pauses, plain
from ..services.quota import QUOTA_CATEGORY_LABELS
from ..services.time_notices import duration
from . import tools

# Who a group word means, as a parent would say it.
GROUP_WORDS = {
    "kid": "все дети",
    "guest": "гости",
    "unknown": "незнакомые устройства",
    "adult": "взрослые",
    "shared": "общие устройства (телевизоры, колонки)",
    "iot": "умный дом",
}
KIND_WORDS = {
    "personal": "личное устройство",
    "shared": "общее устройство (телевизор, колонка)",
    "iot": "умный дом",
    "infra": "оборудование сети",
}
WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
GONE = "устройство, которого уже нет"


def _device(db: Session, value: object) -> Device | None:
    try:
        return db.get(Device, int(str(value)))
    except (TypeError, ValueError):
        return None


def _device_words(dev: Device | None) -> str:
    if dev is None:
        return GONE
    owner = f" ({dev.person.name})" if dev.person is not None else ""
    return f"{tools.device_label(dev)}{owner}"


def _person(db: Session, name: str) -> Person | None:
    key = name.strip().casefold()
    if not key:
        return None
    return next((p for p in db.scalars(select(Person)) if p.name.casefold() == key), None)


def _who(db: Session, target: object) -> str:
    """A pause, quota or bonus target: all children, a group, a child by name or a device."""
    asked = str(target or "").strip()
    if asked.casefold() in pauses.KIDS_WORDS:
        return GROUP_WORDS["kid"]
    if asked in GROUP_WORDS:
        return GROUP_WORDS[asked]
    person = _person(db, asked)
    if person is not None:
        return person.name
    dev = tools.find_device(db, asked)
    if dev is not None:
        return _device_words(dev)
    if not asked or asked.isdigit() or plain.is_address(asked):
        return GONE
    return f"«{asked}» (такого не нашёл)"


def _int(value: object, default: int = 0) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _until(args: dict, now: datetime) -> str:
    clock = str(args.get("until") or "").strip()
    if clock:
        try:
            return f"до {tools.next_clock(clock, now):%H:%M}"
        except tools.ToolError:
            return f"до «{clock}» (не понял время)"
    minutes = _int(args.get("minutes"))
    if minutes <= 0:
        return "время не указано"
    return f"до {now + timedelta(minutes=minutes):%H:%M} (на {duration(minutes)})"


def _days(raw: object) -> str:
    days = sorted({int(d) for d in str(raw or "").split(",") if d.strip() in set("0123456")})
    if not days or days == list(range(7)):
        return "каждый день"
    if days == [0, 1, 2, 3, 4]:
        return "по будням"
    if days == [5, 6]:
        return "по выходным"
    return ", ".join(WEEKDAYS[d] for d in days)


def _limit(category: object) -> str:
    return QUOTA_CATEGORY_LABELS.get(str(category or "internet"), "неизвестный лимит")


def _block(db: Session, args: dict, now: datetime) -> list[str]:
    dev = _device(db, args.get("device_id"))
    return ["⛔ Выключить интернет", f"Устройство: {_device_words(dev)}", "До: пока не включите"]


def _unblock(db: Session, args: dict, now: datetime) -> list[str]:
    dev = _device(db, args.get("device_id"))
    return ["✅ Включить интернет", f"Устройство: {_device_words(dev)}"]


def _speed(db: Session, args: dict, now: datetime) -> list[str]:
    dev = _device(db, args.get("device_id"))
    limit = str(args.get("limit") or "").strip()
    title = "🐢 Ограничить скорость" if limit else "🐢 Вернуть обычную скорость"
    lines = [title, f"Устройство: {_device_words(dev)}"]
    if limit:
        lines.append(f"Скорость: {plain.speed_words(limit)}")
    return lines


def _pause(db: Session, args: dict, now: datetime) -> list[str]:
    lines = ["⏸ Выключить интернет на время", f"Кому: {_who(db, args.get('target'))}"]
    lines.append(f"Когда включится: {_until(args, now)}")
    if str(args.get("reason") or "").strip():
        lines.append(f"Причина: {str(args['reason']).strip()}")
    return lines


def _bedtime(db: Session, args: dict, now: datetime) -> list[str]:
    who = str(args.get("who") or "").strip()
    person = _person(db, who)
    if who.casefold() in tools.BEDTIME_KIDS:
        whom = GROUP_WORDS["kid"]
    elif person is not None:
        whom = person.name
    else:
        whom = f"«{who}» (такого не нашёл)" if who else "не указано"
    nights = tools.NIGHT_WORDS.get(str(args.get("nights") or ""), "ночи не понял")
    return [
        "🌙 Отбой по расписанию",
        f"Кому: {whom}",
        f"Когда: {nights}",
        f"Выключать в {args.get('start') or '?'}, включать в {args.get('end') or '?'}",
    ]


def _resume(db: Session, args: dict, now: datetime) -> list[str]:
    return ["▶️ Снять паузу", f"Кому: {_who(db, args.get('target'))}"]


def _quota(db: Session, args: dict, now: datetime) -> list[str]:
    minutes = _int(args.get("minutes_per_day"))
    who = f"Кому: {_who(db, args.get('target'))}"
    if minutes <= 0:
        return [f"⏳ Убрать дневной лимит «{_limit(args.get('category'))}»", who]
    return [
        f"⏳ Дневной лимит «{_limit(args.get('category'))}»",
        who,
        f"Сколько: {duration(minutes)} в день",
        f"Дни: {_days(args.get('days', '0,1,2,3,4,5,6'))}",
    ]


def _bonus(db: Session, args: dict, now: datetime) -> list[str]:
    minutes = duration(_int(args.get("minutes")))
    lines = [
        "➕ Добавить время на сегодня",
        f"Кому: {_who(db, args.get('target'))}",
        f"Сколько: +{minutes} к лимиту «{_limit(args.get('category'))}»",
    ]
    if str(args.get("comment") or "").strip():
        lines.append(f"За что: {str(args['comment']).strip()}")
    return lines


def _give(db: Session, args: dict, now: datetime) -> list[str]:
    who = str(args.get("who") or "").strip()
    person = _person(db, who)
    if who.casefold() in pauses.KIDS_WORDS:
        whom = GROUP_WORDS["kid"]
    elif person is not None:
        whom = person.name
    else:
        whom = f"«{who}» (такого не нашёл)" if who else "не указано"
    minutes = _int(args.get("minutes"))
    whole = "до конца паузы, без паузы — весь день без лимитов"
    how = f"+{duration(minutes)}" if minutes > 0 else whole
    return [
        "➕ Дать ещё времени",
        f"Кому: {whom}",
        f"Сколько: {how}",
        "Во время паузы интернет откроется на это время, без паузы время добавится к лимитам.",
    ]


def _merge(db: Session, args: dict, now: datetime) -> list[str]:
    duplicate = _device_words(_device(db, args.get("duplicate_id")))
    target = _device_words(_device(db, args.get("target_id")))
    return [
        "🔁 Объединить устройства",
        f"Новая запись: {duplicate}",
        f"Это то же, что: {target}",
        "Имя, владелец и правила останутся от второго.",
    ]


def _assign(db: Session, args: dict, now: datetime) -> list[str]:
    dev = _device(db, args.get("device_id"))
    owner = str(args.get("person_name") or "").strip() or "никто (станет незнакомым)"
    return ["👤 Записать устройство", f"Устройство: {_device_words(dev)}", f"Владелец: {owner}"]


def _kind(db: Session, args: dict, now: datetime) -> list[str]:
    dev = _device(db, args.get("device_id"))
    kind = str(args.get("kind") or "").strip().lower()
    becomes = KIND_WORDS.get(kind) or DEVICE_KIND_LABELS.get(kind) or "вид не понял"
    return ["🏷 Вид устройства", f"Устройство: {_device_words(dev)}", f"Станет: {becomes}"]


def _movie(db: Session, args: dict, now: datetime) -> list[str]:
    hours = _int(args.get("hours"), 8)
    until = f"{now + timedelta(hours=hours):%H:%M}" if hours > 0 else "утра"
    return [
        "🍿 Режим для взрослых",
        "Телевизоры и приставки без детских ограничений",
        f"До: {until}",
    ]


def _note(db: Session, args: dict, now: datetime) -> list[str]:
    return ["📝 Запомнить в семейной памяти", f"«{str(args.get('content') or '').strip()}»"]


DESCRIBERS: dict[str, Callable[[Session, dict, datetime], list[str]]] = {
    "block_device": _block,
    "unblock_device": _unblock,
    "set_speed_limit": _speed,
    "pause_internet": _pause,
    "set_bedtime": _bedtime,
    "resume_internet": _resume,
    "set_quota": _quota,
    "add_bonus_time": _bonus,
    "give_time": _give,
    "merge_devices": _merge,
    "assign_device": _assign,
    "set_device_kind": _kind,
    "set_movie_mode": _movie,
    "save_family_note": _note,
}


def describe(db: Session, name: str, args: dict, now: datetime | None = None) -> str:
    """The confirmation text of a proposed change, one fact per line."""
    describer = DESCRIBERS.get(name)
    if describer is None:
        spec = tools.REGISTRY.get(name)
        return spec.description.splitlines()[0] if spec and spec.description else "Действие"
    return "\n".join(describer(db, args, now or datetime.now()))
