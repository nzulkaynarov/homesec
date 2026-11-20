"""The owner's view of the machines: «🖥 Сервер» shows the PC and the Pi now, and a loop samples
the Pi every minute and tells the owner when either machine needs attention."""

import asyncio
import logging

from aiogram import Bot, F, Router
from aiogram.types import Message

from .. import db as dbmod
from ..services import hosts
from . import common, texts

log = logging.getLogger("homesec.bot.server")

INTERVAL_SECONDS = 60


def _status_sync() -> str:
    s = dbmod.session()
    try:
        return hosts.status_text(s)
    finally:
        s.close()


async def btn_server(message: Message) -> None:
    user_id = message.from_user.id if message.from_user else 0
    role, _ = await asyncio.to_thread(common.get_user_role_sync, user_id)
    if role != "owner":
        await message.answer("Состояние серверов видит только владелец.")
        return
    await message.answer(await asyncio.to_thread(_status_sync))


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
