"""Deny-by-default access control applied to every Telegram update.

The policy lives here rather than in individual handlers, so a new command is
closed to everyone except adults until it is explicitly opened."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove, TelegramObject

from .. import db as dbmod
from ..config import settings
from ..services import family as family_svc

log = logging.getLogger("homesec.bot.access")

ADULT_ROLES = ("owner", "admin")

# Granting roles, the server screen and its chart, and the AI spend (the owner pays for it).
OWNER_CALLBACKS = {"acc", "chs", "srv", "srvm", "chm"}
ADULT_BUTTONS = {  # the parents' menu; a tap on one of these ends any "type your answer" wait
    "🖥 Сервер",
    "🏠 Кто дома",
    "⏳ Квоты",
    "⏸ Пауза",
    "📺 Общие устройства",
    "🗂 Разбор устройств",
    "📱 Устройства",
    "📋 Правила",
    "📝 Заметки",
}
NOT_SERVED = ("kid", "guest")  # known people without parent rights: their place is Luna

MOVED = (
    "Этот бот теперь только для родителей 🛡\n\n"
    "Сколько осталось времени, попросить ещё, викторина и всё остальное — у Luna 🐾 "
    "Открой чат с Luna и нажми «⏳ Интернет»."
)
DENIED_CALLBACK = "Нет доступа к этому действию"
ACCESS_REQUEST_INTERVAL = 3600  # seconds between owner notifications per requester
MOVED_INTERVAL = 3600  # seconds between "go to Luna" replies to one child or guest


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reply: str = ""


def check_message(role: str, text: str | None = None, is_voice: bool = False) -> Decision:
    """Only parents use this bot; children and guests are sent to Luna."""
    if role in ADULT_ROLES:
        return Decision(True)
    return Decision(False, MOVED if role in NOT_SERVED else "")


def check_callback(role: str, data: str | None) -> Decision:
    """Decide whether a user with this role may press this inline button."""
    prefix = (data or "").split(":", 1)[0]
    if role == "owner":
        return Decision(True)
    if role == "admin":
        return Decision(prefix not in OWNER_CALLBACKS, DENIED_CALLBACK)
    return Decision(False, DENIED_CALLBACK)


def effective_role(role: str, user_id: int, chat_id: int | None) -> str:
    """Treat members of an allowlisted group chat as owners, as HS_TELEGRAM_USER_IDS documents."""
    if role != "unauthorized" or chat_id is None or chat_id >= 0:
        return role
    allowed_users = settings.telegram_allowed_user_ids
    if chat_id in settings.telegram_allowed_ids and (not allowed_users or user_id in allowed_users):
        return "owner"
    return role


def _role_sync(user_id: int) -> str:
    session = dbmod.session()
    try:
        return family_svc.get_user_role(session, user_id)[0]
    finally:
        session.close()


class AccessMiddleware(BaseMiddleware):
    """Stops every update the sender's role does not permit, before any handler runs."""

    def __init__(
        self,
        on_unauthorized: Callable[[Message], Awaitable[None]],
        unauthorized_text: str,
    ) -> None:
        self._on_unauthorized = on_unauthorized
        self._unauthorized_text = unauthorized_text
        self._last_request: dict[int, float] = {}
        self._last_moved: dict[int, float] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        sender = getattr(event, "from_user", None)
        if sender is None:
            return None
        chat_id: int | None = None
        if isinstance(event, Message):
            chat_id = event.chat.id
        elif isinstance(event, CallbackQuery) and isinstance(event.message, Message):
            chat_id = event.message.chat.id
        role = effective_role(await asyncio.to_thread(_role_sync, sender.id), sender.id, chat_id)

        if isinstance(event, CallbackQuery):
            decision = check_callback(role, event.data)
            if not decision.allowed:
                await event.answer(decision.reply, show_alert=True)
                return None
            return await handler(event, data)

        if not isinstance(event, Message):
            return None
        if role == "unauthorized":
            await self._reject_unauthorized(event, sender.id)
            return None
        if role in NOT_SERVED:
            await self._send_to_luna(event, sender.id)
            return None
        return await handler(event, data)

    async def _send_to_luna(self, message: Message, user_id: int) -> None:
        """One calm pointer an hour, and the old menu goes away; no parent is bothered."""
        now = time.monotonic()
        last = self._last_moved.get(user_id)
        if last is not None and now - last < MOVED_INTERVAL:
            return
        self._last_moved[user_id] = now
        await message.answer(MOVED, reply_markup=ReplyKeyboardRemove())

    async def _reject_unauthorized(self, message: Message, user_id: int) -> None:
        log.warning("rejected update from unauthorized telegram user %s", user_id)
        now = time.monotonic()
        last = self._last_request.get(user_id)
        if last is not None and now - last < ACCESS_REQUEST_INTERVAL:
            return
        self._last_request[user_id] = now
        await message.answer(self._unauthorized_text)
        await self._on_unauthorized(message)
