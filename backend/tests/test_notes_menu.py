"""«📝 Заметки»: the family memory is read, added to by typing and cleaned up with buttons."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.filters import CommandObject
from aiogram.types import CallbackQuery, Message

from app.bot import access, handlers, notes_menu
from app.bot.rich import to_plain
from app.bot.texts import DANGER
from app.db import Base, engine, session
from app.models import EventLog, FamilyNote
from app.services import family as family_svc

CHAT, USER, SCREEN_ID, PROMPT_ID = 400, 9, 50, 77


@pytest.fixture
def db():
    Base.metadata.create_all(engine)
    s = session()
    for model in (EventLog, FamilyNote):
        s.query(model).delete()
    s.commit()
    notes_menu._adding.clear()
    yield s
    s.close()


def _bot() -> MagicMock:
    bot = MagicMock()
    bot.send_rich_message = AsyncMock(return_value=MagicMock(message_id=PROMPT_ID))
    bot.delete_message = AsyncMock()
    return bot


def _press(data: str, bot: MagicMock | None = None) -> MagicMock:
    cb = MagicMock(spec=CallbackQuery, data=data, answer=AsyncMock())
    cb.from_user = MagicMock(id=USER, first_name="Мама")
    cb.message = MagicMock(spec=Message, text=None, message_id=SCREEN_ID, edit_text=AsyncMock())
    cb.message.chat = MagicMock(id=CHAT)
    cb.message.bot = bot or _bot()
    return cb


def _typed(text: str, bot: MagicMock | None = None) -> MagicMock:
    message = MagicMock(spec=Message, text=text, answer=AsyncMock())
    message.chat = MagicMock(id=CHAT)
    message.from_user = MagicMock(id=USER, first_name="Папа")
    message.bot = bot or _bot()
    return message


def _edited(cb: MagicMock) -> tuple[str, dict[str, str]]:
    """What the pressed message became: its text and its buttons (data -> label)."""
    call = cb.message.edit_text.await_args
    markup = call.kwargs["reply_markup"]
    buttons = {b.callback_data: b.text for row in markup.inline_keyboard for b in row}
    return to_plain(call.kwargs["rich_message"].html), buttons


def _sent(bot: MagicMock) -> tuple[str, dict[str, str]]:
    call = bot.send_rich_message.await_args
    markup = call.kwargs["reply_markup"]
    buttons = {b.callback_data: b.text for row in markup.inline_keyboard for b in row}
    return to_plain(call.kwargs["rich_message"].html), buttons


def _press_run(handler, data: str) -> tuple[str, dict[str, str]]:
    cb = _press(data)
    asyncio.run(handler(cb))
    cb.answer.assert_awaited()
    return _edited(cb)


def test_empty_memory_offers_to_add(db):
    message = _typed(notes_menu.BTN_NOTES)
    asyncio.run(notes_menu.btn_notes(message))
    text, buttons = _sent(message.bot)
    assert "пока пусто" in text and "«➕ Добавить»" in text
    assert buttons == {notes_menu.ADD: "➕ Добавить"}  # nothing to delete yet


def test_a_typed_note_is_saved_and_the_prompt_goes_away(db):
    text, buttons = _press_run(notes_menu.cb_add, notes_menu.ADD)
    assert text == notes_menu.ASK and buttons == {notes_menu.SCREEN: "← Отмена"}

    message = _typed("Размер ноги Миши 36")
    assert notes_menu.waiting_for_note(message)
    asyncio.run(notes_menu.on_note(message))

    message.bot.delete_message.assert_awaited_once_with(CHAT, SCREEN_ID)  # clean chat
    text, buttons = _sent(message.bot)
    assert "✅ Заметка сохранена: «Размер ноги Миши 36»" in text
    assert "Размер ноги Миши 36 (Папа)" in text
    assert set(buttons) == {notes_menu.ADD, notes_menu.PICK}
    assert [n.content for n in family_svc.get_family_notes(db)] == ["Размер ноги Миши 36"]
    assert not notes_menu.waiting_for_note(_typed("а это уже вопрос ИИ"))


def test_a_too_long_note_asks_again(db):
    _press_run(notes_menu.cb_add, notes_menu.ADD)
    message = _typed("х" * (notes_menu.MAX_LENGTH + 1))
    asyncio.run(notes_menu.on_note(message))
    text, buttons = _sent(message.bot)
    assert text == notes_menu.TOO_LONG and buttons == {notes_menu.SCREEN: "← Отмена"}
    assert family_svc.count_family_notes(db) == 0
    assert notes_menu._adding[(CHAT, USER)][0] == PROMPT_ID  # the new prompt is the one to drop
    assert notes_menu.waiting_for_note(_typed("Код домофона 4589"))


def test_cancel_returns_to_the_notes_and_ends_the_wait(db):
    _press_run(notes_menu.cb_add, notes_menu.ADD)
    text, buttons = _press_run(notes_menu.cb_screen, notes_menu.SCREEN)
    assert "пока пусто" in text and notes_menu.ADD in buttons
    assert not notes_menu.waiting_for_note(_typed("просто вопрос"))


@pytest.mark.parametrize("text", ["📱 Устройства", "❓ Помощь", "/status", "📝 Заметки"])
def test_a_menu_button_or_command_ends_the_wait(db, text):
    _press_run(notes_menu.cb_add, notes_menu.ADD)
    assert not notes_menu.waiting_for_note(_typed(text))
    assert not notes_menu.waiting_for_note(_typed("поставь паузу Мише"))


def test_another_inline_button_ends_the_wait(db):
    """A name typed for a device or a time typed for a pause never lands in the notes."""
    _press_run(notes_menu.cb_add, notes_menu.ADD)
    assert notes_menu.moved_on(_press("dmr:5:none:0")) is False  # never takes the press
    assert not notes_menu.waiting_for_note(_typed("Планшет Миши"))
    _press_run(notes_menu.cb_add, notes_menu.ADD)
    assert notes_menu.moved_on(_press(notes_menu.ADD)) is False
    assert notes_menu.waiting_for_note(_typed("Код домофона 4589"))


def test_delete_asks_first_in_red_and_then_removes(db):
    keep = family_svc.save_family_note(db, "Папа", "Код домофона 4589")
    gone = family_svc.save_family_note(db, "Мама", "Размер ноги Миши 36")

    text, buttons = _press_run(notes_menu.cb_pick, notes_menu.PICK)
    assert "Какую заметку удалить?" in text
    assert buttons[f"{notes_menu.CONFIRM}:{gone.id}"] == "🗑 1. Размер ноги Миши 36"
    assert buttons[f"{notes_menu.CONFIRM}:{keep.id}"] == "🗑 2. Код домофона 4589"
    assert buttons[notes_menu.SCREEN] == "← Назад"

    cb = _press(f"{notes_menu.CONFIRM}:{gone.id}")
    asyncio.run(notes_menu.cb_confirm(cb))
    text, buttons = _edited(cb)
    assert text == "Удалить заметку «Размер ноги Миши 36»?"
    delete = f"{notes_menu.DELETE}:{gone.id}"
    styles = {
        b.callback_data: b.style
        for row in cb.message.edit_text.await_args.kwargs["reply_markup"].inline_keyboard
        for b in row
    }
    assert styles[delete] == DANGER and buttons[notes_menu.PICK] == "← Назад"
    assert family_svc.count_family_notes(db) == 2  # nothing is gone before the red button

    text, _ = _press_run(notes_menu.cb_delete, delete)
    assert "🗑 Заметка удалена." in text and "Размер ноги" not in text
    assert [n.content for n in family_svc.get_family_notes(db)] == ["Код домофона 4589"]
    assert db.query(EventLog).filter_by(kind="family_note_deleted").count() == 1

    text, _ = _press_run(notes_menu.cb_delete, delete)  # a second tap on an old button
    assert notes_menu.GONE in text


def test_nothing_to_delete_goes_back_to_the_notes(db):
    text, buttons = _press_run(notes_menu.cb_pick, notes_menu.PICK)
    assert "Удалять нечего." in text and notes_menu.ADD in buttons


def test_the_screen_says_when_older_notes_are_not_shown(db):
    for i in range(notes_menu.ON_SCREEN + 2):
        family_svc.save_family_note(db, "Папа", f"Заметка {i}")
    text, _ = _press_run(notes_menu.cb_screen, notes_menu.SCREEN)
    assert f"Показаны последние {notes_menu.ON_SCREEN} из {notes_menu.ON_SCREEN + 2}." in text


def test_note_command_saves_at_once_or_asks_like_the_button(db):
    message = _typed("/note код домофона 4589")
    asyncio.run(notes_menu.cmd_note(message, CommandObject(command="note", args="код 4589")))
    text, _ = _sent(message.bot)
    assert "✅ Заметка сохранена: «код 4589»" in text

    message = _typed("/note")
    asyncio.run(notes_menu.cmd_note(message, CommandObject(command="note", args=None)))
    text, buttons = _sent(message.bot)
    assert text == notes_menu.ASK and notes_menu.SCREEN in buttons
    assert notes_menu.waiting_for_note(_typed("Размер ноги Миши 36"))


@pytest.mark.parametrize("role", ["kid", "guest", "unauthorized"])
def test_only_parents_use_the_notes(role):
    assert not access.check_message(role, notes_menu.BTN_NOTES).allowed
    assert not access.check_callback(role, f"{notes_menu.DELETE}:1").allowed


def test_both_parents_manage_the_notes():
    for data in (notes_menu.ADD, notes_menu.PICK, f"{notes_menu.DELETE}:1"):
        assert access.check_callback("admin", data).allowed


def test_the_wait_is_checked_before_any_other_handler():
    messages = [h.callback for h in handlers.router.message.handlers]
    assert messages[0] is notes_menu.on_note
    assert messages.index(notes_menu.btn_notes) < messages.index(handlers.free_text)
    callbacks = [h.callback for h in handlers.router.callback_query.handlers]
    assert callbacks[0] is notes_menu._never
    assert {notes_menu.cb_add, notes_menu.cb_confirm, notes_menu.cb_delete} <= set(callbacks)


def test_callback_data_fits_telegram_limit():
    assert len(f"{notes_menu.DELETE}:{10**12}".encode()) <= 64
