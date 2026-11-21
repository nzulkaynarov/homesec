"""Parents' report screens as rich messages: tables instead of columns of text.

The plain-text versions in texts.py and services stay: AI tools and tests read them, and
rich.send falls back to text when Telegram refuses a rich message.
"""

from collections.abc import Sequence
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


def _works(ok: bool) -> str:
    return "✅ работает" if ok else "❌ не отвечает"


def status_html(s: dict, paused: Sequence[tuple[str, datetime]] = ()) -> str:
    """The network in plain words; paused is pauses.active(): who is off the internet, by name."""
    rows = [
        ("Роутер", _works(s["router_ok"])),
        ("Фильтр сайтов", _works(s["adguard_ok"])),
        ("Устройств", f"{s['devices_total']}, в сети {s['devices_online']}"),
        ("Заблокировано вручную", str(s["devices_blocked"])),
        ("Незнакомых", str(s["devices_unknown"])),
    ]
    if s.get("dns_queries_today") is not None:
        rows.append(
            (
                "Обращений к сайтам сегодня",
                f"{s['dns_queries_today']}, заблокировано {s.get('dns_blocked_today', 0)}",
            )
        )
    parts = [heading("🛡 Состояние сети"), table(rows)]
    if paused:
        parts.append(
            table(
                ((name, f"{until:%H:%M}") for name, until in paused),
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
    spend: list[tuple[str, float, int, dict]], week_usd: float, used_pct: int | None = None
) -> str:
    """The AI spend in money: who and on what today, the week, and how much of the daily limit
    is used (None when there is no limit)."""
    parts = [heading("🤖 Расходы на ИИ сегодня")]
    rows = []
    for name, usd, requests, features in spend:
        top = sorted(features.items(), key=lambda item: item[1], reverse=True)
        detail = ", ".join(f"{texts.FEATURE_LABELS.get(k, k)} ${v:.2f}" for k, v in top)
        rows.append((name, f"${usd:.2f}", str(requests), detail))
    if rows:
        total_usd = sum(r[1] for r in spend)
        rows.append(("Итого", f"${total_usd:.2f}", str(sum(r[2] for r in spend)), ""))
        parts.append(
            table(rows, header=("Кто", "$", "Запросов", "На что"), caption="💵 Деньги (оценка)")
        )
    else:
        parts.append(paragraph("Сегодня ИИ ещё ничего не потратил."))
    if used_pct is not None:
        limit = (
            "исчерпан до конца дня" if used_pct >= 100 else f"израсходовано {used_pct}%"
        )
        parts.append(paragraph(f"Дневной лимит ИИ: {limit}"))
    parts.append(paragraph(f"За 7 дней: ${week_usd:.2f}"))
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


def notes_html(notes: list[FamilyNote], total: int | None = None) -> str:
    if not notes:
        return paragraph(
            "📝 В семейной памяти пока пусто. Нажмите «➕ Добавить» "
            "или напишите боту: «Запомни: размер ноги Миши 36»."
        )
    items = []
    for n in notes:
        author = f" ({esc(n.author)})" if n.author else ""
        items.append(f"<li>{esc(n.content)}{author} — <i>{n.created:%d.%m %H:%M}</i></li>")
    parts = [heading("📝 Семейная память"), "<ol>", *items, "</ol>"]
    if total is not None and total > len(notes):
        parts.append(f"<footer>Показаны последние {len(notes)} из {total}.</footer>")
    return "".join(parts)
