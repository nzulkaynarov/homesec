"""What once only a command showed now sits under a button: the network state under
«🏠 Кто дома», the AI spend under «🖥 Сервер» for the owner who pays for it."""

import asyncio
import re
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import CallbackQuery, Message

from app.bot import access, charts_menu, handlers, home_menu, server_menu
from app.bot.rich import to_plain
from app.db import Base, engine, session
from app.models import Device, EventLog, KVState, Pause, Person


@pytest.fixture
def db():
    Base.metadata.create_all(engine)
    s = session()
    for model in (EventLog, KVState, Pause, Device, Person):
        s.query(model).delete()
    s.commit()
    yield s
    s.close()


def _press(data: str) -> MagicMock:
    cb = MagicMock(spec=CallbackQuery, data=data, answer=AsyncMock())
    cb.from_user = MagicMock(id=1)
    cb.message = MagicMock(spec=Message, text=None, edit_text=AsyncMock())
    cb.message.chat = MagicMock(id=1)
    return cb


def _run(handler, data: str) -> tuple[str, dict[str, str]]:
    cb = _press(data)
    asyncio.run(handler(cb))
    cb.answer.assert_awaited()
    call = cb.message.edit_text.await_args
    markup = call.kwargs["reply_markup"]
    buttons = {b.callback_data: b.text for row in markup.inline_keyboard for b in row}
    return to_plain(call.kwargs["rich_message"].html), buttons


def _message(role: str, monkeypatch) -> MagicMock:
    monkeypatch.setattr(server_menu.common, "get_user_role_sync", lambda user_id: (role, None))
    message = MagicMock(spec=Message, answer=AsyncMock())
    message.from_user = MagicMock(id=1)
    message.chat = MagicMock(id=1)
    message.bot = MagicMock(send_rich_message=AsyncMock())
    return message


def _sent(message: MagicMock) -> tuple[str, dict[str, str]]:
    call = message.bot.send_rich_message.await_args
    markup = call.kwargs["reply_markup"]
    buttons = {b.callback_data: b.text for row in markup.inline_keyboard for b in row}
    return to_plain(call.kwargs["rich_message"].html), buttons


def test_who_is_home_leads_to_the_network_and_back(db):
    kid = Person(name="Миша", role="kid")
    db.add(kid)
    db.commit()
    db.add(
        Pause(target_type="person", target=str(kid.id), until=datetime.now() + timedelta(hours=1))
    )
    db.commit()

    text, buttons = _run(home_menu.cb_who, home_menu.WHO)
    assert "🏠 Кто дома" in text and buttons == {home_menu.NETWORK: "🛡 Состояние сети"}

    text, buttons = _run(home_menu.cb_network, home_menu.NETWORK)
    assert "🛡 Состояние сети" in text and buttons == {home_menu.WHO: "← 🏠 Кто дома"}
    assert "Роутер · ❌ не отвечает" in text  # no router in tests
    assert re.search(r"Миша · \d\d:\d\d", text)  # a pause by name, never by number


def test_status_command_shows_the_same_screen(db, monkeypatch):
    message = _message("admin", monkeypatch)
    asyncio.run(home_menu.cmd_status(message))
    text, buttons = _sent(message)
    assert "🛡 Состояние сети" in text and home_menu.WHO in buttons


def test_server_screen_offers_the_ai_spend_and_the_way_back(db, monkeypatch):
    monkeypatch.setattr(server_menu, "_status_sync", lambda: "<p>ПК</p>")
    message = _message("owner", monkeypatch)
    asyncio.run(server_menu.btn_server(message))
    _, buttons = _sent(message)
    assert buttons == {charts_menu.SERVER: "📈 За сутки", server_menu.SPEND: "🤖 Расходы на ИИ"}

    text, buttons = _run(server_menu.cb_spend, server_menu.SPEND)
    assert "🤖 Расходы на ИИ сегодня" in text and "Токен" not in text
    assert buttons == {charts_menu.SPEND: "📈 Расходы за неделю", server_menu.SCREEN: "← Сервер"}

    _, buttons = _run(server_menu.cb_server, server_menu.SCREEN)
    assert server_menu.SPEND in buttons


def test_usage_command_is_the_owners(db, monkeypatch):
    message = _message("admin", monkeypatch)
    asyncio.run(server_menu.cmd_usage(message))
    message.answer.assert_awaited_once_with(server_menu.SPEND_OWNER_ONLY)
    message.bot.send_rich_message.assert_not_awaited()

    message = _message("owner", monkeypatch)
    asyncio.run(server_menu.cmd_usage(message))
    text, buttons = _sent(message)
    assert "🤖 Расходы на ИИ сегодня" in text and server_menu.SCREEN in buttons


@pytest.mark.parametrize("data", [server_menu.SCREEN, server_menu.SPEND, charts_menu.SPEND])
def test_spend_and_server_buttons_are_the_owners(data):
    assert access.check_callback("owner", data).allowed
    assert not access.check_callback("admin", data).allowed
    assert not access.check_callback("kid", data).allowed


def test_who_and_network_buttons_are_for_both_parents():
    for data in (home_menu.WHO, home_menu.NETWORK):
        assert access.check_callback("admin", data).allowed
        assert not access.check_callback("guest", data).allowed


def test_new_screens_sit_behind_the_access_middleware():
    callbacks = {h.callback for h in handlers.router.callback_query.handlers}
    assert {home_menu.cb_who, home_menu.cb_network} <= callbacks
    assert {server_menu.cb_server, server_menu.cb_spend} <= callbacks
    names = [h.callback.__name__ for h in handlers.router.message.handlers]
    assert names.index("btn_who") < names.index("free_text")
