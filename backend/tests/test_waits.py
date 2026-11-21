"""One typed answer at a time: a menu button, a command or another button ends every wait, so
the next free text goes to the AI and never renames a device or adds a person."""

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.dispatcher.event.telegram import TelegramEventObserver
from aiogram.types import CallbackQuery, Message

from app.bot import device_menu, handlers, notes_menu, pause_menu, rules_menu, waits
from app.db import Base, engine

CHAT, USER = 700, 11
KEY = (CHAT, USER)


@pytest.fixture(autouse=True)
def no_waits():
    Base.metadata.create_all(engine)
    for table in waits._tables:
        table.clear()
    yield
    for table in waits._tables:
        table.clear()


def _typed(text: str) -> MagicMock:
    message = MagicMock(spec=Message, text=text, caption=None, voice=None, answer=AsyncMock())
    message.chat = MagicMock(id=CHAT)
    message.from_user = MagicMock(id=USER)
    return message


def _press(data: str) -> MagicMock:
    cb = MagicMock(spec=CallbackQuery, data=data, answer=AsyncMock())
    cb.from_user = MagicMock(id=USER)
    cb.message = MagicMock(spec=Message, text="", edit_text=AsyncMock())
    cb.message.chat = MagicMock(id=CHAT)
    return cb


def _taker(observer: TelegramEventObserver, event: Any) -> Any:
    """The handler the dispatcher would run: the first whose filters all pass, in order."""

    async def walk() -> Any:
        for handler in observer.handlers:
            passed, _ = await handler.check(event, bot=MagicMock())
            if passed:
                return handler.callback
        return None

    return asyncio.run(walk())


def _later() -> float:
    return time.monotonic() + 300


WAITS = {
    "device rename": lambda: waits.start(device_menu._renames, KEY, (5, "none", 0, _later())),
    "pause time": lambda: waits.start(pause_menu._custom, KEY, ("a", _later())),
    "new person": lambda: waits.start(rules_menu._typing, KEY, ("new", "kid", _later())),
    "note": lambda: waits.start(notes_menu._adding, KEY, (1, _later())),
}
LEAVING = [
    "📝 Заметки",
    "/notes",
    "/note",
    "📱 Устройства",
    "⏸ Пауза",
    "📋 Правила",
    "🗂 Разбор устройств",
    "⏳ Квоты",
    "❓ Помощь",
    "/status",
]


@pytest.mark.parametrize("wait", WAITS)
@pytest.mark.parametrize("text", LEAVING)
def test_a_menu_button_or_command_ends_every_wait(wait, text):
    """The hole: «📝 Заметки» and its commands were checked before the other waits, so after
    «✏️ Переименовать» → «📝 Заметки» the next message renamed the phone."""
    WAITS[wait]()
    taker = _taker(handlers.router.message, _typed(text))
    assert taker not in (
        device_menu.on_name,
        pause_menu.on_time,
        rules_menu.on_text,
        notes_menu.on_note,
    )
    assert all(KEY not in table for table in waits._tables), (wait, text)
    assert _taker(handlers.router.message, _typed("отключи Мише интернет на час")) is (
        handlers.free_text
    )


@pytest.mark.parametrize("wait", WAITS)
def test_another_inline_button_ends_every_wait(wait):
    WAITS[wait]()
    _taker(handlers.router.callback_query, _press("hrp:3"))
    assert all(KEY not in table for table in waits._tables)
    assert _taker(handlers.router.message, _typed("30")) is handlers.free_text


def test_a_new_prompt_ends_the_one_before():
    """Only the prompt on screen takes the answer: a typed note never renames a device."""
    WAITS["device rename"]()
    WAITS["note"]()
    assert KEY not in device_menu._renames and KEY in notes_menu._adding
    assert _taker(handlers.router.message, _typed("Код домофона 4589")) is notes_menu.on_note


def test_the_wait_survives_until_the_answer():
    WAITS["device rename"]()
    assert _taker(handlers.router.message, _typed("Планшет Миши")) is device_menu.on_name


def test_cancel_under_a_typed_pause_time_ends_the_wait():
    """«⌨️ Своё время» → «← Отмена» lands on the picker; a later «30» must not pause anyone."""
    WAITS["pause time"]()
    asyncio.run(pause_menu.cb_picker(_press("pzt:x")))  # the cancel button, even on its own
    assert KEY not in pause_menu._custom
    assert not pause_menu.waiting_for_time(_typed("30"))
