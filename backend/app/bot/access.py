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
from aiogram.types import CallbackQuery, Message, TelegramObject

from .. import db as dbmod
from ..config import settings
from ..services import family as family_svc

log = logging.getLogger("homesec.bot.access")

ADULT_ROLES = ("owner", "admin")

KID_COMMANDS = {"start", "help", "quota", "ask", "quiz", "explore"}
KID_CALLBACKS = {"kexp", "qz"}
OWNER_CALLBACKS = {"acc"}  # granting roles is reserved for the owner
GUEST_COMMANDS = {"start", "help"}
GUEST_BUTTONS = {"📶 Wi-Fi", "❓ Помощь"}
ADULT_BUTTONS = {
    "🏠 Кто дома",
    "⏳ Квоты",
    "⏸ Пауза",
    "📺 Общие устройства",
    "🗂 Разбор устройств",
    "📱 Устройства",
    "📝 Заметки",
}

DENIED_KID = "Эта функция доступна только родителям 🛡"
DENIED_GUEST = "Гостям доступны только Wi-Fi и справка."
DENIED_CALLBACK = "Нет доступа к этому действию"
ACCESS_REQUEST_INTERVAL = 3600  # seconds between owner notifications per requester


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reply: str = ""


def _command_name(text: str) -> str | None:
    if not text.startswith("/"):
        return None
    return text.split()[0][1:].split("@")[0].lower()


def check_message(role: str, text: str | None, is_voice: bool = False) -> Decision:
    """Decide whether a user with this role may send this message."""
    if role in ADULT_ROLES:
        return Decision(True)
    content = text or ""
    command = _command_name(content)
    if role == "kid":
        if command is not None:
            return Decision(command in KID_COMMANDS, DENIED_KID)
        return Decision(is_voice or content not in ADULT_BUTTONS, DENIED_KID)
    if role == "guest":
        if command is not None:
            return Decision(command in GUEST_COMMANDS, DENIED_GUEST)
        return Decision(content in GUEST_BUTTONS, DENIED_GUEST)
    return Decision(False)


def check_callback(role: str, data: str | None) -> Decision:
    """Decide whether a user with this role may press this inline button."""
    prefix = (data or "").split(":", 1)[0]
    if role == "owner":
        return Decision(True)
    if role == "admin":
        return Decision(prefix not in OWNER_CALLBACKS, DENIED_CALLBACK)
    if role == "kid":
        return Decision(prefix in KID_CALLBACKS, DENIED_CALLBACK)
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
        decision = check_message(role, event.text, is_voice=event.voice is not None)
        if not decision.allowed:
            await event.answer(decision.reply)
            return None
        return await handler(event, data)

    async def _reject_unauthorized(self, message: Message, user_id: int) -> None:
        log.warning("rejected update from unauthorized telegram user %s", user_id)
        now = time.monotonic()
        last = self._last_request.get(user_id)
        if last is not None and now - last < ACCESS_REQUEST_INTERVAL:
            return
        self._last_request[user_id] = now
        await message.answer(self._unauthorized_text)
        await self._on_unauthorized(message)
