"""«📝 Заметки»: the family's shared memory. Read it page by page, add a note by typing it,
delete any one after a confirmation. Everything works with buttons and without the AI."""

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
from . import rich, screens, waits
from .texts import DANGER

log = logging.getLogger("homesec.bot.notes")

BTN_NOTES = "📝 Заметки"
SCREEN = "nt"  # the notes, newest first; nt:<page> for older pages; also "← Отмена" while typing
ADD = "nta"  # ask for a note
PICK = "ntd"  # which note to delete; ntd:<page> for older pages
CONFIRM = "ntx"  # ntx:<note id>:<page>: ask before deleting
DELETE = "ntxy"  # ntxy:<note id>:<page>: delete it
ON_SCREEN = 10
INPUT_SECONDS = 300
MAX_LENGTH = 1000
ASK = "📝 Что запомнить? Напишите одним сообщением, например: размер ноги Миши 36."
TOO_LONG = f"Это длиннее {MAX_LENGTH} знаков. Напишите заметку короче одним сообщением."
GONE = "Этой заметки уже нет."
Card = tuple[str, InlineKeyboardMarkup]

# (chat, user) -> (the prompt message, deadline): who is typing a note now.
_adding: dict[tuple[int, int], tuple[int, float]] = waits.table()


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


def _page(db: Any, page: int) -> tuple[list[FamilyNote], int, int, int]:
    """(the notes on the page, how many there are, the page shown, how many pages); a page
    past the end shows the last one, so a deletion never leaves an empty page."""
    total = family_svc.count_family_notes(db)
    pages = max(1, -(-total // ON_SCREEN))
    page = max(0, min(page, pages - 1))
    notes = family_svc.get_family_notes(db, limit=ON_SCREEN, offset=page * ON_SCREEN)
    return notes, total, page, pages


def _screen_data(db: Any, page: int) -> tuple[str, bool, int, int]:
    notes, total, page, pages = _page(db, page)
    return screens.notes_html(notes, total, page, pages), bool(notes), page, pages


def _listing(db: Any, page: int) -> tuple[list[tuple[int, str]], int, int]:
    notes, _, page, pages = _page(db, page)
    return [(n.id, n.content) for n in notes], page, pages


def _content(db: Any, note_id: int) -> str | None:
    note = db.get(FamilyNote, note_id)
    return note.content if note is not None else None


def _at(data: str, page: int) -> str:
    """Callback data on a page; the first page keeps the data it always had."""
    return data if page == 0 else f"{data}:{page}"


def _pages_row(base: str, page: int, pages: int) -> list[InlineKeyboardButton]:
    """«← Новее» and «Старее →» between the pages of notes, newest first."""
    row = []
    if page > 0:
        row.append(_button("← Новее", _at(base, page - 1)))
    if page < pages - 1:
        row.append(_button("Старее →", _at(base, page + 1)))
    return row


async def screen(notice: str = "", page: int = 0) -> Card:
    html, has_notes, page, pages = await _call(_screen_data, page)
    actions = [_button("➕ Добавить", ADD)]
    if has_notes:
        actions.append(_button("🗑 Удалить", _at(PICK, page)))
    rows = [row for row in (_pages_row(SCREEN, page, pages), actions) if row]
    prefix = rich.paragraph(notice) if notice else ""
    return prefix + html, InlineKeyboardMarkup(inline_keyboard=rows)


async def pick(page: int = 0) -> Card:
    notes, page, pages = await _call(_listing, page)
    if not notes:
        return await screen("Удалять нечего.")
    rows = [
        [_button(f"🗑 {number}. {_short(content)}", _at(f"{CONFIRM}:{note_id}", page))]
        for number, (note_id, content) in enumerate(notes, 1)
    ]
    navigation = _pages_row(PICK, page, pages)
    if navigation:
        rows.append(navigation)
    rows.append([_button("← Назад", _at(SCREEN, page))])
    title = "Какую заметку удалить?"
    if pages > 1:
        title += f" Страница {page + 1} из {pages}."
    return rich.paragraph(title), InlineKeyboardMarkup(inline_keyboard=rows)


async def confirm(note_id: int, page: int = 0) -> Card:
    content = await _call(_content, note_id)
    if content is None:
        return await screen(GONE, page)
    rows = [
        [_button("🗑 Да, удалить", _at(f"{DELETE}:{note_id}", page), DANGER)],
        [_button("← Назад", _at(PICK, page))],
    ]
    text = rich.paragraph(f"Удалить заметку «{content}»?")
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def delete(note_id: int, by: str, page: int = 0) -> Card:
    try:
        await _call(family_svc.delete_family_note, note_id, by)
    except LookupError:
        return await screen(GONE, page)
    return await screen("🗑 Заметка удалена.", page)


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
    if pending[1] < time.monotonic() or waits.leaves(message.text):
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
        waits.start(_adding, (message.chat.id, message.from_user.id), (sent.message_id, deadline))


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


def _numbers(cb: CallbackQuery) -> list[int | None]:
    """The numbers after the prefix: a page, or a note id and a page; buttons drawn before the
    pages had no page, which means the first one."""
    return [int(raw) if raw.isdigit() else None for raw in (cb.data or "").split(":")[1:]]


def _page_of(cb: CallbackQuery, position: int) -> int:
    numbers = _numbers(cb)
    page = numbers[position] if len(numbers) > position else None
    return page or 0


async def cb_screen(cb: CallbackQuery) -> None:
    _forget(cb)  # "← Отмена" while typing a note lands here
    await _show(cb, await screen(page=_page_of(cb, 0)))


async def cb_add(cb: CallbackQuery) -> None:
    if isinstance(cb.message, Message):
        deadline = time.monotonic() + INPUT_SECONDS
        key = (cb.message.chat.id, cb.from_user.id)
        waits.start(_adding, key, (cb.message.message_id, deadline))
    await _show(cb, (rich.paragraph(ASK), _ask_markup()))


async def cb_pick(cb: CallbackQuery) -> None:
    _forget(cb)
    await _show(cb, await pick(_page_of(cb, 0)))


async def cb_confirm(cb: CallbackQuery) -> None:
    note_id, page = (_numbers(cb) or [None])[0], _page_of(cb, 1)
    await _show(cb, await confirm(note_id, page) if note_id is not None else await screen(GONE))


async def cb_delete(cb: CallbackQuery) -> None:
    note_id, page = (_numbers(cb) or [None])[0], _page_of(cb, 1)
    if note_id is None:
        await _show(cb, await screen(GONE))
        return
    await _show(cb, await delete(note_id, cb.from_user.first_name, page))


def register(router: Router) -> None:
    """Attach to the router that carries the access middleware, after waits.register, which
    ends this wait on a menu button, a command or another inline button, and before the free
    text that goes to the AI."""
    router.message.register(on_note, waiting_for_note)
    router.message.register(btn_notes, F.text == BTN_NOTES)
    router.message.register(btn_notes, Command("notes"))
    router.message.register(cmd_note, Command("note"))
    router.callback_query.register(cb_screen, F.data == SCREEN)
    router.callback_query.register(cb_screen, F.data.startswith(f"{SCREEN}:"))
    router.callback_query.register(cb_add, F.data == ADD)
    router.callback_query.register(cb_pick, F.data == PICK)
    router.callback_query.register(cb_pick, F.data.startswith(f"{PICK}:"))
    router.callback_query.register(cb_confirm, F.data.startswith(f"{CONFIRM}:"))
    router.callback_query.register(cb_delete, F.data.startswith(f"{DELETE}:"))
