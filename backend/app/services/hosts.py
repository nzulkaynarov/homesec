"""Health of the two machines the house runs on: this Raspberry Pi and the PC that hosts Luna.

The PC reports once a minute through the Luna API; the Pi samples itself. Samples are kept a
week on the Pi, so the minutes before a sudden power-off survive even when the PC cannot boot.
The owner hears about a problem once, after a run of bad samples, and once when it is gone:
the point is to see a disk filling or a backup stopping days before it hurts, not after."""

import json
import shutil
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..models import HostSample, KVState, kv_get, kv_set

PC, PI = "pc", "pi"
KEEP = timedelta(days=7)
SILENT_AFTER = timedelta(minutes=3)
RECENT = 10  # samples read to judge a run of bad ones

ALERT_KEY = "host_alert:"  # + problem key; "1" while the owner has been told
SILENT_KEY = "host_silent:pc"  # the last PC sample's time, once its silence was announced
BOOT_KEY = "host_boot:pc"  # the PC boot already seen
KERNEL_KEY = "host_kernel:pc"  # "<boot id>:<count>" of kernel hardware errors already told
OOM_KEY = "host_oom:pc"  # "<boot id>:<count>" of processes killed for lack of memory already told
OOM_NOTE_KEY = "host_oom_note:pc"  # when the last routine note about a session's memory was sent
# A session filling its own memory limit loses a test run and retries it; the owner hears about
# that at most once an hour. Claude itself or the whole PC running out is told at once.
OOM_NOTE_EVERY = timedelta(hours=1)

# Thresholds. A run of this many bad minutes raises an alert; temperatures and memory spike
# under normal work, so they need several, a full disk or an old backup does not.
CPU_HOT_C = 90
NVME_HOT_C = 70
DISK_LOW_PCT = 15
PC_MEMORY_LOW_MB = 2048
BACKUP_OLD_H = 36
TAILSCALE_WARN_DAYS = 14
PI_HOT_C = 75
PI_MEMORY_LOW_MB = 80
RUNS = {
    "pc:cpu": 5,
    "pc:nvme": 3,
    "pc:disk": 2,
    "pc:memory": 2,  # two minutes in a row under 2 GB: the out-of-memory killer is close
    "pc:service": 3,
    "pc:backup": 1,
    "pc:tailscale": 1,
    "pi:cpu": 5,
    "pi:power": 1,
    "pi:disk": 2,
    "pi:memory": 5,
}

SERVICE_NAMES = {
    "luna-assistant": "Luna",
    "lunagpt-gateway": "шлюз моделей",
    "lunagpt-postgres": "база Luna",
    "lunagpt-langfuse": "Langfuse",
    "lunagpt-searxng": "поиск",
    "ollama": "локальные модели (Ollama)",
}
BACKUP_NAMES = {"luna": "Luna", "homesec": "HomeSec и роутер"}


@dataclass(frozen=True)
class Change:
    key: str  # KV key remembered once the message is delivered
    value: str
    text: str


# ---------------------------------------------------------------- storage


def record(db: Session, host: str, data: dict[str, Any], now: datetime | None = None) -> None:
    now = now or datetime.now()
    db.add(HostSample(host=host, received=now, data=json.dumps(data, ensure_ascii=False)))
    db.execute(delete(HostSample).where(HostSample.received < now - KEEP))
    db.commit()


def samples(
    db: Session, host: str, since: datetime | None = None, limit: int | None = None
) -> list[tuple[datetime, dict[str, Any]]]:
    """Oldest first."""
    query = select(HostSample).where(HostSample.host == host)
    if since is not None:
        query = query.where(HostSample.received >= since)
    query = query.order_by(HostSample.received.desc(), HostSample.id.desc())
    if limit is not None:
        query = query.limit(limit)
    rows = list(db.scalars(query))
    return [(row.received, json.loads(row.data)) for row in reversed(rows)]


def latest(db: Session, host: str) -> tuple[datetime, dict[str, Any]] | None:
    found = samples(db, host, limit=1)
    return found[0] if found else None


# ---------------------------------------------------------------- the Pi itself


def _read(path: str) -> str:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return ""


def _meminfo_mb(key: str) -> int | None:
    for line in _read("/proc/meminfo").splitlines():
        name, _, rest = line.partition(":")
        if name == key and rest.split():
            return int(rest.split()[0]) // 1024
    return None


def _throttled() -> int | None:
    """vcgencmd get_throttled: bit 0 under-voltage now, 2 throttled now, 16 under-voltage
    since boot, 18 throttled since boot."""
    try:
        out = subprocess.run(
            ["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=5
        ).stdout
        return int(out.strip().split("=")[1], 16)
    except (OSError, subprocess.SubprocessError, IndexError, ValueError):
        return None


def free_pct(path: str) -> float | None:
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None
    return round(100 * usage.free / usage.total, 1) if usage.total else None


def pi_sample() -> dict[str, Any]:
    data: dict[str, Any] = {}
    raw = _read("/sys/class/thermal/thermal_zone0/temp")
    if raw.isdigit():
        data["cpu_c"] = round(int(raw) / 1000, 1)
    bits = _throttled()
    if bits is not None:
        data["undervoltage_now"] = bool(bits & 0x1)
        data["undervoltage_since_boot"] = bool(bits & 0x10000)
        data["throttled_since_boot"] = bool(bits & 0x40000)
    disk = free_pct("/")
    if disk is not None:
        data["disks"] = {"/": disk}
    data["mem_avail_mb"] = _meminfo_mb("MemAvailable")
    load = _read("/proc/loadavg").split(" ")[0]
    data["load1"] = float(load) if load else None
    uptime = _read("/proc/uptime").split(" ")[0]
    data["uptime_s"] = int(float(uptime)) if uptime else None
    try:
        data["boot"] = {"id": _read("/proc/sys/kernel/random/boot_id")}
    except OSError:
        pass
    return {k: v for k, v in data.items() if v is not None}


# ---------------------------------------------------------------- what is wrong in a sample


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def comma(value: float, digits: int = 1) -> str:
    return f"{value:.{digits}f}".replace(".", ",")


def problems(host: str, data: dict[str, Any]) -> dict[str, str]:
    """Problem key -> the message that announces it. Missing readings are not problems."""
    found: dict[str, str] = {}
    if host == PC:
        cpu = _num(data.get("cpu_max_c"))
        if cpu is not None and cpu >= CPU_HOT_C:
            found["pc:cpu"] = f"🔥 Процессор ПК горячий уже 5 минут: {cpu:.0f} °C."
        nvme = _num(data.get("nvme_c"))
        if nvme is not None and nvme >= NVME_HOT_C:
            found["pc:nvme"] = f"🔥 Системный диск ПК нагрелся до {nvme:.0f} °C."
        memory = _num(data.get("mem_avail_mb"))
        if memory is not None and memory < PC_MEMORY_LOW_MB:
            found["pc:memory"] = (
                f"🧠 На ПК почти кончилась память: свободно {comma(memory / 1024)} ГБ. "
                f"Больше всех ест: {data.get('top_process') or 'неизвестно'}."
            )
        for name in data.get("services_down") or []:
            label = SERVICE_NAMES.get(name, name)
            found[f"pc:service:{name}"] = f"🔴 На ПК не работает {label} ({name})."
        for name, age in (data.get("backup_age_h") or {}).items():
            label = BACKUP_NAMES.get(name, name)
            if age is None:
                found[f"pc:backup:{name}"] = f"💾 Резервной копии {label} на ПК нет ни одной."
            elif _num(age) is not None and age > BACKUP_OLD_H:
                found[f"pc:backup:{name}"] = (
                    f"💾 Резервная копия {label} не обновлялась {age:.0f} ч. "
                    "Ночное копирование не сработало."
                )
        days = _num(data.get("tailscale_days"))
        if days is not None and days <= TAILSCALE_WARN_DAYS:
            found["pc:tailscale"] = (
                f"🔑 Ключ Tailscale на ПК истекает через {days:.0f} дн.: удалённый доступ "
                "пропадёт. Продлите в админке Tailscale или отключите истечение ключа."
            )
        where = "ПК"
        disk_key = "pc:disk"
    else:
        cpu = _num(data.get("cpu_c"))
        if cpu is not None and cpu >= PI_HOT_C:
            found["pi:cpu"] = f"🔥 Raspberry Pi горячий уже 5 минут: {cpu:.0f} °C."
        if data.get("undervoltage_now") or data.get("undervoltage_since_boot"):
            found["pi:power"] = (
                "⚡ У Raspberry Pi была просадка питания. Проверьте блок питания и ИБП: "
                "от просадок портится карта памяти."
            )
        memory = _num(data.get("mem_avail_mb"))
        if memory is not None and memory < PI_MEMORY_LOW_MB:
            found["pi:memory"] = f"🧠 На Raspberry Pi почти кончилась память: {memory:.0f} МБ."
        where = "Raspberry Pi"
        disk_key = "pi:disk"
    for mount, pct in (data.get("disks") or {}).items():
        if _num(pct) is not None and pct < DISK_LOW_PCT:
            found[f"{disk_key}:{mount}"] = f"💾 На {where} мало места: {mount} свободно {pct:.0f}%."
    return found


def _runs_needed(key: str) -> int:
    parts = key.split(":")
    return RUNS.get(f"{parts[0]}:{parts[1]}", 3)


def _alerted(db: Session) -> set[str]:
    rows = db.scalars(select(KVState).where(KVState.key.like(f"{ALERT_KEY}%")))
    return {row.key.removeprefix(ALERT_KEY) for row in rows if row.value == "1"}


def _judge(host: str, recent: list[dict[str, Any]], alerted: set[str]) -> list[Change]:
    """Raise after a run of bad samples, clear once the newest ones are clean."""
    if not recent:
        return []
    found = [problems(host, data) for data in recent]
    keys = {key for p in found for key in p} | {k for k in alerted if k.startswith(f"{host}:")}
    out = []
    for key in sorted(keys):
        run = 0
        for p in reversed(found):
            if key not in p:
                break
            run += 1
        if key not in alerted and run >= _runs_needed(key):
            out.append(Change(ALERT_KEY + key, "1", found[-1][key]))
        clean = min(2, _runs_needed(key), len(found))
        if key in alerted and all(key not in p for p in found[-clean:]):
            out.append(Change(ALERT_KEY + key, "", f"✅ Снова в норме: {_cleared(key)}."))
    return out


def _cleared(key: str) -> str:
    parts = key.split(":", 2)
    where = "ПК" if parts[0] == PC else "Raspberry Pi"
    kind = parts[1]
    rest = parts[2] if len(parts) > 2 else ""
    if kind == "service":
        return f"{SERVICE_NAMES.get(rest, rest)} на ПК работает"
    if kind == "backup":
        return f"резервная копия {BACKUP_NAMES.get(rest, rest)} свежая"
    if kind == "disk":
        return f"место на {where} ({rest})"
    return {
        "cpu": f"температура процессора {where}",
        "nvme": "температура диска ПК",
        "memory": f"память {where}",
        "power": "питание Raspberry Pi",
        "tailscale": "ключ Tailscale",
    }.get(kind, key)


# ---------------------------------------------------------------- the PC going quiet


def _when(moment: datetime, now: datetime) -> str:
    return f"{moment:%H:%M}" if moment.date() == now.date() else f"{moment:%d.%m %H:%M}"


def _span(delta: timedelta) -> str:
    minutes = max(1, int(delta.total_seconds() // 60))
    if minutes < 120:
        return f"{minutes} мин"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} ч {minutes % 60} мин"
    return f"{hours // 24} дн. {hours % 24} ч"


def _parse(value: Any) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return moment.replace(tzinfo=None)


def readings(data: dict[str, Any]) -> str:
    """One line of the PC's state for a message."""
    parts = []
    if (cpu := _num(data.get("cpu_max_c"))) is not None:
        busy = _num(data.get("cpu_busy_pct"))
        parts.append(
            f"процессор {cpu:.0f} °C" + (f", загрузка {busy:.0f}%" if busy is not None else "")
        )
    if (gpu := _num(data.get("gpu_c"))) is not None:
        parts.append(f"видеокарта {gpu:.0f} °C")
    if (memory := _num(data.get("mem_avail_mb"))) is not None:
        parts.append(f"свободно памяти {comma(memory / 1024)} ГБ")
    if data.get("top_process"):
        parts.append(f"тяжелее всех: {data['top_process']}")
    return "; ".join(parts) or "показаний нет"


def _peak(history: Iterable[dict[str, Any]], field: str) -> float | None:
    values = [v for d in history if (v := _num(d.get(field))) is not None]
    return max(values) if values else None


def _pc_changes(db: Session, now: datetime) -> list[Change]:
    last = latest(db, PC)
    if last is None:
        return []
    received, data = last
    out = []
    silent_since = kv_get(db, SILENT_KEY, "")
    if now - received > SILENT_AFTER:
        if not silent_since:
            if data.get("stopping"):
                text = f"🖥 ПК выключен штатно в {_when(received, now)}."
            else:
                previous = [
                    d
                    for _, d in samples(db, PC, since=received - timedelta(minutes=10))
                    if not d.get("stopping")
                ]
                text = (
                    f"🔴 ПК не отвечает с {_when(received, now)}. Luna и локальные модели "
                    "недоступны.\nПоследние показания: "
                    f"{readings(previous[-1] if previous else data)}."
                )
                peak = _peak(previous, "cpu_max_c")
                if peak is not None:
                    text += f"\nМаксимум процессора за 10 минут до этого: {peak:.0f} °C."
            out.append(Change(SILENT_KEY, received.isoformat(), text))
        return out
    if silent_since:
        quiet = _parse(silent_since)
        span = f" (молчал {_span(received - quiet)})" if quiet else ""
        out.append(Change(SILENT_KEY, "", f"✅ ПК снова на связи{span}."))
    out += _boot_change(db, data, now)
    out += _kernel_change(db, data)
    out += _oom_change(db, data, now)
    return out


def _boot_change(db: Session, data: dict[str, Any], now: datetime) -> list[Change]:
    boot = data.get("boot") or {}
    boot_id = str(boot.get("id") or "")
    known = kv_get(db, BOOT_KEY, "")
    if not boot_id or boot_id == known:
        return []
    if not known or boot.get("previous_clean") is not False:
        return [Change(BOOT_KEY, boot_id, "")]  # first sight, or a clean or unknown stop
    last_moment = _parse(boot.get("previous_last"))
    started = _parse(boot.get("started"))
    text = "⚡ ПК выключился не штатно"
    text += f" около {_when(last_moment, now)}" if last_moment else ""
    text += f", снова включился в {_when(started, now)}." if started else "."
    before = [
        d
        for _, d in samples(db, PC, since=now - KEEP)
        if (d.get("boot") or {}).get("id") == known and not d.get("stopping")
    ]
    peak = _peak(before[-10:], "cpu_max_c")
    if before:
        text += f"\nПоследние показания перед этим: {readings(before[-1])}."
    if peak is not None:
        text += f"\nМаксимум процессора за 10 минут до выключения: {peak:.0f} °C."
        text += (
            "\nПроцессор был горячим: возможен перегрев."
            if peak >= CPU_HOT_C
            else "\nПерегрева не было: скорее питание (свет, розетка, блок питания)."
        )
    return [Change(BOOT_KEY, boot_id, text)]


def _counted_change(
    db: Session, data: dict[str, Any], key: str, field: str
) -> tuple[Change | None, int]:
    """A since-boot counter told once per increase: (silent bookkeeping or None, new events)."""
    count = data.get(field)
    boot_id = str((data.get("boot") or {}).get("id") or "")
    if not isinstance(count, int) or not boot_id:
        return None, 0
    known_boot, _, known_count = kv_get(db, key, "").partition(":")
    told = int(known_count) if known_boot == boot_id and known_count.isdigit() else 0
    value = f"{boot_id}:{count}"
    if count > told:
        return Change(key, value, ""), count - told
    # a new boot, or a counter that shrank because the monitor changed what it counts
    same = known_boot == boot_id and count == told
    return (None if same else Change(key, value, "")), 0


def _kernel_change(db: Session, data: dict[str, Any]) -> list[Change]:
    change, new = _counted_change(db, data, KERNEL_KEY, "kernel_errors")
    if change is None:
        return []
    if new:
        text = f"🧯 В журнале ядра ПК новые аппаратные ошибки: {new}."
        if data.get("kernel_error_last"):
            text += f"\nПоследняя: {data['kernel_error_last']}"
        change = replace(change, text=text)
    return [change]


def _count(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} {one}"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} {few}"
    return f"{n} {many}"


def _where(events: list[dict[str, Any]]) -> str:
    names = list(dict.fromkeys(str(e["session"]) for e in events if e.get("session")))
    if not names:
        return "одной из сессий Claude"
    if len(names) == 1:
        return f"сессии {names[0]}"
    return "сессиях " + ", ".join(names[:-1]) + f" и {names[-1]}"


def _oom_text(events: list[dict[str, Any]], new: int) -> tuple[str, bool]:
    """The note about new kills and whether it is urgent (Claude itself or the whole PC)."""
    whole = [e for e in events if e.get("whole_pc")]
    claude = [e for e in events if e.get("claude")]
    if whole:
        return (
            "🧠 Кончилась память у всего ПК, не только у одной сессии: система остановила "
            f"{whole[-1].get('process', 'процесс')}. Могли пострадать Luna и другие службы. "
            "Если повторится, стоит найти, что съедает память.",
            True,
        )
    if claude:
        return (
            f"🧠 В {_where(claude)} кончилась память, и система остановила сам Claude. "
            "Чтобы продолжить, перезапусти его: в окне этой сессии в tmux команда claude-session.",
            True,
        )
    stopped = _count(max(new, len(events)), "процесс", "процесса", "процессов")
    return (
        f"🧠 В {_where(events)} не хватило памяти на всё сразу: система остановила {stopped} "
        f"(последний: {events[-1].get('process', 'процесс')}). Обычно это тесты и сборки; Claude "
        "работает и запустит их заново, делать ничего не нужно.\n"
        "Если такое приходит каждый час, попроси сессию запускать не больше двух тяжёлых "
        "проверок одновременно.",
        False,
    )


def _oom_change(db: Session, data: dict[str, Any], now: datetime) -> list[Change]:
    change, new = _counted_change(db, data, OOM_KEY, "oom_kills")
    if change is None:
        return []
    if not new:
        return [change]
    events = [e for e in data.get("oom_events") or [] if isinstance(e, dict)][-new:]
    if not events:  # a monitor that does not tell sessions yet
        victim = data.get("oom_last") or "процесс"
        text = (
            f"🧠 На ПК кончилась память, и система остановила {victim}"
            + (f" (всего остановлено: {new})." if new > 1 else ".")
            + " Остальное работает; если повторяется, виновника стоит ограничить."
        )
        return [replace(change, text=text)]
    text, urgent = _oom_text(events, new)
    if urgent:
        return [replace(change, text=text)]
    last = _parse(kv_get(db, OOM_NOTE_KEY, ""))
    if last is not None and now - last < OOM_NOTE_EVERY:
        return []  # the told count stays, so the next note counts these kills too
    return [replace(change, text=text), Change(OOM_NOTE_KEY, now.isoformat(), "")]


# ---------------------------------------------------------------- one pass


def changes(db: Session, now: datetime | None = None) -> list[Change]:
    """Everything to tell the owner now. Silent changes (empty text) only move the state."""
    now = now or datetime.now()
    alerted = _alerted(db)
    out = _pc_changes(db, now)
    pc_recent = [(t, d) for t, d in samples(db, PC, limit=RECENT) if not d.get("stopping")]
    if pc_recent and now - pc_recent[-1][0] <= SILENT_AFTER:
        out += _judge(PC, [d for _, d in pc_recent], alerted)
    pi_recent = samples(db, PI, limit=RECENT)
    if pi_recent and now - pi_recent[-1][0] <= SILENT_AFTER:
        out += _judge(PI, [d for _, d in pi_recent], alerted)
    return out


def remember(db: Session, change: Change) -> None:
    kv_set(db, change.key[:64], change.value)


# ---------------------------------------------------------------- the owner's button


def _uptime(seconds: float) -> str:
    return _span(timedelta(seconds=seconds))


def _disks(data: dict[str, Any]) -> str:
    disks = data.get("disks") or {}
    return ", ".join(f"{mount} {pct:.0f}%" for mount, pct in disks.items()) or "нет данных"


Row = tuple[str, str]  # (label, value); an empty value makes a note line


def _pc_block(db: Session, now: datetime) -> tuple[str, list[Row]]:
    last = latest(db, PC)
    if last is None:
        return "🖥 ПК: данных ещё не было. Сборщик на ПК не запущен?", []
    received, data = last
    day = [d for _, d in samples(db, PC, since=now - timedelta(days=1)) if not d.get("stopping")]
    if now - received > SILENT_AFTER:
        title = f"🖥 ПК — 🔴 молчит с {_when(received, now)} ({_span(now - received)})"
        if data.get("stopping"):
            return title, [("Выключен штатно.", "")]
        return title, [("Последние показания", readings(day[-1]))] if day else []
    boot = data.get("boot") or {}
    started = _parse(boot.get("started"))
    title = "🖥 ПК — на связи" + (f", работает {_span(now - started)}" if started else "")
    rows: list[Row] = []
    cpu, peak = _num(data.get("cpu_max_c")), _peak(day, "cpu_max_c")
    if cpu is not None:
        busy = _num(data.get("cpu_busy_pct"))
        value = f"{cpu:.0f} °C"
        value += f" (за сутки макс. {peak:.0f})" if peak is not None else ""
        value += f", загрузка {busy:.0f}%" if busy is not None else ""
        if (load := _num(data.get("load1"))) is not None:
            value += f", очередь {comma(load)}"
        rows.append(("Процессор", value))
    if data.get("top_process"):
        rows.append(("Тяжелее всех", str(data["top_process"])))
    gpu, gpu_peak = _num(data.get("gpu_c")), _peak(day, "gpu_c")
    value = f"{gpu:.0f} °C" if gpu is not None else "спит"
    rows.append(
        (
            "Видеокарта",
            value + (f" (за сутки макс. {gpu_peak:.0f})" if gpu_peak is not None else ""),
        )
    )
    if (nvme := _num(data.get("nvme_c"))) is not None:
        rows.append(("Системный диск", f"{nvme:.0f} °C"))
    rows.append(("Свободно места", _disks(data)))
    memory = _num(data.get("mem_avail_mb"))
    if memory is not None:
        low = [v for d in day if (v := _num(d.get("mem_avail_mb"))) is not None]
        value = f"свободно {comma(memory / 1024)} ГБ"
        value += f" (за сутки мин. {comma(min(low) / 1024)})" if low else ""
        if (swap := _num(data.get("swap_used_mb"))) is not None:
            value += f", подкачка {comma(swap / 1024)} ГБ"
        rows.append(("Память", value))
    down = data.get("services_down") or []
    rows.append(
        (
            "Сервисы",
            "все работают"
            if not down
            else "⚠️ не работает " + ", ".join(SERVICE_NAMES.get(s, s) for s in down),
        )
    )
    ages = data.get("backup_age_h") or {}
    if ages:
        parts = [
            f"{BACKUP_NAMES.get(n, n)} {'нет' if a is None else f'{a:.0f} ч назад'}"
            for n, a in ages.items()
        ]
        rows.append(("Резервные копии", ", ".join(parts)))
    if (days := _num(data.get("tailscale_days"))) is not None:
        rows.append(("Ключ Tailscale", f"ещё {days:.0f} дн."))
    errors = data.get("kernel_errors")
    if isinstance(errors, int):
        rows.append(("Аппаратные ошибки ядра", "нет" if errors == 0 else f"⚠️ {errors} с загрузки"))
    kills = data.get("oom_kills")
    if isinstance(kills, int) and kills:
        rows.append(("Остановлено из-за нехватки памяти", f"⚠️ {kills} с загрузки"))
    return title, rows


def _pi_block(db: Session, now: datetime) -> tuple[str, list[Row]]:
    last = latest(db, PI)
    if last is None:
        return "🍓 Raspberry Pi: данных ещё нет.", []
    _, data = last
    day = [d for _, d in samples(db, PI, since=now - timedelta(days=1))]
    rows: list[Row] = []
    if (cpu := _num(data.get("cpu_c"))) is not None:
        peak = _peak(day, "cpu_c")
        value = f"{cpu:.0f} °C" + (f" (за сутки макс. {peak:.0f})" if peak is not None else "")
        rows.append(("Температура", value))
    if "undervoltage_since_boot" in data:
        rows.append(
            (
                "Питание",
                "⚠️ была просадка" if data["undervoltage_since_boot"] else "в норме",
            )
        )
    rows.append(("Карта", f"свободно {_disks(data)}"))
    if (memory := _num(data.get("mem_avail_mb"))) is not None:
        rows.append(("Память", f"{memory:.0f} МБ свободно"))
    title = "🍓 Raspberry Pi"
    if (uptime := _num(data.get("uptime_s"))) is not None:
        title += f" — работает {_uptime(uptime)}"
    return title, rows


def status_blocks(db: Session, now: datetime | None = None) -> list[tuple[str, list[Row]]]:
    """Both machines as (title, rows): the bot draws them as tables, tests read them as text."""
    now = now or datetime.now()
    return [_pc_block(db, now), _pi_block(db, now)]


def status_text(db: Session, now: datetime | None = None) -> str:
    blocks = []
    for title, rows in status_blocks(db, now):
        lines = [title] + [f"{label}: {value}" if value else label for label, value in rows]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)
