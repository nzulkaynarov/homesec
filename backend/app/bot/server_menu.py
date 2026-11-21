"""The owner's view of the machines: «🖥 Сервер» shows the PC and the Pi now and, one tap away,
what the AI costs; a loop samples the Pi every minute and tells the owner when either machine
needs attention."""

import asyncio
import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .. import db as dbmod
from ..services import hosts
from . import charts_menu, common, reports, rich, texts

log = logging.getLogger("homesec.bot.server")

INTERVAL_SECONDS = 60
SCREEN = "srv"  # back to «🖥 Сервер»; owner only (see access.OWNER_CALLBACKS)
SPEND = "srvm"  # the AI spend: the owner pays for it, so it is his alone
SERVER_OWNER_ONLY = "Состояние серверов видит только владелец."
SPEND_OWNER_ONLY = "Расходы на ИИ видит только владелец."


def status_html(blocks: list[tuple[str, list[hosts.Row]]]) -> str:
    """Each machine as a heading and a two-column table; note lines become paragraphs."""
    parts = []
    for title, rows in blocks:
        parts.append(rich.heading(title))
        cells = [(label, value) for label, value in rows if value]
        if cells:
            parts.append(rich.table(cells))
        parts += [rich.paragraph(label) for label, value in rows if not value]
    return "".join(parts)


def _status_sync() -> str:
    s = dbmod.session()
    try:
        return status_html(hosts.status_blocks(s))
    finally:
        s.close()


def server_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [charts_menu.button(charts_menu.SERVER)],
            [InlineKeyboardButton(text="🤖 Расходы на ИИ", callback_data=SPEND)],
        ]
    )


def spend_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [charts_menu.button(charts_menu.SPEND)],
            [InlineKeyboardButton(text="← Сервер", callback_data=SCREEN)],
        ]
    )


async def _is_owner(message: Message) -> bool:
    user_id = message.from_user.id if message.from_user else 0
    role, _ = await asyncio.to_thread(common.get_user_role_sync, user_id)
    return role == "owner"


async def btn_server(message: Message) -> None:
    if not await _is_owner(message):
        await message.answer(SERVER_OWNER_ONLY)
        return
    await rich.answer(message, await asyncio.to_thread(_status_sync), server_keyboard())


async def cmd_usage(message: Message) -> None:
    """/usage for those used to it: the same screen as «🤖 Расходы на ИИ» under «🖥 Сервер»."""
    if not await _is_owner(message):
        await message.answer(SPEND_OWNER_ONLY)
        return
    await rich.answer(message, await reports.spend_screen(), spend_keyboard())


async def cb_server(cb: CallbackQuery) -> None:
    await cb.answer()
    if isinstance(cb.message, Message):
        await rich.edit(cb.message, await asyncio.to_thread(_status_sync), server_keyboard())


async def cb_spend(cb: CallbackQuery) -> None:
    await cb.answer()
    if isinstance(cb.message, Message):
        await rich.edit(cb.message, await reports.spend_screen(), spend_keyboard())


def _sample_and_judge() -> list[hosts.Change]:
    s = dbmod.session()
    try:
        hosts.record(s, hosts.PI, hosts.pi_sample())
        return hosts.changes(s)
    finally:
        s.close()


def _remember(change: hosts.Change) -> None:
    s = dbmod.session()
    try:
        hosts.remember(s, change)
    finally:
        s.close()


async def _tell_owner(bot: Bot, chat_ids: set[int], text: str) -> bool:
    """Hardware is the owner's business: only the configured chats, not every parent."""
    delivered = False
    for chat_id in chat_ids:
        try:
            for part in texts.split_message(text):
                await bot.send_message(chat_id, part)
            delivered = True
        except Exception:
            log.exception("could not send a server alert to %s", chat_id)
    return delivered


async def watch_loop(bot: Bot, chat_ids: set[int]) -> None:
    """A change is remembered only once delivered, so a Telegram outage does not swallow it."""
    while True:
        try:
            for change in await asyncio.to_thread(_sample_and_judge):
                if not change.text or await _tell_owner(bot, chat_ids, change.text):
                    await asyncio.to_thread(_remember, change)
        except Exception:
            log.exception("server watch loop failed")
        await asyncio.sleep(INTERVAL_SECONDS)


def register(router: Router) -> None:
    router.message.register(btn_server, F.text == common.BTN_SERVER)
    router.message.register(cmd_usage, Command("usage"))
    router.callback_query.register(cb_server, F.data == SCREEN)
    router.callback_query.register(cb_spend, F.data == SPEND)
