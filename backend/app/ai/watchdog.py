"""Агент-сторож: дешёвые эвристики без LLM ищут аномалии (ночная активность
детских устройств, всплеск DNS-запросов к DoH-доменам = попытка обхода
фильтра). LLM подключается только когда эвристика сработала — чтобы оформить
человеческий алерт. Повторные алерты глушатся через kv_state."""

import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Device, kv_get, kv_set
from ..services import adguard
from . import client

log = logging.getLogger("homesec.ai.watchdog")

NIGHT_HOURS = range(0, 6)  # 00:00–05:59
NIGHT_MIN_QUERIES = 15  # столько DNS-запросов за окно = устройство реально активно
DOH_SPIKE_THRESHOLD = 10  # запросов к DoH-доменам за окно

# Домены публичных DoH-резолверов: запрос к ним = клиент ищет обход фильтра
DOH_DOMAINS = (
    "dns.google", "cloudflare-dns.com", "one.one.one.one", "dns.quad9.net",
    "doh.opendns.com", "dns.adguard-dns.com", "mozilla.cloudflare-dns.com",
    "chrome.cloudflare-dns.com", "freedns.controld.com", "dns.sb",
)

ALERT_SYSTEM = """Ты — ИИ-сторож домашней сети HomeSec. Тебе дают сухое описание
аномалии. Напиши короткий (2–4 строки) человеческий алерт для родителей в
Telegram по-русски: что случилось, почему это может быть важно, что можно
сделать (например /pause или /block). Без паники и без markdown."""

_MUTE_HOURS = 6  # один и тот же алерт — не чаще раза в 6 часов


def _muted(db: Session, key: str, now: datetime) -> bool:
    raw = kv_get(db, f"watchdog_mute:{key}", "")
    if raw:
        try:
            if now - datetime.fromisoformat(raw) < timedelta(hours=_MUTE_HOURS):
                return True
        except ValueError:
            pass
    kv_set(db, f"watchdog_mute:{key}", now.isoformat())
    return False


def find_anomalies(db: Session, now: datetime | None = None) -> list[str]:
    """Эвристики без LLM. Возвращает сухие описания сработавших аномалий."""
    now = now or datetime.now()
    try:
        entries = adguard.get_query_log(limit=500)
    except adguard.AdGuardError:
        return []  # без журнала DNS смотреть не на что

    devices = {d.ip: d for d in db.scalars(select(Device)) if d.ip}
    per_device: dict[str, int] = {}
    doh_per_device: dict[str, int] = {}
    for entry in entries:
        ip = entry.get("client", "")
        if ip not in devices:
            continue
        per_device[ip] = per_device.get(ip, 0) + 1
        domain = ((entry.get("question") or {}).get("name", "")).lower().rstrip(".")
        if any(domain == d or domain.endswith("." + d) for d in DOH_DOMAINS):
            doh_per_device[ip] = doh_per_device.get(ip, 0) + 1

    anomalies = []
    if now.hour in NIGHT_HOURS:
        for ip, count in per_device.items():
            dev = devices[ip]
            if dev.group == "kid" and count >= NIGHT_MIN_QUERIES:
                if not _muted(db, f"night:{dev.mac}:{now:%Y-%m-%d}", now):
                    anomalies.append(
                        f"Ночная активность: устройство «{dev.name}» ({ip}, ребёнок) "
                        f"сделало {count} DNS-запросов в {now:%H:%M}."
                    )
    for ip, count in doh_per_device.items():
        dev = devices[ip]
        if count >= DOH_SPIKE_THRESHOLD:
            if not _muted(db, f"doh:{dev.mac}", now):
                anomalies.append(
                    f"Попытка обхода фильтра: устройство «{dev.name}» ({ip}, "
                    f"группа {dev.group}) сделало {count} запросов к DoH-серверам "
                    "(DNS поверх HTTPS) — так обходят родительский контроль."
                )
    return anomalies


def format_alert(db: Session, anomaly: str) -> str:
    """LLM-оформление алерта (дешёвая модель); без ключа — сухой текст как есть."""
    if not client.is_configured():
        return f"🕵️ {anomaly}"
    try:
        response = client.ask(
            db,
            system=ALERT_SYSTEM,
            messages=[{"role": "user", "content": anomaly}],
            model=settings.ai_model_fast,  # рутинное оформление — дешёвая модель
            max_tokens=1024,
        )
    except client.AiError as e:
        log.warning("алерт без LLM: %s", e)
        return f"🕵️ {anomaly}"
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    return text or f"🕵️ {anomaly}"


def check(db: Session, now: datetime | None = None) -> list[str]:
    """Полный цикл: эвристики -> оформленные алерты для отправки."""
    return [format_alert(db, a) for a in find_anomalies(db, now)]
