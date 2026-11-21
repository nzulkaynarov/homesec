"""Parents' report screens as rich messages: tables instead of columns of text.

The plain-text versions in texts.py and services stay: AI tools and tests read them, and
rich.send falls back to text when Telegram refuses a rich message.
"""

from datetime import datetime
from typing import Any

from ..models import FamilyNote
from . import texts
from .rich import esc, heading, paragraph, table


def quota_html(items: list[dict]) -> str:
    if not items:
        return paragraph("⏳ На сегодня правил экранного времени не назначено.")
    parts = [heading("⏳ Экранное время сегодня")]
    for item in items:
        title = item["device"]
        if item.get("owner"):
            title = f"{item['owner']} ({title})"
        rows = []
        for row in item["rows"]:
            used, limit = row["used"], row["limit"]
            left = max(0, limit - used)
            rows.append(
                (
                    row["label"],
                    f"{texts.progress_bar(used, limit)} {texts.hhmm(used)}/{texts.hhmm(limit)}",
                    f"{left} мин" if left > 0 else "⛔ всё",
                )
            )
        parts.append(table(rows, header=("", "Сегодня", "Осталось"), caption=f"📱 {title}"))
    return "".join(parts)


def status_html(s: dict) -> str:
    rows = [
        ("Роутер MikroTik", texts.yes_no(s["router_ok"])),
        ("AdGuard Home", texts.yes_no(s["adguard_ok"])),
        ("Устройств", f"{s['devices_total']}, онлайн {s['devices_online']}"),
        ("Заблокировано вручную", str(s["devices_blocked"])),
        ("Неизвестных", str(s["devices_unknown"])),
    ]
    if s.get("dns_queries_today") is not None:
        rows.append(
            (
                "DNS сегодня",
                f"{s['dns_queries_today']} запросов, {s.get('dns_blocked_today', 0)} заблокировано",
            )
        )
    parts = [heading("🛡 Состояние HomeSec"), table(rows)]
    pauses = s.get("active_pauses", [])
    if pauses:
        parts.append(
            table(
                ((p["target"], p["until"][11:16] or p["until"]) for p in pauses),
                header=("Кто", "до"),
                caption="⏸ Паузы",
            )
        )
    screen = s.get("screen_time", [])
    if screen:
        parts.append(
            table(
                (
                    (
                        q["device"],
                        q["label"].lower(),
                        f"{texts.hhmm(q['used_minutes'])}/{texts.hhmm(q['limit_minutes'])}",
                    )
                    for q in screen
                ),
                header=("Устройство", "Что", "Сегодня"),
                caption="⏳ Экранное время",
            )
        )
    return "".join(parts)


def usage_html(
    today: dict,
    week_tokens: int,
    budget_left: int | None,
    gateway: str,
    spend: list[tuple[str, float, int, dict]],
    week_usd: float,
) -> str:
    in_tok, out_tok = today.get("input", 0), today.get("output", 0)
    tokens = [
        ("Запросов", f"{today.get('requests', 0)}"),
        ("Токенов на входе", f"{in_tok:,}"),
        ("Токенов на выходе", f"{out_tok:,}"),
        ("Всего сегодня", f"{in_tok + out_tok:,}"),
        (
            "Остаток бюджета",
            f"{budget_left:,}" if budget_left is not None else "без ограничений",
        ),
        ("За 7 дней", f"{week_tokens:,}"),
    ]
    parts = [heading("🤖 Расход ИИ сегодня"), table(tokens, caption="Токены")]
    rows = []
    for name, usd, requests, features in spend:
        top = sorted(features.items(), key=lambda item: item[1], reverse=True)
        detail = ", ".join(f"{texts.FEATURE_LABELS.get(k, k)} ${v:.2f}" for k, v in top)
        rows.append((name, f"${usd:.2f}", str(requests), detail))
    if rows:
        rows.append(("Итого", f"${sum(r[1] for r in spend):.2f}", "", ""))
        parts.append(
            table(rows, header=("Кто", "$", "Запросов", "На что"), caption="💵 Деньги (оценка)")
        )
    parts.append(paragraph(f"За 7 дней: ${week_usd:.2f}"))
    if gateway:
        parts.append(f"<footer>Шлюз: {esc(gateway)}</footer>")
    return "".join(parts)


def presence_html(status: dict[str, Any]) -> str:
    parts = [heading("🏠 Кто дома")]
    if not status.get("router_answered", True):
        parts.append(paragraph("⚠️ Роутер не ответил, сейчас не могу сказать, кто дома."))
        return "".join(parts)
    family = []
    for p in status.get("people", []):
        person = p["person"]
        icon = "🎒" if person.role == "kid" else "👤"
        if p["is_home"]:
            where = "🟢 дома"
            devices = ", ".join(p["active_devices"])
        else:
            where, devices = "⚪ не в сети", ""
        family.append((f"{icon} {person.name}", where, devices))
    if family:
        parts.append(table(family, caption="👥 Семья"))
    shared = []
    for s in status.get("shared", []):
        state = "🟢 онлайн" if s["is_online"] else "⚪ выкл"
        if s["blocked"]:
            state += ", ⛔ заблокирован"
        shared.append((s["device"].name, state))
    if shared:
        parts.append(table(shared, caption="📺 Общие устройства"))
    if status.get("iot_total", 0) > 0:
        parts.append(
            paragraph(
                f"🤖 Умный дом: {status.get('iot_online', 0)} из {status['iot_total']} в сети."
            )
        )
    checked_at = status.get("checked_at")
    if isinstance(checked_at, datetime):
        parts.append(f"<footer>Проверено в {checked_at:%H:%M:%S}</footer>")
    return "".join(parts)


def notes_html(notes: list[FamilyNote]) -> str:
    if not notes:
        return paragraph(
            "📝 В семейной памяти пока пусто. Напишите боту: «Запомни: размер ноги Миши 36»."
        )
    items = []
    for n in notes:
        author = f" ({esc(n.author)})" if n.author else ""
        items.append(f"<li>{esc(n.content)}{author} — <i>{n.created:%d.%m %H:%M}</i></li>")
    return heading("📝 Семейная память") + "<ol>" + "".join(items) + "</ol>"
