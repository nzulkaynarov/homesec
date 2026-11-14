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


@pytest.mark.parametrize(
    "text",
    ["/status", "/who", "/notes", "/block tv", "/digest", "/usage", "🏠 Кто дома", "как дела"],
)
def test_guest_gets_only_wifi_and_help(text):
    assert not access.check_message("guest", text).allowed


@pytest.mark.parametrize("text", ["/start", "/help", "📶 Wi-Fi", "❓ Помощь"])
def test_guest_allowed_items(text):
    assert access.check_message("guest", text).allowed


def test_guest_voice_is_denied_and_buttons_closed():
    assert not access.check_message("guest", None, is_voice=True).allowed
    assert not access.check_callback("guest", "act:1:yes").allowed


@pytest.mark.parametrize(
    "text", ["/status", "/block tv", "/digest", "/notes", "📝 Заметки", "⏸ Пауза"]
)
def test_kid_denied_parent_items(text):
    assert not access.check_message("kid", text).allowed


@pytest.mark.parametrize(
    "text",
    ["/start", "/quota", "/ask", "/help@homesec_bot", "помоги с уроками", "🙏 Попросить время"],
)
def test_kid_allowed_items(text):
    assert access.check_message("kid", text).allowed


@pytest.mark.parametrize("text", ["/quiz", "/explore"])
def test_tutor_and_quiz_commands_left_for_luna(text):
    assert not access.check_message("kid", text).allowed


@pytest.mark.parametrize("data", ["qz:1:0", "kexp:quest", "act:1:yes", "bdir:1:15"])
def test_kid_presses_no_inline_buttons(data):
    assert not access.check_callback("kid", data).allowed


def test_kid_voice_allowed():
    assert access.check_message("kid", None, is_voice=True).allowed


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
