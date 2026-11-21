"""Access policy: everything is closed unless the role explicitly allows it."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import CallbackQuery, Message

from app.bot import access
from app.config import settings


@pytest.mark.parametrize(
    "text", ["/status", "/devices", "/who", "/notes", "/digest", "/block tv", "free text"]
)
def test_stranger_is_denied_everything(text):
    assert not access.check_message("unauthorized", text).allowed


@pytest.mark.parametrize(
    "data", ["act:1:yes", "pick:cancel", "nd:1:block", "acc:5:admin", "qz:1:0"]
)
def test_stranger_cannot_press_buttons(data):
    assert not access.check_callback("unauthorized", data).allowed


@pytest.mark.parametrize("role", ["kid", "guest"])
@pytest.mark.parametrize(
    "text", ["/start", "/help", "/quota", "/ask", "📶 Wi-Fi", "🙏 Попросить время", "как дела"]
)
def test_children_and_guests_are_sent_to_luna(role, text):
    decision = access.check_message(role, text)
    assert not decision.allowed and "Luna" in decision.reply
    assert not access.check_message(role, None, is_voice=True).allowed


@pytest.mark.parametrize("role", ["kid", "guest"])
@pytest.mark.parametrize("data", ["qz:1:0", "act:1:yes", "bdir:1:15", "gt:1:15"])
def test_children_and_guests_press_no_buttons(role, data):
    assert not access.check_callback(role, data).allowed


def test_only_owner_grants_roles():
    assert access.check_callback("owner", "acc:5:admin").allowed
    assert not access.check_callback("admin", "acc:5:admin").allowed
    assert access.check_callback("admin", "act:1:yes").allowed


def test_adult_message_allowed():
    assert access.check_message("admin", "/block tv").allowed


def test_group_chat_rule(monkeypatch):
    monkeypatch.setattr(settings, "telegram_chat_ids", "-100,111")
    monkeypatch.setattr(settings, "telegram_user_ids", "")
    assert access.effective_role("unauthorized", 5, -100) == "owner"
    assert access.effective_role("unauthorized", 5, -200) == "unauthorized"
    assert access.effective_role("unauthorized", 5, 5) == "unauthorized"
    monkeypatch.setattr(settings, "telegram_user_ids", "7")
    assert access.effective_role("unauthorized", 5, -100) == "unauthorized"


def _message(text="/block tv", user_id=999):
    message = MagicMock(spec=Message)
    message.from_user = MagicMock(id=user_id)
    message.chat = MagicMock(id=user_id)
    message.text = text
    message.voice = None
    message.answer = AsyncMock()
    return message


def _middleware(monkeypatch, role):
    monkeypatch.setattr(access, "_role_sync", lambda user_id: role)
    notify = AsyncMock()
    return access.AccessMiddleware(notify, "request sent"), notify


def test_middleware_stops_stranger_and_notifies_once(monkeypatch):
    middleware, notify = _middleware(monkeypatch, "unauthorized")
    handler = AsyncMock()
    message = _message()
    asyncio.run(middleware(handler, message, {}))
    asyncio.run(middleware(handler, message, {}))
    handler.assert_not_awaited()
    notify.assert_awaited_once()
    message.answer.assert_awaited_once_with("request sent")


def test_middleware_passes_owner(monkeypatch):
    middleware, _ = _middleware(monkeypatch, "owner")
    handler = AsyncMock()
    asyncio.run(middleware(handler, _message(), {}))
    handler.assert_awaited_once()


def test_middleware_blocks_callback_from_guest(monkeypatch):
    middleware, _ = _middleware(monkeypatch, "guest")
    handler = AsyncMock()
    callback = MagicMock(spec=CallbackQuery)
    callback.from_user = MagicMock(id=5)
    callback.message = None
    callback.data = "act:1:yes"
    callback.answer = AsyncMock()
    asyncio.run(middleware(handler, callback, {}))
    handler.assert_not_awaited()
    callback.answer.assert_awaited_once()


def test_a_child_is_pointed_to_luna_once_and_no_parent_is_bothered(monkeypatch):
    middleware, notify = _middleware(monkeypatch, "kid")
    handler = AsyncMock()
    message = _message("🙏 Попросить время")
    asyncio.run(middleware(handler, message, {}))
    asyncio.run(middleware(handler, message, {}))
    handler.assert_not_awaited()
    notify.assert_not_awaited()
    message.answer.assert_awaited_once()
    assert message.answer.await_args.args[0] == access.MOVED
    assert type(message.answer.await_args.kwargs["reply_markup"]).__name__ == "ReplyKeyboardRemove"
