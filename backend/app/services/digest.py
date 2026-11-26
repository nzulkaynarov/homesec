"""The evening summary: the facts of the last day, gathered by code for one fixed layout.

Until 2026-11-26 a language model wrote the summary as running text from these facts. Parents
could not find what mattered in it, and it misread the journal (three "manual address" events
became "three manual IP bans"). Now code picks the facts and bot/digest.py draws them the same
way every evening, so what needs an answer always stands first.
"""

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import BonusRequest, Device, EventLog, Person, QuotaUsage, Rule
from . import adguard, plain

DAY = timedelta(days=1)
ERRORS_KEPT = 5
CATEGORY_COLUMNS = (("games", "Игры"), ("video", "Видео"), ("social", "Соцсети"))
CHANGE_KINDS = ("bot_action", "luna_action", "ai_action")
_BLOCKED = re.compile(r"^Заблокирован доступ: «(.+)»$")


@dataclass(frozen=True)
class Kid:
    name: str
    total: int  # minutes: a device's day is its largest category, as in the week chart
    categories: dict[str, int]  # quota category -> minutes summed over the child's devices
    luna: int  # questions to the AI today


@dataclass(frozen=True)
class Cutoff:
    """Devices whose internet went off at the same minute, and why when it is known."""

    at: str  # "22:00"
    names: list[str]
    why: str = ""


@dataclass(frozen=True)
class Waiting:
    request_id: int
    who: str
    since: datetime


@dataclass(frozen=True)
class Facts:
    day: datetime
    kids: list[Kid] = field(default_factory=list)
    waiting: list[Waiting] = field(default_factory=list)
    new_devices: list[str] = field(default_factory=list)
    cutoffs: list[Cutoff] = field(default_factory=list)
    changes: list[str] = field(default_factory=list)  # "09:27 Объединено: …"
    errors: list[str] = field(default_factory=list)  # first lines, for the owner
    dns_total: int | None = None
    dns_blocked: int | None = None


def _kids(db: Session, today: str, luna: dict[int, int]) -> list[Kid]:
    kids = {p.id: p.name for p in db.scalars(select(Person).where(Person.role == "kid"))}
    owner = {d.id: d.person_id for d in db.scalars(select(Device)) if d.person_id in kids}
    per_device: dict[int, dict[str, int]] = defaultdict(dict)
    for row in db.scalars(select(QuotaUsage).where(QuotaUsage.date == today)):
        if row.device_id in owner:
            per_device[row.device_id][row.category] = row.minutes
    totals: dict[int, int] = defaultdict(int)
    categories: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for device_id, spent in per_device.items():
        person_id = owner[device_id]
        totals[person_id] += max(spent.values(), default=0)
        for category, minutes in spent.items():
            categories[person_id][category] += minutes
    return [
        Kid(name, totals[pid], dict(categories[pid]), luna.get(pid, 0))
        for pid, name in sorted(kids.items(), key=lambda kv: kv[1])
    ]


def _why(rules: list[Rule], at: str, kind: str, line: str) -> str:
    if kind == "quota_block":
        return "кончился лимит"
    if line.startswith("Ручная блокировка"):
        return "вручную"
    for rule in rules:
        if rule.enabled and rule.start_time == at:
            return f"расписание «{rule.name}»"
    return ""


def _cutoffs(db: Session, events: list[EventLog]) -> list[Cutoff]:
    rules = list(db.scalars(select(Rule)))
    grouped: dict[tuple[str, str], list[str]] = {}
    for e in events:
        if e.kind not in ("block", "quota_block"):
            continue
        at = f"{e.ts:%H:%M}"
        line = plain.journal_line(e.message)
        if e.kind == "quota_block":
            name = line.split(":", 1)[-1].split("—", 1)[0].strip()
        else:
            found = _BLOCKED.match(line)
            name = found.group(1) if found else line.split(":", 1)[-1].strip()
        grouped.setdefault((at, _why(rules, at, e.kind, line)), []).append(name)
    return [Cutoff(at, names, why) for (at, why), names in grouped.items()]


def _waiting(db: Session) -> list[Waiting]:
    out = []
    pending = select(BonusRequest).where(BonusRequest.status == "pending")
    for req in db.scalars(pending.order_by(BonusRequest.created)):
        device = db.get(Device, req.device_id)
        if device is None:
            continue
        who = device.person.name if device.person else f"«{device.name}»"
        out.append(Waiting(req.id, who, req.created))
    return out


def collect(db: Session, now: datetime | None = None, luna: dict[int, int] | None = None) -> Facts:
    """The last day as parents read it. `luna` is {person id: AI questions today}."""
    now = now or datetime.now()
    query = select(EventLog).where(EventLog.ts >= now - DAY).order_by(EventLog.ts)
    events = list(db.scalars(query))
    stats: dict = {}
    try:
        stats = adguard.get_stats()
    except adguard.AdGuardError:
        pass
    return Facts(
        day=now,
        kids=_kids(db, now.strftime("%Y-%m-%d"), luna or {}),
        waiting=_waiting(db),
        new_devices=[
            plain.journal_line(e.message).removeprefix("Новое устройство: ")
            for e in events
            if e.kind == "device_new"
        ],
        cutoffs=_cutoffs(db, events),
        changes=[
            f"{e.ts:%H:%M} {plain.journal_line(e.message)}"
            for e in events
            if e.kind in CHANGE_KINDS
        ],
        errors=[
            f"{e.ts:%H:%M} {e.message.splitlines()[0][:200]}" for e in events if e.kind == "error"
        ][-ERRORS_KEPT:],
        dns_total=stats.get("num_dns_queries"),
        dns_blocked=stats.get("num_blocked_filtering"),
    )
