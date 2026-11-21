"""«🏠 Кто дома»: who of the family is online now and, one tap away, whether the network itself
is fine — the router, the site filter, devices and pauses (/status shows the same screen)."""

import asyncio

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .. import db as dbmod
from ..ai import tools
from ..services import family as family_svc
from ..services import pauses
from . import rich, screens

BTN_WHO = "🏠 Кто дома"
WHO = "who"  # back to «🏠 Кто дома»
NETWORK = "net"  # the state of the network


def _who_sync() -> str:
    s = dbmod.session()
    try:
        return screens.presence_html(family_svc.get_presence_status(s))
    finally:
        s.close()


def _network_sync() -> str:
    s = dbmod.session()
    try:
        status = tools.run_tool(s, "get_status", {}, source="bot")
        return screens.status_html(status, pauses.active(s))
    finally:
        s.close()


def who_keyboard() -> InlineKeyboardMarkup:
    button = InlineKeyboardButton(text="🛡 Состояние сети", callback_data=NETWORK)
    return InlineKeyboardMarkup(inline_keyboard=[[button]])


def network_keyboard() -> InlineKeyboardMarkup:
    button = InlineKeyboardButton(text=f"← {BTN_WHO}", callback_data=WHO)
    return InlineKeyboardMarkup(inline_keyboard=[[button]])


async def btn_who(message: Message) -> None:
    await rich.answer(message, await asyncio.to_thread(_who_sync), who_keyboard())


async def cmd_status(message: Message) -> None:
    await rich.answer(message, await asyncio.to_thread(_network_sync), network_keyboard())


async def cb_who(cb: CallbackQuery) -> None:
    await cb.answer()
    if isinstance(cb.message, Message):
        await rich.edit(cb.message, await asyncio.to_thread(_who_sync), who_keyboard())


async def cb_network(cb: CallbackQuery) -> None:
    await cb.answer()
    if isinstance(cb.message, Message):
        await rich.edit(cb.message, await asyncio.to_thread(_network_sync), network_keyboard())


def register(router: Router) -> None:
    """Attach to the router that carries the access middleware, before catch-all text handlers."""
    router.message.register(btn_who, F.text == BTN_WHO)
    router.message.register(btn_who, Command("who"))
    router.message.register(cmd_status, Command("status"))
    router.callback_query.register(cb_who, F.data == WHO)
    router.callback_query.register(cb_network, F.data == NETWORK)
