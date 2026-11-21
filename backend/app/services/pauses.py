"""Switch children or shared devices off the internet for a while: one service for every screen.

A target is all children, one child (every device they own) or one device. "All children" is one
pause per child with the same end, whichever screen, command or AI tool sets it, so one child can
be let out and a grant works per child. A new pause on a target replaces the old one, so "change
the time" and "pause again" mean the same thing. Pauses end by themselves; reconcile applies them
within seconds through the reconcile_asap flag.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..models import Device, Pause, Person, kv_set, log_event
from . import house_rules, pause_state, unlock

MAX_PAUSE = timedelta(hours=24)
PRESET_MINUTES = (15, 30, 60, 120, 180)
MORNING = "m"
ALL_KIDS_NAME = "Все дети"
# What a parent may type after /pause or say to the AI for every child at once.
KIDS_WORDS = frozenset({"kid", "kids", "дети", "все дети", "детям", "всем детям"})
KIDS_RESUMED = f"▶️ {ALL_KIDS_NAME}: интернет снова включён"


class PauseError(ValueError):
    """The text is safe to show to a parent."""


@dataclass(frozen=True)
class Target:
    kind: str  # "kids" | "person" | "device"
    id: int = 0

    @property
    def code(self) -> str:
        return "a" if self.kind == "kids" else f"{self.kind[0]}{self.id}"

    @staticmethod
    def parse(code: str) -> "Target | None":
        if code == "a":
            return Target("kids")
        if len(code) > 1 and code[0] in "pd" and code[1:].isdigit():
            return Target("person" if code[0] == "p" else "device", int(code[1:]))
        return None


ALL_KIDS = Target("kids")


def kids(db: Session) -> list[Person]:
    return list(db.scalars(select(Person).where(Person.role == "kid").order_by(Person.name)))


def shared_devices(db: Session) -> list[Device]:
    return list(db.scalars(select(Device).where(Device.kind == "shared").order_by(Device.name)))


def kid_devices(db: Session, person_id: int) -> list[Device]:
    query = select(Device).where(Device.person_id == person_id, Device.kind == "personal")
    return list(db.scalars(query.order_by(Device.name)))


def pausable(device: Device) -> bool:
    """Network equipment and the Pi itself are never switched off."""
    from .device_owners import is_this_pi  # device_owners imports the AI tools, which import us

    return device.kind != "infra" and not is_this_pi(device)


def _person(db: Session, person_id: int) -> Person:
    person = db.get(Person, person_id)
    if person is None:
        raise LookupError(person_id)
    return person


def _device(db: Session, device_id: int) -> Device:
    device = db.get(Device, device_id)
    if device is None:
        raise LookupError(device_id)
    if not pausable(device):
        raise PauseError("Это сетевое оборудование, его отключать нельзя.")
    return device


def label(db: Session, target: Target) -> str:
    if target.kind == "kids":
        return ALL_KIDS_NAME
    if target.kind == "person":
        return _person(db, target.id).name
    return _device(db, target.id).name


def morning(db: Session, target: Target, now: datetime | None = None) -> datetime:
    """When "until morning" ends: the child's wake-up time, 07:00 for shared devices."""
    now = now or datetime.now()
    person_id: int | None = None
    if target.kind == "person":
        person_id = target.id
    elif target.kind == "device":
        person_id = _device(db, target.id).person_id
    elif target.kind == "kids":
        children = kids(db)
        if children:  # the latest wake-up, so nobody is let out early
            return max(house_rules.wake_time(db, kid.id, now) for kid in children)
    if person_id is not None:
        return house_rules.wake_time(db, person_id, now)
    hour, minute = (int(part) for part in house_rules.DEFAULT_WAKE.split(":"))
    moment = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return moment if moment > now else moment + timedelta(days=1)


def until_for(db: Session, target: Target, choice: str, now: datetime | None = None) -> datetime:
    """A button's choice (minutes or "m" for morning) as the moment the pause ends."""
    now = now or datetime.now()
    if choice == MORNING:
        return morning(db, target, now)
    if not choice.isdigit():
        raise PauseError("Не понял время.")
    return now + timedelta(minutes=int(choice))


_CLOCK = re.compile(r"^(?:до\s*)?(\d{1,2})[:.](\d{2})$")
_HOURS = re.compile(r"^(\d+(?:[.,]\d+)?)\s*(?:ч|час|часа|часов|h)\.?$")
_MINUTES = re.compile(r"^(\d+)\s*(?:м|мин|минут|минуты|минуту|min)?\.?$")


def parse_until(text: str, now: datetime | None = None) -> datetime | None:
    """A typed time: "45" or "45 мин" (minutes), "2ч" or "1,5 часа", "18:30" or "до 18:30"."""
    now = now or datetime.now()
    value = " ".join(text.lower().split())
    if match := _CLOCK.match(value):
        hour, minute = int(match.group(1)), int(match.group(2))
        if hour > 23 or minute > 59:
            return None
        moment = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return moment if moment > now else moment + timedelta(days=1)
    if match := _HOURS.match(value):
        return now + timedelta(hours=float(match.group(1).replace(",", ".")))
    if match := _MINUTES.match(value):
        return now + timedelta(minutes=int(match.group(1)))
    return None


def _replace(
    db: Session, target_type: str, target_id: int, until: datetime, reason: str
) -> None:
    for row in db.scalars(
        select(Pause).where(Pause.target_type == target_type, Pause.target == str(target_id))
    ):
        db.delete(row)
    db.add(Pause(target_type=target_type, target=str(target_id), until=until, reason=reason))


def _applied(db: Session, message: str) -> None:
    db.commit()
    log_event(db, "bot_action", message)
    kv_set(db, "reconcile_asap", "1")


def pause(
    db: Session,
    target: Target,
    until: datetime,
    now: datetime | None = None,
    reason: str = "бот",
) -> str:
    """Switch the target off until `until`; returns the confirmation for the parent, the same
    words from the «⏸ Пауза» and «📋 Правила» screens, /pause and the AI."""
    now = now or datetime.now()
    if until <= now:
        raise PauseError("Это время уже прошло.")
    if until - now > MAX_PAUSE:
        raise PauseError("Отключить можно не больше чем на сутки.")
    affected: list[Person] = []
    if target.kind == "kids":
        children = kids(db)
        if not children:
            raise PauseError("В HomeSec пока нет детей.")
        affected = list(children)
        # The new time replaces a pause left on the whole group, as it does a child's own.
        db.execute(
            delete(Pause).where(
                Pause.target_type == "group", Pause.target == house_rules.KIDS_GROUP
            )
        )
        for kid in children:
            _replace(db, "person", kid.id, until, reason)
            unlock.close_window(db, kid.id)  # a pause the parent sets now wins over an open window
        name = ALL_KIDS_NAME
    elif target.kind == "person":
        person = _person(db, target.id)
        affected = [person]
        name = person.name
        _replace(db, "person", target.id, until, reason)
        unlock.close_window(db, target.id)
    else:
        name = _device(db, target.id).name
        _replace(db, "device", target.id, until, reason)
    _applied(db, f"Пауза: {name} до {until:%d.%m %H:%M}")
    return f"⏸ {name}: без интернета до {until:%H:%M}" + _asleep_note(db, affected, until, now)


def _asleep_note(db: Session, people: list[Person], until: datetime, now: datetime) -> str:
    """A pause inside a bedtime changes nothing; the parent should know instead of guessing."""
    asleep: dict[str, datetime] = {}
    for person in people:
        end = house_rules.bedtime_until(db, person.id, now)
        if end is not None and until <= end:
            asleep[person.name] = end
    if not asleep:
        return ""
    wake = max(asleep.values())
    if len(asleep) == len(people):
        return f"\n😴 Сейчас и так отбой до {wake:%H:%M}, пауза ничего не меняет."
    names = ", ".join(asleep)
    return f"\n😴 {names} и так спят до {wake:%H:%M}, для них пауза ничего не меняет."


def own_pauses(db: Session, target: Target) -> list[Pause]:
    """The pauses that resume() lifts for a person or a device: a person's own, the ones on
    their devices and, for a child, a pause left on the whole children group (resume() keeps
    it for the other children); a device's own."""
    if target.kind == "person":
        person = _person(db, target.id)
        device_ids = {str(d.id) for d in kid_devices(db, target.id)}
        group = house_rules.group_pauses(db) if person.role == "kid" else []
        return group + [
            row
            for row in db.scalars(select(Pause))
            if (row.target_type == "person" and row.target == str(target.id))
            or (row.target_type == "device" and row.target in device_ids)
        ]
    _device(db, target.id)
    query = select(Pause).where(Pause.target_type == "device", Pause.target == str(target.id))
    return list(db.scalars(query))


def resume(db: Session, target: Target) -> str:
    """Lift the pauses set on the target itself; a bedtime or a limit keeps working. A window a
    parent opened through the pause closes once nothing pauses the person, so their limits hold
    again."""
    if target.kind == "kids":
        removed = house_rules.resume_kids(db)
        kv_set(db, "reconcile_asap", "1")
        return KIDS_RESUMED if removed else "У детей паузы не было"
    name = label(db, target)
    if target.kind == "person" and _person(db, target.id).role == "kid":
        house_rules.split_group_pause(db)  # the other children stay paused
    rows = own_pauses(db, target)
    for row in rows:
        db.delete(row)
    db.flush()
    person_id = target.id if target.kind == "person" else _device(db, target.id).person_id
    if person_id is not None:
        house_rules.close_window_if_free(db, person_id)
    if not rows:
        return f"У «{name}» паузы не было"
    _applied(db, f"Пауза снята: {name}")
    return f"▶️ {name}: интернет снова включён"


def paused_until(db: Session, target: Target, now: datetime | None = None) -> datetime | None:
    """End of the pause that switches the target off right now, whoever set it."""
    now = now or datetime.now()
    if target.kind == "kids":
        return house_rules.kids_paused_until(db, now)
    if target.kind == "person":
        return house_rules.paused_until(db, target.id, now)
    return pause_state.held_until(db, _device(db, target.id), now)


def _together(db: Session, now: datetime) -> set[str]:
    """The children (person ids as stored in a pause) shown as one «Все дети» line: every child
    is paused until the same minute, as a pause for all of them leaves it. An only child keeps
    their name."""
    until = house_rules.kids_paused_until(db, now)
    children = kids(db)
    if until is None or len(children) < 2:
        return set()
    minute = until.replace(second=0, microsecond=0)
    for kid in children:
        end = paused_until(db, Target("person", kid.id), now)
        if end is None or end.replace(second=0, microsecond=0) != minute:
            return set()
    return {str(kid.id) for kid in children}


def active(db: Session, now: datetime | None = None) -> list[tuple[str, datetime]]:
    """Every pause in force, as (who, until), for the overview."""
    now = now or datetime.now()
    together = _together(db, now)
    found: dict[str, datetime] = {}
    for row in db.scalars(select(Pause).where(Pause.until > now)):
        if row.target_type == "person" and row.target in together:
            name: str | None = ALL_KIDS_NAME
        elif row.target_type == "person":
            person = db.get(Person, int(row.target)) if row.target.isdigit() else None
            name = person.name if person else None
        elif row.target_type == "device":
            device = db.get(Device, int(row.target)) if row.target.isdigit() else None
            name = device.name if device else None
        else:
            name = {"kid": ALL_KIDS_NAME, "guest": "Гости", "unknown": "Незнакомые"}.get(row.target)
        if name:
            found[name] = max(found.get(name, row.until), row.until)
    return sorted(found.items(), key=lambda item: item[1])
