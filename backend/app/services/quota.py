"""Квоты времени. Учёт — по DNS-активности из журнала AdGuard (приближение,
согласованное в docs/07-phase2-tz.md): минута активна, если устройство сделало
достаточно запросов (интернет) или обратилось к домену категории.

Учёт (record_activity) гоняет планировщик панели раз в минуту; применение
(exhausted) читает счётчики в _desired_state ядра. Бонусы расширяют лимит
на конкретный день."""

import logging
import re
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Device, Quota, QuotaBonus, QuotaUsage, kv_get, kv_set
from . import adguard

log = logging.getLogger("homesec.quota")

QUOTA_CATEGORIES = ("internet", "games", "video", "social")
QUOTA_CATEGORY_LABELS = {
    "internet": "Интернет целиком",
    "games": "Игры",
    "video": "YouTube и видео",
    "social": "Соцсети и мессенджеры",
}

# Минута считается «активной» для квоты на интернет при таком числе запросов;
# фоновые пинги ОС порог не пробивают.
INTERNET_ACTIVE_THRESHOLD = 5

# Домены-маркеры категорий (суффиксное совпадение). Стартовый список —
# дополняется по опыту эксплуатации. Категории совпадают с SERVICE_CATEGORIES.
CATEGORY_DOMAINS: dict[str, tuple[str, ...]] = {
    "games": (
        "roblox.com", "rbxcdn.com", "steampowered.com", "steamcontent.com",
        "steamcommunity.com", "epicgames.com", "minecraft.net", "mojang.com",
        "minecraftservices.com", "battle.net", "ea.com", "playstation.net",
        "xboxlive.com", "riotgames.com", "wargaming.net", "supercell.com",
        "brawlstarsgame.com", "miniclip.com",
    ),
    "video": (
        "youtube.com", "ytimg.com", "googlevideo.com", "youtu.be",
        "netflix.com", "nflxvideo.net", "twitch.tv", "ttvnw.net",
        "kick.com", "vimeo.com", "hulu.com", "ivi.ru", "kinopoisk.ru",
    ),
    "social": (
        "tiktok.com", "tiktokcdn.com", "tiktokv.com", "instagram.com",
        "cdninstagram.com", "facebook.com", "fbcdn.net", "snapchat.com",
        "sc-cdn.net", "discord.com", "discord.gg", "discordapp.com",
        "discordapp.net", "reddit.com", "redd.it", "9gag.com",
        "vk.com", "vk.ru", "userapi.com",
    ),
}

_TICK_GUARD_KEY = "quota_tick_ts"
_MIN_TICK_GAP = 30  # сек: защита от двойного учёта при частых вызовах
_WINDOW = 90  # сек: окно «сейчас активен» (тик раз в минуту + запас)


def domain_category(domain: str) -> str | None:
    d = domain.lower().rstrip(".")
    for category, suffixes in CATEGORY_DOMAINS.items():
        for s in suffixes:
            if d == s or d.endswith("." + s):
                return category
    return None


_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(.*)$")


def _parse_ts(raw: str) -> datetime | None:
    """RFC3339 из AdGuard (наносекунды!) -> локальное наивное время."""
    m = _TS_RE.match(raw.strip())
    if not m:
        return None
    frac = (m.group(2) or "0")[:6].ljust(6, "0")
    try:
        dt = datetime.fromisoformat(f"{m.group(1)}.{frac}{m.group(3) or ''}")
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


def _bump(db: Session, device_id: int, date: str, category: str) -> None:
    row = db.scalar(
        select(QuotaUsage).where(
            QuotaUsage.device_id == device_id,
            QuotaUsage.date == date,
            QuotaUsage.category == category,
        )
    )
    if row is None:
        db.add(QuotaUsage(device_id=device_id, date=date, category=category, minutes=1))
    else:
        row.minutes += 1


def record_activity(db: Session, now: datetime | None = None) -> dict[int, set[str]]:
    """Один тик учёта: +1 активная минута устройствам, активным в последнем
    окне. Возвращает {device_id: категории}, пустой dict если тик пропущен."""
    now = now or datetime.now()
    raw = kv_get(db, _TICK_GUARD_KEY, "")
    if raw:
        try:
            if (now - datetime.fromisoformat(raw)).total_seconds() < _MIN_TICK_GAP:
                return {}
        except ValueError:
            pass
    kv_set(db, _TICK_GUARD_KEY, now.isoformat())

    try:
        entries = adguard.get_query_log(limit=500)
    except adguard.AdGuardError:
        return {}

    devices = {d.ip: d for d in db.scalars(select(Device)) if d.ip}
    window_start = now - timedelta(seconds=_WINDOW)
    counts: dict[int, int] = {}
    cats: dict[int, set[str]] = {}
    for entry in entries:
        dev = devices.get(entry.get("client", ""))
        if dev is None:
            continue
        ts = _parse_ts(entry.get("time", "") or "")
        if ts is None or ts < window_start or ts > now + timedelta(seconds=5):
            continue
        counts[dev.id] = counts.get(dev.id, 0) + 1
        category = domain_category((entry.get("question") or {}).get("name", ""))
        if category:
            cats.setdefault(dev.id, set()).add(category)

    date = now.strftime("%Y-%m-%d")
    active: dict[int, set[str]] = {}
    for dev_id, n in counts.items():
        marked = set(cats.get(dev_id, set()))
        if n >= INTERNET_ACTIVE_THRESHOLD:
            marked.add("internet")
        for category in marked:
            _bump(db, dev_id, date, category)
        if marked:
            active[dev_id] = marked
    db.commit()
    return active


# ---------- применение ----------

def _active_today(q: Quota, now: datetime) -> bool:
    if not q.enabled:
        return False
    days = {d for d in q.days.split(",") if d.strip() != ""}
    return str(now.weekday()) in days


def _quota_for(quotas: list[Quota], dev: Device, category: str) -> Quota | None:
    """Квота категории для устройства; личная квота важнее групповой."""
    dev_q = [q for q in quotas if q.category == category
             and q.target_type == "device" and q.target == str(dev.id)]
    if dev_q:
        return min(dev_q, key=lambda q: q.minutes_per_day)
    grp_q = [q for q in quotas if q.category == category
             and q.target_type == "group" and q.target == dev.group]
    return min(grp_q, key=lambda q: q.minutes_per_day) if grp_q else None


def _bonus_minutes(bonuses: list[QuotaBonus], dev: Device, category: str) -> int:
    return sum(
        b.minutes for b in bonuses
        if b.category == category and (
            (b.target_type == "device" and b.target == str(dev.id))
            or (b.target_type == "group" and b.target == dev.group)
        )
    )


def _usage_map(db: Session, date: str) -> dict[tuple[int, str], int]:
    out: dict[tuple[int, str], int] = {}
    for r in db.scalars(select(QuotaUsage).where(QuotaUsage.date == date)):
        key = (r.device_id, r.category)
        out[key] = out.get(key, 0) + r.minutes
    return out


def progress(
    db: Session, devices: list[Device], now: datetime | None = None
) -> dict[int, list[dict]]:
    """Для панели/инструментов: {device_id: [{category, label, used, limit}]}.
    Пусто у устройств без квот на сегодня."""
    now = now or datetime.now()
    date = now.strftime("%Y-%m-%d")
    quotas = [q for q in db.scalars(select(Quota)) if _active_today(q, now)]
    if not quotas:
        return {}
    bonuses = list(db.scalars(select(QuotaBonus).where(QuotaBonus.date == date)))
    usage = _usage_map(db, date)

    out: dict[int, list[dict]] = {}
    for dev in devices:
        rows = []
        for category in QUOTA_CATEGORIES:
            q = _quota_for(quotas, dev, category)
            if q is None:
                continue
            limit = q.minutes_per_day + _bonus_minutes(bonuses, dev, category)
            rows.append({
                "category": category,
                "label": QUOTA_CATEGORY_LABELS[category],
                "used": usage.get((dev.id, category), 0),
                "limit": limit,
            })
        if rows:
            out[dev.id] = rows
    return out


def exhausted(
    db: Session, devices: list[Device], now: datetime | None = None
) -> dict[int, set[str]]:
    """{device_id: категории с исчерпанной квотой} — для _desired_state."""
    out: dict[int, set[str]] = {}
    for dev_id, rows in progress(db, devices, now).items():
        spent = {r["category"] for r in rows if r["used"] >= r["limit"]}
        if spent:
            out[dev_id] = spent
    return out
