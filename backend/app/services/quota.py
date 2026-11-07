"""Квоты времени. Учёт — по DNS-активности из журнала AdGuard (приближение,
согласованное в docs/07-phase2-tz.md): минута активна, если устройство сделало
достаточно запросов (интернет) или обратилось к домену категории.

Учёт (record_activity) гоняет планировщик панели раз в минуту; применение
(exhausted) читает счётчики в _desired_state ядра. Бонусы расширяют лимит
на конкретный день."""

import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Device, Quota, QuotaBonus, QuotaUsage, kv_get, kv_set, log_event
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


def _parse_ts(raw: str) -> datetime | None:
    """RFC3339 из AdGuard (наносекунды!) -> локальное наивное время.
    Разбор живёт в adguard.py — это его формат; здесь остаётся тонкая обёртка,
    чтобы тесты по-прежнему могли её подменять."""
    return adguard.parse_ts(raw)


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
    prev_tick: datetime | None = None
    if raw:
        try:
            prev_tick = datetime.fromisoformat(raw)
        except ValueError:
            prev_tick = None
        if prev_tick is not None and (now - prev_tick).total_seconds() < _MIN_TICK_GAP:
            return {}
    kv_set(db, _TICK_GUARD_KEY, now.isoformat())

    try:
        entries = adguard.get_query_log(limit=500)
    except adguard.AdGuardError:
        return {}

    devices = {d.ip: d for d in db.scalars(select(Device)) if d.ip}
    # Окно — «с прошлого тика», а не фиксированные 90 секунд: при тике раз в
    # 60с фиксированное окно перекрывалось соседним на ~30с, и записи из зоны
    # перекрытия считались ДВАЖДЫ (короткий всплеск активности сжигал две
    # минуты квоты вместо одной). _WINDOW остаётся верхней границей — запас на
    # случай, если тик опоздал или панель перезапускалась.
    window_start = now - timedelta(seconds=_WINDOW)
    if prev_tick is not None and prev_tick > window_start:
        window_start = prev_tick
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
    """Квота категории для устройства; приоритет: личная устройства > человека > групповая."""
    dev_q = [q for q in quotas if q.category == category
             and q.target_type == "device" and q.target == str(dev.id)]
    if dev_q:
        return min(dev_q, key=lambda q: q.minutes_per_day)
    if dev.person_id is not None:
        person_q = [q for q in quotas if q.category == category
                    and q.target_type == "person" and q.target == str(dev.person_id)]
        if person_q:
            return min(person_q, key=lambda q: q.minutes_per_day)
    grp_q = [q for q in quotas if q.category == category
             and q.target_type == "group" and q.target == dev.group]
    return min(grp_q, key=lambda q: q.minutes_per_day) if grp_q else None


def _person_devices_map(db: Session, person_ids: set[int]) -> dict[int, set[int]]:
    if not person_ids:
        return {}
    out: dict[int, set[int]] = {}
    for pid, did in db.execute(
        select(Device.person_id, Device.id).where(Device.person_id.in_(person_ids))
    ):
        if pid is not None:
            out.setdefault(pid, set()).add(did)
    return out


def _bonus_minutes(
    bonuses: list[QuotaBonus],
    dev: Device,
    category: str,
    peer_dev_ids: set[int] | None = None,
    is_person_or_group: bool = False,
) -> int:
    peer_keys = {str(did) for did in peer_dev_ids} if peer_dev_ids else {str(dev.id)}
    dev_key = str(dev.id)
    person_key = str(dev.person_id) if dev.person_id is not None else None

    total = 0
    for b in bonuses:
        if b.category != category:
            continue
        if b.target_type == "group" and b.target == dev.group:
            total += b.minutes
        elif b.target_type == "person" and person_key is not None and b.target == person_key:
            total += b.minutes
        elif b.target_type == "device":
            if is_person_or_group:
                if b.target in peer_keys:
                    total += b.minutes
            else:
                if b.target == dev_key:
                    total += b.minutes
    return total


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
    Пусто у устройств без квот на сегодня. Для квот группы/человека экранное
    время суммируется по всем устройствам владельца, закрывая лазейку смены гаджета."""
    now = now or datetime.now()
    date = now.strftime("%Y-%m-%d")
    quotas = [q for q in db.scalars(select(Quota)) if _active_today(q, now)]
    if not quotas:
        return {}
    bonuses = list(db.scalars(select(QuotaBonus).where(QuotaBonus.date == date)))
    usage = _usage_map(db, date)

    person_ids = {d.person_id for d in devices if d.person_id is not None}
    person_devices = _person_devices_map(db, person_ids)

    out: dict[int, list[dict]] = {}
    for dev in devices:
        rows = []
        peer_ids = (
            person_devices.get(dev.person_id, {dev.id})
            if dev.person_id is not None
            else {dev.id}
        )
        for category in QUOTA_CATEGORIES:
            q = _quota_for(quotas, dev, category)
            if q is None:
                continue
            is_shared = q.target_type in ("group", "person") and dev.person_id is not None
            limit = q.minutes_per_day + _bonus_minutes(
                bonuses, dev, category, peer_ids, is_person_or_group=is_shared
            )
            if is_shared:
                used = sum(usage.get((did, category), 0) for did in peer_ids)
            else:
                used = usage.get((dev.id, category), 0)
            rows.append({
                "category": category,
                "label": QUOTA_CATEGORY_LABELS[category],
                "used": used,
                "limit": limit,
            })
        if rows:
            out[dev.id] = rows
    return out


def check_quota_warnings(
    db: Session, devices: list[Device] | None = None, now: datetime | None = None
) -> list[dict]:
    """Проверяет квоты на порог 80%. Пишет событие quota_warn (с антидребезгом
    через kv_state), чтобы родитель получил предупреждение в Telegram до
    жёсткой блокировки."""
    now = now or datetime.now()
    date = now.strftime("%Y-%m-%d")
    if devices is None:
        devices = list(db.scalars(select(Device)))
    prog = progress(db, devices, now)
    if not prog:
        return []

    dev_by_id = {d.id: d for d in devices}
    warned = []
    for dev_id, rows in prog.items():
        dev = dev_by_id.get(dev_id)
        if dev is None or not dev.ip:
            continue
        for r in rows:
            used = r["used"]
            limit = r["limit"]
            if limit <= 0:
                continue
            if used >= 0.8 * limit and used < limit:
                category = r["category"]
                cat_label = r["label"]
                target_key = f"{dev.person_id or dev.id}"
                guard_key = f"quota_warn_{date}_{target_key}_{category}"
                if kv_get(db, guard_key, "") != "":
                    continue
                kv_set(db, guard_key, "1")
                remaining = limit - used
                pct = int((used / limit) * 100)
                msg = (
                    f"Квота «{cat_label}» использована на {pct}%: "
                    f"{dev.name} ({dev.mac}, {dev.ip}) — осталось {remaining} мин."
                )
                log_event(db, "quota_warn", msg)
                warned.append({"device": dev, "category": category, "remaining": remaining})
    return warned


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
