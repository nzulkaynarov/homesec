"""A child's photo of a finished task reaches parents and the decision reaches the child."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.bot import common, kid_menu
from app.bot.access import check_message
from app.config import settings


def _photo_message(user_id=7):
    message = MagicMock()
    message.from_user.id = user_id
    message.photo = [MagicMock(file_id="small"), MagicMock(file_id="large")]
    message.caption = "убрал комнату"
    message.answer = AsyncMock()
    message.bot.send_photo = AsyncMock()
    return message


@pytest.fixture
def kid(monkeypatch):
    person = MagicMock(id=3)
    person.name = "Миша"
    monkeypatch.setattr(common, "get_user_role_sync", lambda user_id: ("kid", person))
    monkeypatch.setattr(settings, "telegram_chat_ids", "111")
    common.ai_request_times.clear()
    return person


def test_photo_goes_to_parents_with_award_buttons(kid):
    message = _photo_message()
    asyncio.run(kid_menu.handle_task_photo(message))
    kwargs = message.bot.send_photo.await_args.kwargs
    assert message.bot.send_photo.await_args.args == (111,)
    assert kwargs["photo"] == "large"
    assert "Миша" in kwargs["caption"] and "убрал комнату" in kwargs["caption"]
    assert kwargs["reply_markup"].inline_keyboard[0][0].callback_data == "bdir:3:15"


def test_adult_photo_is_not_forwarded(monkeypatch):
    monkeypatch.setattr(common, "get_user_role_sync", lambda user_id: ("owner", None))
    message = _photo_message()
    asyncio.run(kid_menu.handle_task_photo(message))
    message.bot.send_photo.assert_not_awaited()


def test_kid_may_send_photo_and_guest_may_not():
    assert check_message("kid", None).allowed
    assert not check_message("guest", None).allowed


@pytest.mark.parametrize(("available", "offered"), [(True, True), (False, False)])
def test_quiz_is_offered_only_with_a_time_request(monkeypatch, available, offered):
    monkeypatch.setattr(settings, "telegram_chat_ids", "111")
    monkeypatch.setattr(kid_menu, "_can_take_quiz_sync", lambda person_id: available)
    message = MagicMock()
    message.answer = AsyncMock()
    message.bot.send_message = AsyncMock()
    asyncio.run(kid_menu._forward_time_request_to_parents(message, 3, "Миша", "хочу доиграть"))
    assert ("викторине у Luna" in message.answer.await_args.args[0]) is offered


def _text_message():
    message = MagicMock()
    message.chat.id = 7
    message.answer = AsyncMock()
    message.bot.send_message = AsyncMock()
    return message


def test_kid_chat_points_to_luna_without_calling_the_ai(kid, monkeypatch):
    monkeypatch.setattr(kid_menu, "_alert_parents_about_kid", AsyncMock())
    message = _text_message()
    asyncio.run(kid_menu.handle_text(message, kid, "помоги решить задачу по математике"))
    assert message.answer.await_args.args[0] == kid_menu.ELSEWHERE
    kid_menu._alert_parents_about_kid.assert_not_awaited()


def test_worrying_kid_message_still_reaches_parents(kid, monkeypatch):
    alert = AsyncMock()
    monkeypatch.setattr(kid_menu, "_alert_parents_about_kid", alert)
    message = _text_message()
    asyncio.run(kid_menu.handle_text(message, kid, "меня травят в школе"))
    alert.assert_awaited_once()
    assert "расскажи" in message.answer.await_args.args[0]


def test_kid_plea_for_time_goes_to_parents(kid, monkeypatch):
    forward = AsyncMock()
    monkeypatch.setattr(kid_menu, "_forward_time_request_to_parents", forward)
    asyncio.run(kid_menu.handle_text(_text_message(), kid, "дай ещё времени пожалуйста"))
    forward.assert_awaited_once()
