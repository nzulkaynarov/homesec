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
LIMITS = "limits"  # minutes are added to every limit on the child's devices
BLOCKED = "blocked"  # only a device switched off by hand holds the child: time does not open it
OPEN = "open"  # nothing holds the child: there is nothing to add to
NO_OWNER = "no_owner"  # a device nobody owns: time goes to a person, so nobody gets it

# The hold a request's buttons were drawn for travels in their callback data: a press made after
# the state changed shows the request again instead of doing what the old button promised.
HOLD_CODES = {BEDTIME: "t", PAUSE: "p", LIMITS: "l", BLOCKED: "b", OPEN: "o", NO_OWNER: "n"}

ADD_15 = "➕ 15 мин"
ADD_30 = "➕ 30 мин"
WHOLE_PAUSE = "▶️ До конца паузы"
WHOLE_DAY = "🌞 До конца дня"
ALLOW = "👍 Разрешить"
DENY = "🚫 Отказать"
OWNER_HINT = "Владельца можно указать в «📱 Устройства»."
QUOTAS_BUTTON = "«⏳ Квоты»"

Choices = tuple[tuple[int, str], ...]  # (id, name) pairs for buttons


@dataclass(frozen=True)
class Hold:
    kind: str
    lines: tuple[str, ...]
    until: datetime | None = None
    opened: datetime | None = None  # a window a parent already opened through the pause
    blocked: Choices = ()  # the person's devices switched off by hand


@dataclass(frozen=True)
class Notice:
    text: str
    hold: str  # one of the kinds above: which buttons go under the text
    blocked: Choices = ()  # devices a parent can switch back on right under a request
    people: Choices = ()  # whose a device nobody owns can be


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


def _blocked_line(blocked: Choices) -> tuple[str, ...]:
    if not blocked:
        return ()
    return ("⛔ Выключено вручную: " + ", ".join(f"«{name}»" for _, name in blocked) + ".",)


def hold_now(db: Session, person: Person | None, devices: list[Device], now: datetime) -> Hold:
    """What keeps the person offline now, in the order time_grants.grant checks it. Limits are
    every limit on the person's devices, whoever set it: a grant reaches each of them."""
    if person is None:
        return Hold(NO_OWNER, _limit_lines(usage(db, devices, now)))
    bedtime = house_rules.bedtime_until(db, person.id, now)
    if bedtime is not None:
        return Hold(BEDTIME, (f"🌙 Сейчас ночной режим до {bedtime:%H:%M}.",), bedtime)
    blocked = tuple((d.id, d.name) for d in devices if d.blocked_manual)
    paused = house_rules.paused_until(db, person.id, now)
    if paused is not None:
        line = f"⏸ Сейчас пауза до {paused:%H:%M}."
        opened = unlock.open_until(db, person.id, now)
        if opened is not None:
            line += f" Интернет уже открыт родителем до {opened:%H:%M}."
        return Hold(PAUSE, (line,), paused, opened, blocked)
    spent = usage(db, devices, now)
    if spent:
        return Hold(LIMITS, _limit_lines(spent), blocked=blocked)
    if blocked:
        return Hold(BLOCKED, (), blocked=blocked)
    return Hold(OPEN, ("Сейчас ни паузы, ни лимитов: интернет открыт.",))


def _request_hint(hold: Hold, people: Choices) -> str:
    adds = f"«{ADD_15}» и «{ADD_30}»"
    switch_on = " Выключенное вручную устройство время не откроет: его можно включить кнопкой ниже."
    if hold.kind == BEDTIME and hold.until is not None:
        return (
            f"Ночью время не добавляется. Отказать можно сразу, а после {hold.until:%H:%M} "
            "нажмите «➕»: просьба обновится, и можно будет выбрать время."
        )
    if hold.kind == PAUSE and hold.until is not None:
        grows = "откроют интернет поверх паузы"
        if hold.opened is not None:
            grows = "продлят открытое время"
        hint = (
            f"{adds} {grows}, потом пауза продолжится; «{WHOLE_PAUSE}» откроет до "
            f"{hold.until:%H:%M}, когда пауза кончится."
        )
        return hint + (switch_on if hold.blocked else "")
    if hold.kind == LIMITS:
        hint = f"{adds} добавят минуты к каждому лимиту, «{WHOLE_DAY}» снимет лимиты до полуночи."
        return hint + (switch_on if hold.blocked else "")
    if hold.kind == BLOCKED:
        return "Время тут не поможет: включите устройство кнопкой ниже или откажите."
    if hold.kind == OPEN:
        return f"Добавлять время не к чему: «{ALLOW}» передаст через Luna, что вы разрешили."
    if not people:
        return "Устройство без владельца, время добавить некому: можно только отказать."
    return (
        "Устройство без владельца, время добавить некому. Чьё оно? "
        "Выберите ниже, и время можно будет дать."
    )


def _alert_hint(hold: Hold, now: datetime) -> str:
    adds = f"«{ADD_15}» и «{ADD_30}»"
    if hold.kind == NO_OWNER:
        return f"Устройство без владельца, время добавить некому. {OWNER_HINT}"
    if hold.kind == BEDTIME and hold.until is not None:
        if hold.until.date() > now.date():
            return "Ночью время не добавляется, а в полночь лимиты начнутся заново."
        return (
            f"Ночью время не добавляется. После {hold.until:%H:%M} его можно добавить "
            f"в {QUOTAS_BUTTON}."
        )
    if hold.kind == PAUSE and hold.until is not None:
        return (
            f"{adds} добавят минуты к каждому лимиту на сегодня, "
            f"а пауза до {hold.until:%H:%M} останется."
        )
    if hold.kind == LIMITS:
        return f"{adds} добавят минуты к каждому лимиту на сегодня."
    return "Этого лимита сегодня уже нет, добавлять время не нужно."


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
    people: Choices = ()
    if hold.kind == NO_OWNER and device is not None:
        people = tuple((p.id, p.name) for p in house_rules.people(db))
    if waited is None:
        lines = [f"🙏 {who} просит ещё времени"]
    else:
        lines = [f"⏰ {who} ждёт ответа уже {duration(waited)}: просит ещё времени"]
    if req.reason:
        lines.append(f"Причина: {req.reason}")
    lines += [*hold.lines, *_blocked_line(hold.blocked), "", _request_hint(hold, people)]
    return Notice("\n".join(lines), hold.kind, hold.blocked, people)


def quota_notice(
    db: Session, device: Device, kind: str, category: str | None, now: datetime | None = None
) -> Notice:
    """A limit almost used up (quota_warn) or the internet limit used up (quota_block). The
    buttons add minutes to the limits, so they show only while the limit is still there and
    never at night."""
    now = now or datetime.now()
    person = device.person
    who = _who(device, person)
    devices = _devices(db, device, person)
    hold = hold_now(db, person, devices, now)
    label = quota.QUOTA_CATEGORY_LABELS.get(category or "")
    limits_now = usage(db, devices, now)
    spent = limits_now.get(category or "")
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
    reachable = bool(limits_now) if label is None else spent is not None
    if hold.kind in (BLOCKED, OPEN) or (hold.kind in (PAUSE, LIMITS) and not reachable):
        hold = Hold(OPEN, ())  # the limit was lifted meanwhile: there is nothing to add to
    lines = [head]
    if hold.kind in (BEDTIME, PAUSE) or label is None:
        lines += hold.lines
    lines += ["", _alert_hint(hold, now)]
    return Notice("\n".join(lines), hold.kind)
