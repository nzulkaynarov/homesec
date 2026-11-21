"""What a parent reads about a child's time: who it is, why they ask, what holds them back right
now and what the buttons under the message will do.

The journal keeps its own lines with the MAC and the request number: the bot links an event to
its device and request by them, and the panel shows them as a log. Parents get these texts
instead, rendered from the request, the device and the person, so no address, number or English
status word reaches the family.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import BonusRequest, Device, Person
from . import house_rules, quota, unlock

# What a "give time" button does right now; the same order of checks as time_grants.grant.
BEDTIME = "bedtime"  # nothing is added at night
PAUSE = "pause"  # a window opens through the pause
LIMITS = "limits"  # minutes are added to every personal limit
OPEN = "open"  # no pause and no personal limit: there is nothing to add to
NO_OWNER = "no_owner"  # a device nobody owns: time goes to a person, so nobody gets it

ADD_15 = "➕ 15 мин"
ADD_30 = "➕ 30 мин"
WHOLE_PAUSE = "▶️ До конца паузы"
WHOLE_DAY = "🌞 До конца дня"
ALLOW = "👍 Разрешить"
DENY = "🚫 Отказать"
OWNER_HINT = "Владельца можно указать в /devices."


@dataclass(frozen=True)
class Hold:
    kind: str
    lines: tuple[str, ...]
    until: datetime | None = None


@dataclass(frozen=True)
class Notice:
    text: str
    hold: str  # one of the kinds above: which buttons go under the text


def duration(minutes: int) -> str:
    hours, rest = divmod(max(minutes, 0), 60)
    if not hours:
        return f"{rest} мин"
    return f"{hours} ч {rest} мин" if rest else f"{hours} ч"


def _devices(db: Session, device: Device | None, person: Person | None) -> list[Device]:
    if person is not None:
        return list(db.scalars(select(Device).where(Device.person_id == person.id)))
    return [device] if device is not None else []


def usage(db: Session, devices: list[Device], now: datetime) -> dict[str, tuple[int, int]]:
    """{category: (used, limit)} today; for each category the device that is closest to it."""

    def closeness(pair: tuple[int, int]) -> tuple[bool, int]:
        return pair[0] >= pair[1], pair[0]

    found: dict[str, tuple[int, int]] = {}
    for rows in quota.progress(db, devices, now).values():
        for row in rows:
            current = (row["used"], row["limit"])
            best = found.get(row["category"])
            if best is None or closeness(current) > closeness(best):
                found[row["category"]] = current
    return found


def _limit_lines(spent: dict[str, tuple[int, int]]) -> tuple[str, ...]:
    lines = []
    for category in quota.QUOTA_CATEGORIES:
        if category not in spent:
            continue
        used, limit = spent[category]
        label = quota.QUOTA_CATEGORY_LABELS[category]
        state = " — исчерпан" if used >= limit else ""
        lines.append(f"• {label}: {used} из {limit} мин{state}")
    return ("⏳ Лимиты на сегодня:", *lines) if lines else ()


def hold_now(db: Session, person: Person | None, devices: list[Device], now: datetime) -> Hold:
    """What keeps the person offline now, in the order time_grants.grant checks it."""
    if person is None:
        return Hold(NO_OWNER, _limit_lines(usage(db, devices, now)))
    bedtime = house_rules.bedtime_until(db, person.id, now)
    if bedtime is not None:
        return Hold(BEDTIME, (f"🌙 Сейчас ночной режим до {bedtime:%H:%M}.",), bedtime)
    paused = house_rules.paused_until(db, person.id, now)
    if paused is not None:
        line = f"⏸ Сейчас пауза до {paused:%H:%M}."
        opened = unlock.open_until(db, person.id, now)
        if opened is not None:
            line += f" Интернет уже открыт родителем до {opened:%H:%M}."
        return Hold(PAUSE, (line,), paused)
    lines = _limit_lines(usage(db, devices, now))
    if not house_rules.limits(db, person.id):
        return Hold(OPEN, lines or ("Сейчас ни паузы, ни лимитов: интернет открыт.",))
    return Hold(LIMITS, lines or ("⏳ Лимиты сегодня не действуют.",))


def _request_hint(hold: Hold) -> str:
    adds = f"«{ADD_15}» и «{ADD_30}»"
    if hold.kind == BEDTIME and hold.until is not None:
        return (
            f"Ночью время не добавляется: «➕» сработают после {hold.until:%H:%M}, "
            "отказать можно сразу."
        )
    if hold.kind == PAUSE and hold.until is not None:
        return (
            f"{adds} откроют интернет поверх паузы, «{WHOLE_PAUSE}» — до {hold.until:%H:%M}. "
            "Потом пауза продолжится."
        )
    if hold.kind == LIMITS:
        return f"{adds} добавят минуты к каждому лимиту, «{WHOLE_DAY}» снимет лимиты до полуночи."
    if hold.kind == OPEN:
        return (
            f"Паузы и личных лимитов нет, добавлять время не к чему: «{ALLOW}» просто передаст "
            "ответ через Luna."
        )
    return f"Устройство без владельца, время добавить некому: можно только отказать. {OWNER_HINT}"


def _alert_hint(hold: Hold) -> str:
    adds = f"«{ADD_15}» и «{ADD_30}»"
    if hold.kind == BEDTIME and hold.until is not None:
        return f"Ночью время не добавляется: кнопки сработают после {hold.until:%H:%M}."
    if hold.kind == PAUSE:
        return f"{adds} откроют интернет поверх паузы."
    if hold.kind == LIMITS:
        return f"{adds} добавят минуты к каждому лимиту на сегодня."
    if hold.kind == OPEN:
        return "Это не личный лимит ребёнка, кнопкой время к нему не добавить."
    return f"Устройство без владельца, время добавить некому. {OWNER_HINT}"


def _who(device: Device | None, person: Person | None) -> str:
    if person is not None:
        return person.name
    if device is not None:
        return f"Устройство «{device.name}»"
    return "Удалённое устройство"


def request_notice(
    db: Session, req: BonusRequest, now: datetime | None = None, waited: int | None = None
) -> Notice:
    """A child's request for time; with `waited` the reminder that it is still unanswered."""
    now = now or datetime.now()
    device = db.get(Device, req.device_id)
    person = device.person if device is not None else None
    who = _who(device, person)
    hold = hold_now(db, person, _devices(db, device, person), now)
    if waited is None:
        lines = [f"🙏 {who} просит ещё времени"]
    else:
        lines = [f"⏰ {who} ждёт ответа уже {duration(waited)}: просит ещё времени"]
    if req.reason:
        lines.append(f"Причина: {req.reason}")
    lines += [*hold.lines, "", _request_hint(hold)]
    return Notice("\n".join(lines), hold.kind)


def quota_notice(
    db: Session, device: Device, kind: str, category: str | None, now: datetime | None = None
) -> Notice:
    """A limit almost used up (quota_warn) or the internet limit used up (quota_block)."""
    now = now or datetime.now()
    person = device.person
    who = _who(device, person)
    devices = _devices(db, device, person)
    hold = hold_now(db, person, devices, now)
    label = quota.QUOTA_CATEGORY_LABELS.get(category or "")
    spent = usage(db, devices, now).get(category or "")
    numbers = f" ({spent[0]} из {spent[1]} мин)" if spent else ""
    if kind == "quota_block":
        head = (
            f"⛔ {who}: лимит «{label or 'Интернет целиком'}» исчерпан{numbers}, "
            "интернет выключен до полуночи."
        )
    elif label is None:
        head = f"⚠️ {who}: один из лимитов почти исчерпан."
    else:
        left = f", осталось {spent[1] - spent[0]} мин" if spent and spent[0] < spent[1] else ""
        head = f"⚠️ {who}: лимит «{label}» почти исчерпан{numbers}{left}."
    lines = [head]
    if hold.kind in (BEDTIME, PAUSE) or label is None:
        lines += hold.lines
    lines += ["", _alert_hint(hold)]
    return Notice("\n".join(lines), hold.kind)
