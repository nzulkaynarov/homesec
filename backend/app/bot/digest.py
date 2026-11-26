"""The evening summary as one screen: a verdict first, what needs an answer next, the children's
day as a table, and the rest folded away until a parent opens it.

The owner's copy also folds in technical failures; the other parent does not need them.
"""

import asyncio
import logging
from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .. import db as dbmod
from ..ai import client as ai_client
from ..models import BonusRequest
from ..services import digest as digest_svc
from ..services import family as family_svc
from ..services import time_notices
from . import alerts, charts_menu, reports, rich, texts
from .rich import esc

log = logging.getLogger("homesec.bot.digest")

SHOW_REQUEST = "dgr"  # + ":" + request id: the request again, with its buttons
QUOTAS = "dgq"
MONTHS = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)  # fmt: skip


def _number(n: int) -> str:
    return f"{n:,}".replace(",", " ")  # 451 812, a thin space keeps it on one line


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _verdict(facts: digest_svc.Facts, owner: bool) -> str:
    if facts.waiting:
        return f"🔴 Нужно ваше решение: {len(facts.waiting)}"
    to_look = len(facts.new_devices) + (len(facts.errors) if owner else 0)
    if to_look:
        return f"🟡 Есть что посмотреть: {to_look}"
    return "🟢 Всё спокойно"


def _kids_table(kids: list[digest_svc.Kid]) -> str:
    if not any(k.total or k.luna for k in kids):
        return rich.paragraph("Сегодня дети в интернет не выходили.")
    columns = [
        (code, label) for code, label in digest_svc.CATEGORY_COLUMNS
        if any(k.categories.get(code) for k in kids)
    ]  # fmt: skip
    header = ["", "Всего", *(label for _, label in columns)]
    luna = any(k.luna for k in kids)
    if luna:
        header.append("Вопросов Luna")
    rows = []
    for kid in kids:
        row = [kid.name, time_notices.duration(kid.total) if kid.total else "—"]
        for code, _ in columns:
            minutes = kid.categories.get(code, 0)
            row.append(time_notices.duration(minutes) if minutes else "—")
        if luna:
            row.append(str(kid.luna) if kid.luna else "—")
        rows.append(row)
    return rich.table(rows, header)


def _network(facts: digest_svc.Facts) -> str:
    if facts.dns_total is None:
        return rich.paragraph("🌐 Фильтр сайтов не ответил, статистики за сутки нет.")
    blocked = facts.dns_blocked or 0
    share = round(100 * blocked / facts.dns_total) if facts.dns_total else 0
    return rich.paragraph(
        f"🌐 За сутки {_number(facts.dns_total)} обращений к сайтам, "
        f"фильтр отсеял {_number(blocked)} ({share} %): реклама, слежка и запрещённое."
    )


def render(facts: digest_svc.Facts, owner: bool = False) -> str:
    day = facts.day
    parts = [
        rich.heading(f"📋 Сводка за {day.day} {MONTHS[day.month - 1]}"),
        f"<p><b>{esc(_verdict(facts, owner))}</b></p>",
    ]
    if facts.waiting:
        parts.append(rich.heading("🙏 Ждут ответа", 4))
        parts.append(rich.bullets(
            f"{w.who} просит ещё времени, с {w.since:%H:%M}" for w in facts.waiting
        ))  # fmt: skip
    if facts.new_devices:
        parts.append(rich.heading("🆕 Новые устройства", 4))
        parts.append(rich.bullets(facts.new_devices))
    if facts.kids:
        parts.append(rich.heading("👧 Дети сегодня", 4))
        parts.append(_kids_table(facts.kids))
    if facts.cutoffs:
        count = sum(len(c.names) for c in facts.cutoffs)
        lines = [
            f"{c.at} — {', '.join(c.names)}" + (f" ({c.why})" if c.why else "")
            for c in facts.cutoffs
        ]
        word = _plural(count, "устройство", "устройства", "устройств")
        parts.append(rich.details(f"⛔ Отключали интернет: {count} {word}", rich.bullets(lines)))
    if facts.changes:
        parts.append(rich.details(
            f"🛠 Изменения через бота и Luna: {len(facts.changes)}", rich.bullets(facts.changes)
        ))  # fmt: skip
    parts.append(_network(facts))
    if owner and facts.errors:
        parts.append(rich.details(
            f"🔧 Технические сбои: {len(facts.errors)}", rich.bullets(facts.errors)
        ))  # fmt: skip
    return "".join(parts)


def keyboard(facts: digest_svc.Facts) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            text=f"🙏 Ответить: {w.who}"[:60], callback_data=f"{SHOW_REQUEST}:{w.request_id}"
        )]
        for w in facts.waiting
    ]  # fmt: skip
    rows.append([
        InlineKeyboardButton(text="⏳ Квоты", callback_data=QUOTAS),
        charts_menu.button(charts_menu.SCREEN),
    ])  # fmt: skip
    return InlineKeyboardMarkup(inline_keyboard=rows)


def facts_now(now: datetime | None = None) -> digest_svc.Facts:
    s = dbmod.session()
    try:
        ledger = ai_client.ledger_for_day(s)
        luna = {
            int(key.split(":", 1)[1]): int(row.get("requests", 0))
            for key, row in ledger.items()
            if key.startswith("person:") and key.split(":", 1)[1].isdigit()
        }
        return digest_svc.collect(s, now, luna)
    finally:
        s.close()


def _owner_ids() -> set[int]:
    s = dbmod.session()
    try:
        return family_svc.owner_chat_ids(s)
    finally:
        s.close()


async def send(bot: Bot, chat_ids: set[int]) -> int:
    """The evening summary to every chat; returns how many got it."""
    facts = await asyncio.to_thread(facts_now)
    owners = await asyncio.to_thread(_owner_ids)
    markup = keyboard(facts)
    delivered = 0
    for chat_id in chat_ids:
        try:
            if await rich.send(bot, chat_id, render(facts, chat_id in owners), markup):
                delivered += 1
        except Exception:
            log.exception("the summary did not reach chat %s", chat_id)
    return delivered


async def cmd_digest(message: Message) -> None:
    facts = await asyncio.to_thread(facts_now)
    owner = message.chat.id in await asyncio.to_thread(_owner_ids)
    await rich.answer(message, render(facts, owner), keyboard(facts))


def _request_notice(request_id: int) -> time_notices.Notice | None:
    s = dbmod.session()
    try:
        req = s.get(BonusRequest, request_id)
        if req is None or req.status != "pending":
            return None
        return time_notices.request_notice(s, req)
    finally:
        s.close()


async def cb_show_request(cb: CallbackQuery) -> None:
    request_id = int((cb.data or "").split(":")[1])
    notice = await asyncio.to_thread(_request_notice, request_id)
    if notice is None or not isinstance(cb.message, Message):
        await cb.answer("На эту просьбу уже ответили.", show_alert=True)
        return
    await cb.answer()
    await cb.message.answer(
        texts.alert_html(notice.text),
        parse_mode="HTML",
        reply_markup=alerts.request_keyboard(request_id, notice),
    )


async def cb_quotas(cb: CallbackQuery) -> None:
    await cb.answer()
    if isinstance(cb.message, Message):
        await reports.cmd_quota(cb.message)


def register(router: Router) -> None:
    router.message.register(cmd_digest, Command("digest"))
    router.callback_query.register(cb_show_request, F.data.startswith(f"{SHOW_REQUEST}:"))
    router.callback_query.register(cb_quotas, F.data == QUOTAS)
