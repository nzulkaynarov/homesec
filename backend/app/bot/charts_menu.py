"""«📈» buttons under the reports: a chart is drawn on request and sent as a picture."""

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime

from aiogram import F, Router
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.chat_action import ChatActionSender

from .. import db as dbmod
from ..services import charts

log = logging.getLogger("homesec.bot.charts")

SCREEN = "chq"  # a week of the children's screen time
SERVER = "chs"  # a day of the PC and the Pi, owner only (see access.OWNER_CALLBACKS)
SPEND = "chm"  # a week of AI spend

LABELS = {SCREEN: "📈 Неделя", SERVER: "📈 За сутки", SPEND: "📈 Расходы за неделю"}
NO_DATA = "Пока нет данных для графика."


def button(kind: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=LABELS[kind], callback_data=kind)


def _screen() -> tuple[bytes | None, str]:
    s = dbmod.session()
    try:
        days, people = charts.screen_week(s, datetime.now().date())
    finally:
        s.close()
    if not any(any(m) for m in people.values()):
        return None, ""
    return charts.screen_week_png(days, people), "⏳ Экранное время детей за 7 дней"


def _server() -> tuple[bytes | None, str]:
    s = dbmod.session()
    try:
        pc_heat, memory, pi_heat = charts.host_day(s, datetime.now())
    finally:
        s.close()
    if not pc_heat and not pi_heat:
        return None, ""
    picture = charts.host_day_png(pc_heat, memory, pi_heat)
    return picture, "🖥 Температура и память за сутки, шаг 10 минут. Пунктир — порог тревоги."


def _spend() -> tuple[bytes | None, str]:
    s = dbmod.session()
    try:
        days, people = charts.spend_week(s, datetime.now())
    finally:
        s.close()
    if not any(any(v) for v in people.values()):
        return None, ""
    return charts.spend_week_png(days, people), "🤖 Расходы на ИИ за 7 дней (оценка)"


BUILDERS: dict[str, Callable[[], tuple[bytes | None, str]]] = {
    SCREEN: _screen,
    SERVER: _server,
    SPEND: _spend,
}


async def on_chart(cb: CallbackQuery) -> None:
    kind = cb.data or ""
    if not isinstance(cb.message, Message) or cb.bot is None:
        await cb.answer()
        return
    await cb.answer("Рисую график…")
    chat_id = cb.message.chat.id
    try:
        async with ChatActionSender.upload_photo(chat_id=chat_id, bot=cb.bot):
            picture, caption = await asyncio.to_thread(BUILDERS[kind])
    except Exception:
        log.exception("chart %s failed", kind)
        await cb.message.answer("Не получилось нарисовать график, подробности в журнале.")
        return
    if picture is None:
        await cb.message.answer(NO_DATA)
        return
    await cb.bot.send_photo(chat_id, BufferedInputFile(picture, "chart.png"), caption=caption)


def register(router: Router) -> None:
    router.callback_query.register(on_chart, F.data.in_(BUILDERS.keys()))
