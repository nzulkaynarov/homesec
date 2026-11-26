"""Pictures for the parents: a week of screen time, a day of the PC and the Pi, a week of AI spend.

matplotlib is imported on the first chart only: on the Pi 3 the import takes about three seconds and
some 50 MB, which a bot that may never draw a chart should not pay at start. Charts are drawn on
request, so the memory is spent only when someone asks.
"""

import io
import os
import tempfile
import threading
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..ai import client as ai_client
from ..models import Device, Person, QuotaUsage
from . import hosts, plain

DAYS = 7
SHORT_DAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
_lock = threading.Lock()  # pyplot keeps global state; one chart at a time


def _plt() -> Any:
    os.environ.setdefault("MPLCONFIGDIR", str(Path(tempfile.gettempdir()) / "homesec-matplotlib"))
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update(
        {
            "font.size": 10,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.grid": True,
            "grid.alpha": 0.3,
        }
    )
    return plt


def _png(plt: Any, fig: Any) -> bytes:
    buf = io.BytesIO()
    fig.tight_layout()
    fig.savefig(buf, format="png", dpi=120)
    plt.close(fig)
    return buf.getvalue()


def _day_labels(days: list[date]) -> list[str]:
    return [f"{SHORT_DAYS[d.weekday()]}\n{d:%d.%m}" for d in days]


# ---------------------------------------------------------------- data


def week_days(today: date) -> list[date]:
    return [today - timedelta(days=DAYS - 1 - i) for i in range(DAYS)]


def screen_week(db: Session, today: date) -> tuple[list[date], dict[str, list[int]]]:
    """Active minutes per child per day. A device's day counts once: the largest of its categories,
    since "internet" already covers games and videos."""
    days = week_days(today)
    kids = {p.id: p.name for p in db.scalars(select(Person).where(Person.role == "kid"))}
    owner_of = {d.id: kids[d.person_id] for d in db.scalars(select(Device)) if d.person_id in kids}
    per_device: dict[tuple[int, str], int] = defaultdict(int)
    first = days[0].isoformat()
    for row in db.scalars(select(QuotaUsage).where(QuotaUsage.date >= first)):
        if row.device_id in owner_of:
            key = (row.device_id, row.date)
            per_device[key] = max(per_device[key], row.minutes)
    index = {d.isoformat(): i for i, d in enumerate(days)}
    out: dict[str, list[int]] = {name: [0] * DAYS for name in sorted(set(owner_of.values()))}
    for (device_id, day), minutes in per_device.items():
        if day in index:
            out[owner_of[device_id]][index[day]] += minutes
    return days, out


BUCKET_MINUTES = (
    10  # a day of minute samples is a saw; ten-minute steps keep the peaks and read calmly
)


def _bucket(moment: datetime) -> datetime:
    return moment.replace(
        minute=moment.minute - moment.minute % BUCKET_MINUTES, second=0, microsecond=0
    )


def _steps(points: list[tuple[datetime, Any]], pick: Any) -> list[tuple[datetime, float]]:
    """Ten-minute steps with the hottest or lowest value of each: no warning is smoothed away."""
    groups: dict[datetime, list[float]] = defaultdict(list)
    for moment, value in points:
        if isinstance(value, int | float):
            groups[_bucket(moment)].append(float(value))
    return [(moment, pick(values)) for moment, values in sorted(groups.items())]


def host_day(db: Session, now: datetime) -> tuple[list[tuple[datetime, float]], ...]:
    """The last day in ten-minute steps: PC CPU °C (max), PC free memory GB (min), Pi °C (max)."""
    since = now - timedelta(days=1)
    pc = [(t, d) for t, d in hosts.samples(db, hosts.PC, since=since) if not d.get("stopping")]
    pi = hosts.samples(db, hosts.PI, since=since)
    pc_heat = _steps([(t, d.get("cpu_max_c")) for t, d in pc], max)
    memory = [(t, mb / 1024) for t, mb in _steps([(t, d.get("mem_avail_mb")) for t, d in pc], min)]
    pi_heat = _steps([(t, d.get("cpu_c")) for t, d in pi], max)
    return pc_heat, memory, pi_heat


def spend_week(db: Session, now: datetime) -> tuple[list[date], dict[str, list[float]]]:
    """Estimated AI dollars per person per day."""
    days = week_days(now.date())
    names = {f"person:{p.id}": p.name for p in db.scalars(select(Person))}
    names["system"] = "Система"
    out: dict[str, list[float]] = defaultdict(lambda: [0.0] * DAYS)
    for i, day in enumerate(days):
        moment = datetime.combine(day, now.time())
        for caller, entry in ai_client.ledger_for_day(db, moment).items():
            out[names.get(caller, "Владелец")][i] += float(entry.get("usd", 0))
    return days, dict(out)


# ---------------------------------------------------------------- pictures


def screen_week_png(days: list[date], people: dict[str, list[int]]) -> bytes:
    with _lock:
        plt = _plt()
        fig, ax = plt.subplots(figsize=(8, 4.2))
        count = max(1, len(people))
        width = 0.8 / count
        for n, (name, minutes) in enumerate(people.items()):
            xs = [i - 0.4 + width * (n + 0.5) for i in range(len(days))]
            bars = ax.bar(xs, [m / 60 for m in minutes], width, label=name)
            ax.bar_label(
                bars, labels=[f"{m // 60}:{m % 60:02d}" if m else "" for m in minutes], fontsize=7
            )
        ax.set_xticks(range(len(days)), _day_labels(days))
        ax.set_ylabel("часы")
        ax.set_title("Экранное время детей за неделю")
        if people:
            ax.legend(frameon=False)
        return _png(plt, fig)


def host_day_png(
    pc_heat: list[tuple[datetime, float]],
    memory: list[tuple[datetime, float]],
    pi_heat: list[tuple[datetime, float]],
) -> bytes:
    with _lock:
        plt = _plt()
        fig, (heat, free) = plt.subplots(2, 1, figsize=(8, 5.6), sharex=True, height_ratios=(3, 2))
        if pc_heat:
            heat.plot(*zip(*pc_heat, strict=True), label="процессор ПК", linewidth=1.4)
        if pi_heat:
            heat.plot(*zip(*pi_heat, strict=True), label="Raspberry Pi", linewidth=1.4)
        heat.axhline(hosts.CPU_HOT_C, color="tab:red", linestyle="--", linewidth=0.8)
        heat.axhline(hosts.PI_HOT_C, color="tab:orange", linestyle=":", linewidth=0.8)
        heat.set_ylabel("°C")
        heat.set_title("ПК и Pi за сутки")
        heat.legend(frameon=False, loc="upper left")
        if memory:
            free.plot(*zip(*memory, strict=True), color="tab:green", linewidth=1.4)
        free.axhline(hosts.PC_MEMORY_LOW_MB / 1024, color="tab:red", linestyle="--", linewidth=0.8)
        free.set_ylabel("свободно, ГБ")
        free.set_ylim(bottom=0)
        import matplotlib.dates as mdates

        free.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
        return _png(plt, fig)


def spend_week_png(days: list[date], people: dict[str, list[float]]) -> bytes:
    """Stacked bars per day. Below a dollar a day the axis is in cents, and nobody who spent
    nothing this week takes a place in the legend."""
    people = {name: usd for name, usd in people.items() if sum(usd) > 0}
    totals = [sum(day) for day in zip(*people.values(), strict=True)] or [0.0] * len(days)
    scale, unit = (1.0, "$") if max(totals) >= 1 else (1 / plain.CENT, "центы")
    with _lock:
        plt = _plt()
        fig, ax = plt.subplots(figsize=(8, 4.2))
        bottom = [0.0] * len(days)
        for name, usd in sorted(people.items(), key=lambda item: -sum(item[1])):
            values = [u * scale for u in usd]
            ax.bar(range(len(days)), values, 0.6, bottom=bottom, label=name)
            bottom = [b + v for b, v in zip(bottom, values, strict=True)]
        for i, total in enumerate(totals):
            if total:
                ax.annotate(
                    plain.money(total), (i, bottom[i]), ha="center", va="bottom", fontsize=8
                )
        ax.set_xticks(range(len(days)), _day_labels(days))
        ax.yaxis.set_major_formatter(lambda value, _pos: f"{value:g}".replace(".", ","))
        ax.set_ylabel(f"{unit}, оценка")
        ax.set_title(f"Расходы на ИИ за неделю: {plain.money(sum(totals))}")
        if people:
            ax.legend(frameon=False)
        return _png(plt, fig)
