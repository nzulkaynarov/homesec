"""A child's device in use during bedtime: tell the parents once a night.

Bedtime takes the internet away, not the device: a computer keeps running games that need no
internet, and nothing told the parents (seen on 2026-11-21, a PC played on until midnight). A
device counts as in use when it is on the network and keeps asking AdGuard for names, as a
working computer does hundreds of times in ten minutes; a phone asleep on its charger asks a
few times, so it raises nothing. Without the DNS log there is no alert rather than a guess.
"""

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Device, Person, Rule, kv_get, kv_set
from . import adguard
from .enforcement import rule_is_active
from .restrictions import rule_until

GRACE = timedelta(minutes=15)  # a device is allowed to wind down after bedtime starts
SEEN_WITHIN = timedelta(minutes=5)  # on the network now, not hours ago
ACTIVITY_WINDOW = timedelta(minutes=10)
ACTIVE_QUERIES = 40  # DNS questions in ten minutes that mean a device is in use
LOG_LIMIT = 3000
TOLD_KEY = "night_awake:"  # + device id + ":" + the morning the bedtime ends


@dataclass(frozen=True)
class Awake:
    device_id: int
    key: str  # remembered once the parents got the message
    text: str


def _bedtime(db: Session, person: Person, now: datetime) -> tuple[datetime, datetime] | None:
    """(start, end) of the child's bedtime that holds now; their own rule or a group's."""
    for rule in db.scalars(select(Rule)):
        if not rule_is_active(rule, now):
            continue
        mine = rule.target_type == "person" and str(rule.target) == str(person.id)
        group = rule.target_type == "group" and rule.target == person.role
        if not (mine or group):
            continue
        end = rule_until(rule, now)
        try:
            hour, minute = (int(x) for x in rule.start_time.split(":"))
        except ValueError:
            continue
        start = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if start > now:
            start -= timedelta(days=1)
        if end is not None:
            return start, end
    return None


def _activity(now: datetime) -> Counter[str] | None:
    """DNS questions per client address in the last ten minutes; None when AdGuard is silent."""
    try:
        log = adguard.get_query_log(limit=LOG_LIMIT)
    except Exception:  # noqa: BLE001 — no log means no alert, never a false one
        return None
    counts: Counter[str] = Counter()
    for entry in log:
        moment = adguard.parse_ts(entry.get("time", ""))
        if moment is not None and now - moment <= ACTIVITY_WINDOW:
            counts[str(entry.get("client", ""))] += 1
    return counts


def awake_devices(db: Session, now: datetime | None = None) -> list[Awake]:
    now = now or datetime.now()
    candidates: list[tuple[Person, Device, datetime, datetime]] = []
    for person in db.scalars(select(Person).where(Person.role == "kid")):
        window = _bedtime(db, person, now)
        if window is None or now - window[0] < GRACE:
            continue
        devices = db.scalars(
            select(Device).where(Device.person_id == person.id, Device.kind == "personal")
        )
        for device in devices:
            if device.ip and device.last_seen and now - device.last_seen <= SEEN_WITHIN:
                candidates.append((person, device, *window))
    if not candidates:
        return []
    activity = _activity(now)
    if activity is None:
        return []
    out = []
    for person, device, start, end in candidates:
        key = f"{TOLD_KEY}{device.id}:{end:%Y-%m-%d}"
        if activity[device.ip] < ACTIVE_QUERIES or kv_get(db, key, ""):
            continue
        text = (
            f"🌙 {person.name}: «{device.name}» работает в {now:%H:%M}, "
            f"хотя отбой с {start:%H:%M}.\n"
            "Интернета у него нет, но игры и программы без интернета работают. "
            "Выключить или ограничить само устройство можно только на нём, например "
            "через «Семейную безопасность» в Windows или «Экранное время» на iPhone."
        )
        out.append(Awake(device.id, key, text))
    return out


def remember(db: Session, awake: Awake) -> None:
    kv_set(db, awake.key, "1")
