"""Агент-аналитик: ежедневный дайджест сети для Telegram. Сначала собирает
факты кодом (журнал событий, статистика AdGuard, топ доменов детских
устройств), потом просит модель превратить их в короткий человеческий текст.
Без ключа API отдаёт простой текстовый дайджест без LLM — фичи деградируют,
но не пропадают."""

import json
import logging
from collections import Counter
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Device, EventLog, Person, QuotaUsage
from ..services import adguard
from ..services.quota import QUOTA_CATEGORY_LABELS
from . import client

log = logging.getLogger("homesec.ai.analyst")

DIGEST_SYSTEM = """Ты — ИИ-помощник домашней сети HomeSec. Раз в день ты пишешь
родителям короткий дайджест в Telegram о том, что происходило в сети.

Правила:
- пиши по-русски, простым языком, без технического жаргона;
- 6–12 строк, без markdown (плоский текст, можно эмодзи в меру);
- начни с общего состояния (всё ли работает), затем самое интересное:
  активность детских устройств, заблокированные попытки, новые устройства,
  ошибки. Если день скучный — так и скажи, коротко;
- не выдумывай ничего, чего нет в данных."""


def collect_digest_data(db: Session) -> dict:
    """Факты за последние 24 часа — собираются кодом, не моделью."""
    since = datetime.now() - timedelta(days=1)
    events = list(
        db.scalars(select(EventLog).where(EventLog.ts >= since).order_by(EventLog.ts))
    )
    by_kind: dict[str, int] = Counter(e.kind for e in events)

    stats: dict = {}
    try:
        stats = adguard.get_stats()
    except adguard.AdGuardError:
        pass

    devices = list(db.scalars(select(Device)))

    # Экранное время за день (заявлено в ТЗ фазы 2): активные минуты из
    # QuotaUsage за сегодня, {устройство: {категория: минуты}}
    names = {d.id: d.name for d in devices}
    screen_time: dict[str, dict[str, int]] = {}
    today = datetime.now().strftime("%Y-%m-%d")
    for r in db.scalars(select(QuotaUsage).where(QuotaUsage.date == today)):
        name = names.get(r.device_id)
        if name is None:
            continue
        label = QUOTA_CATEGORY_LABELS.get(r.category, r.category)
        per = screen_time.setdefault(name, {})
        per[label] = per.get(label, 0) + r.minutes

    kid_ips = {d.ip: d.name for d in devices if d.ip and d.group == "kid"}
    kid_domains: dict[str, Counter] = {name: Counter() for name in kid_ips.values()}
    try:
        for entry in adguard.get_query_log(limit=1000):
            name = kid_ips.get(entry.get("client", ""))
            domain = (entry.get("question") or {}).get("name", "")
            if name and domain:
                kid_domains[name][domain] += 1
    except adguard.AdGuardError:
        pass

    return {
        "date": datetime.now().strftime("%d.%m.%Y"),
        "events_by_kind": dict(by_kind),
        "new_devices": [e.message for e in events if e.kind == "device_new"],
        "blocks": [e.message for e in events if e.kind in ("block", "ai_action", "bot_action")],
        "errors": [e.message for e in events if e.kind == "error"][-5:],
        "dns_queries_today": stats.get("num_dns_queries"),
        "dns_blocked_today": stats.get("num_blocked_filtering"),
        "screen_time": screen_time,
        "kid_ai_requests": _kid_ai_requests(db),
        "kid_safety_alerts": by_kind.get("kid_safety", 0),
        "kid_top_domains": {
            name: [d for d, _ in counter.most_common(10)]
            for name, counter in kid_domains.items()
        },
    }


def _kid_ai_requests(db: Session) -> dict[str, int]:
    """How many AI requests each child made today."""
    ledger = client.ledger_for_day(db)
    kids = db.scalars(select(Person).where(Person.role == "kid"))
    return {
        kid.name: ledger[f"person:{kid.id}"]["requests"]
        for kid in kids
        if f"person:{kid.id}" in ledger
    }


def _fallback_digest(data: dict) -> str:
    lines = [f"Дайджест HomeSec за {data['date']} (ИИ выключен, краткая сводка)"]
    if data["dns_queries_today"] is not None:
        lines.append(
            f"DNS: {data['dns_queries_today']} запросов, "
            f"{data.get('dns_blocked_today', 0)} заблокировано"
        )
    for name, cats in data.get("screen_time", {}).items():
        spent = ", ".join(f"{label.lower()} {minutes} мин" for label, minutes in cats.items())
        lines.append(f"⏳ {name}: {spent}")
    for msg in data["new_devices"]:
        lines.append(f"🆕 {msg}")
    for msg in data["blocks"][-5:]:
        lines.append(f"⛔ {msg}")
    for name, requests in data.get("kid_ai_requests", {}).items():
        lines.append(f"🎓 {name}: запросов к репетитору и викторине {requests}")
    if data.get("kid_safety_alerts"):
        lines.append(f"🚨 Обращений ребёнка, требующих внимания: {data['kid_safety_alerts']}")
    if data["errors"]:
        lines.append(f"⚠️ Ошибок за сутки: {len(data['errors'])}")
    if len(lines) == 1:
        lines.append("Спокойный день: событий не было.")
    return "\n".join(lines)


def daily_digest(db: Session) -> str:
    """Текст дайджеста для отправки в Telegram."""
    data = collect_digest_data(db)
    if not client.is_configured():
        return _fallback_digest(data)
    try:
        with client.attribute(client.Caller(feature="digest")):
            response = client.ask_with_fallback(
                db,
                client.model_ladder(settings.ai_model, settings.ai_model_support),
                system=DIGEST_SYSTEM,
                messages=[{
                    "role": "user",
                    "content": "Данные за сутки в JSON:\n" + json.dumps(data, ensure_ascii=False),
                }],
                max_tokens=2048,
                thinking=True,
            )
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        return text or _fallback_digest(data)
    except client.AiError as e:
        log.warning("дайджест без LLM: %s", e)
        return _fallback_digest(data)
