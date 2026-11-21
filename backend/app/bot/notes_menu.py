"""«📝 Заметки»: the family's shared memory. Read it, add a note by typing it, delete one after
a confirmation. Everything works with buttons and without the AI."""

import asyncio
import logging
import time
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .. import db as dbmod
from ..models import FamilyNote
from ..services import family as family_svc
from . import access, rich, screens
from .texts import DANGER

log = logging.getLogger("homesec.bot.notes")

BTN_NOTES = "📝 Заметки"
SCREEN = "nt"  # the notes; also "← Отмена" while typing a note
ADD = "nta"  # ask for a note
PICK = "ntd"  # which note to delete
CONFIRM = "ntx"  # ntx:<note id>: ask before deleting
DELETE = "ntxy"  # ntxy:<note id>: delete it
ON_SCREEN = 10
INPUT_SECONDS = 300
MAX_LENGTH = 1000
ASK = "📝 Что запомнить? Напишите одним сообщением, например: размер ноги Миши 36."
TOO_LONG = f"Это длиннее {MAX_LENGTH} знаков. Напишите заметку короче одним сообщением."
GONE = "Этой заметки уже нет."
MENU_TEXTS = access.ADULT_BUTTONS | {"❓ Помощь"}
Card = tuple[str, InlineKeyboardMarkup]

# (chat, user) -> (the prompt message, deadline): who is typing a note now.
_adding: dict[tuple[int, int], tuple[int, float]] = {}


def _button(text: str, data: str, style: str | None = None) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text[:60], callback_data=data, style=style)


def _in_session(work: Any, *args: Any) -> Any:
    session = dbmod.session()
    try:
        return work(session, *args)
    finally:
        session.close()


async def _call(work: Any, *args: Any) -> Any:
    return await asyncio.to_thread(_in_session, work, *args)


def _short(text: str, limit: int = 40) -> str:
    line = " ".join(text.split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _screen_data(db: Any) -> tuple[str, bool]:
    notes = family_svc.get_family_notes(db, limit=ON_SCREEN)
    return screens.notes_html(notes, family_svc.count_family_notes(db)), bool(notes)


def _listing(db: Any) -> list[tuple[int, str]]:
    return [(n.id, n.content) for n in family_svc.get_family_notes(db, limit=ON_SCREEN)]


def _content(db: Any, note_id: int) -> str | None:
    note = db.get(FamilyNote, note_id)
    return note.content if note is not None else None


async def screen(notice: str = "") -> Card:
    html, has_notes = await _call(_screen_data)
    actions = [_button("➕ Добавить", ADD)]
    if has_notes:
        actions.append(_button("🗑 Удалить", PICK))
    prefix = rich.paragraph(notice) if notice else ""
    return prefix + html, InlineKeyboardMarkup(inline_keyboard=[actions])


async def pick() -> Card:
    notes = await _call(_listing)
    if not notes:
        return await screen("Удалять нечего.")
    rows = [
        [_button(f"🗑 {number}. {_short(content)}", f"{CONFIRM}:{note_id}")]
        for number, (note_id, content) in enumerate(notes, 1)
    ]
    rows.append([_button("← Назад", SCREEN)])
    return rich.paragraph("Какую заметку удалить?"), InlineKeyboardMarkup(inline_keyboard=rows)


async def confirm(note_id: int) -> Card:
    content = await _call(_content, note_id)
    if content is None:
        return await screen(GONE)
    rows = [
        [_button("🗑 Да, удалить", f"{DELETE}:{note_id}", DANGER)],
        [_button("← Назад", PICK)],
    ]
    text = rich.paragraph(f"Удалить заметку «{content}»?")
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def delete(note_id: int, by: str) -> Card:
    try:
        await _call(family_svc.delete_family_note, note_id, by)
    except LookupError:
        return await screen(GONE)
    return await screen("🗑 Заметка удалена.")


def _ask_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_button("← Отмена", SCREEN)]])


def waiting_for_note(message: Message) -> bool:
    """True when this sender was asked for a note and is typing it now."""
    if message.from_user is None:
        return False
    key = (message.chat.id, message.from_user.id)
    pending = _adding.get(key)
    if pending is None:
        return False
    text = message.text or ""
    if pending[1] < time.monotonic() or not text or text.startswith("/") or text in MENU_TEXTS:
        _adding.pop(key, None)  # another button or command means the parent moved on
        return False
    return True


async def _drop(message: Message, message_id: int) -> None:
    """The prompt has done its job: take it away so the chat keeps only the result."""
    if message.bot is None:
        return
    try:
        await message.bot.delete_message(message.chat.id, message_id)
    except TelegramBadRequest:
        log.info("note prompt %s is already gone", message_id)


async def _ask(message: Message, text: str) -> None:
    """Send a prompt as a new message and wait for the note under it."""
    if message.from_user is None:
        return
    sent = await rich.answer(message, rich.paragraph(text), _ask_markup())
    if sent is not None:
        deadline = time.monotonic() + INPUT_SECONDS
        _adding[(message.chat.id, message.from_user.id)] = (sent.message_id, deadline)


def _author(message: Message) -> str:
    return message.from_user.first_name if message.from_user else "Семья"


def _save(db: Any, author: str, content: str) -> str:
    return family_svc.save_family_note(db, author, content).content


async def _save_and_show(message: Message, content: str) -> None:
    saved = await _call(_save, _author(message), content)
    html, markup = await screen(f"✅ Заметка сохранена: «{saved}»")
    await rich.answer(message, html, markup)


async def on_note(message: Message) -> None:
    if message.from_user is None:
        return
    pending = _adding.pop((message.chat.id, message.from_user.id), None)
    if pending is None:
        return
    await _drop(message, pending[0])
    content = (message.text or "").strip()
    if len(content) > MAX_LENGTH:
        await _ask(message, TOO_LONG)
        return
    await _save_and_show(message, content)


async def btn_notes(message: Message) -> None:
    html, markup = await screen()
    await rich.answer(message, html, markup)


async def cmd_note(message: Message, command: CommandObject) -> None:
    """/note text saves at once; a bare /note asks for the text like «➕ Добавить»."""
    content = (command.args or "").strip()
    if not content:
        await _ask(message, ASK)
    elif len(content) > MAX_LENGTH:
        await _ask(message, TOO_LONG)
    else:
        await _save_and_show(message, content)


async def _show(cb: CallbackQuery, card: Card) -> None:
    await cb.answer()
    if isinstance(cb.message, Message):
        await rich.edit(cb.message, card[0], card[1])


def _forget(cb: CallbackQuery) -> None:
    if isinstance(cb.message, Message):
        _adding.pop((cb.message.chat.id, cb.from_user.id), None)


def moved_on(cb: CallbackQuery) -> bool:
    """Any other inline button ends the wait for a note, so a name or a time typed for another
    screen never lands in the notes. It never takes the press itself."""
    if cb.data != ADD:
        _forget(cb)
    return False


async def _never(cb: CallbackQuery) -> None:  # moved_on never lets a press reach it
    await cb.answer()


def _note_id(cb: CallbackQuery) -> int | None:
    raw = (cb.data or "").split(":", 1)[-1]
    return int(raw) if raw.isdigit() else None


async def cb_screen(cb: CallbackQuery) -> None:
    _forget(cb)  # "← Отмена" while typing a note lands here
    await _show(cb, await screen())


async def cb_add(cb: CallbackQuery) -> None:
    if isinstance(cb.message, Message):
        deadline = time.monotonic() + INPUT_SECONDS
        _adding[(cb.message.chat.id, cb.from_user.id)] = (cb.message.message_id, deadline)
    await _show(cb, (rich.paragraph(ASK), _ask_markup()))


async def cb_pick(cb: CallbackQuery) -> None:
    _forget(cb)
    await _show(cb, await pick())


async def cb_confirm(cb: CallbackQuery) -> None:
    note_id = _note_id(cb)
    await _show(cb, await confirm(note_id) if note_id is not None else await screen(GONE))


async def cb_delete(cb: CallbackQuery) -> None:
    note_id = _note_id(cb)
    if note_id is None:
        await _show(cb, await screen(GONE))
        return
    await _show(cb, await delete(note_id, cb.from_user.first_name))


def register(router: Router) -> None:
    """Attach first to the router that carries the access middleware.

    The typed note and moved_on are checked before every other handler, so a menu button, a
    command or another inline button ends the wait before its own handler runs, and the next
    free text goes to the AI, not into the notes."""
    router.callback_query.register(_never, moved_on)
    router.message.register(on_note, waiting_for_note)
    router.message.register(btn_notes, F.text == BTN_NOTES)
    router.message.register(btn_notes, Command("notes"))
    router.message.register(cmd_note, Command("note"))
    router.callback_query.register(cb_screen, F.data == SCREEN)
    router.callback_query.register(cb_add, F.data == ADD)
    router.callback_query.register(cb_pick, F.data == PICK)
    router.callback_query.register(cb_confirm, F.data.startswith(f"{CONFIRM}:"))
    router.callback_query.register(cb_delete, F.data.startswith(f"{DELETE}:"))
