"""Команды и кнопки бота. Хендлеры тонкие: вся логика — в app.ai.tools (общий
реестр инструментов с панелью и ИИ), здесь только парсинг и форматирование.
Явная команда человека (/block и т.п.) — это уже подтверждение, поэтому
мутации исполняются сразу; подтверждение кнопкой нужно только действиям ИИ."""

import asyncio
import logging
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .. import db as dbmod
from ..ai import tools
from ..models import GROUP_LABELS, Person
from . import texts

log = logging.getLogger("homesec.bot")
router = Router()


def run_tool_sync(name: str, args: dict, source: str = "bot") -> Any:
    """Инструмент в отдельной сессии; для вызова через asyncio.to_thread."""
    s = dbmod.session()
    try:
        return tools.run_tool(s, name, args, source=source)
    finally:
        s.close()


def _find_device_id(query: str) -> int | None:
    s = dbmod.session()
    try:
        dev = tools.find_device(s, query)
        return dev.id if dev else None
    finally:
        s.close()


async def _do_tool(message: Message, name: str, args: dict) -> None:
    """Исполняет инструмент и отвечает результатом или текстом ошибки."""
    try:
        result = await asyncio.to_thread(run_tool_sync, name, args)
    except tools.ToolError as e:
        await message.answer(str(e))
        return
    except Exception:
        log.exception("инструмент %s упал", name)
        await message.answer("Что-то пошло не так, подробности в журнале бота.")
        return
    await message.answer(str(result))


@router.message(Command("start", "help"))
async def cmd_help(message: Message) -> None:
    await message.answer(texts.HELP)


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    status = await asyncio.to_thread(run_tool_sync, "get_status", {})
    await message.answer(texts.format_status(status))


@router.message(Command("devices"))
async def cmd_devices(message: Message) -> None:
    rows = await asyncio.to_thread(run_tool_sync, "list_devices", {})
    await message.answer(texts.format_devices(rows))


async def _device_command(message: Message, command: CommandObject, tool_name: str) -> None:
    query = (command.args or "").strip()
    if not query:
        await message.answer(f"Использование: /{command.command} <имя|id>")
        return
    dev_id = await asyncio.to_thread(_find_device_id, query)
    if dev_id is None:
        await message.answer(f"Не нашёл устройство «{query}». /devices покажет список.")
        return
    await _do_tool(message, tool_name, {"device_id": dev_id})


@router.message(Command("block"))
async def cmd_block(message: Message, command: CommandObject) -> None:
    await _device_command(message, command, "block_device")


@router.message(Command("unblock"))
async def cmd_unblock(message: Message, command: CommandObject) -> None:
    await _device_command(message, command, "unblock_device")


@router.message(Command("pause"))
async def cmd_pause(message: Message, command: CommandObject) -> None:
    parts = (command.args or "").split()
    if len(parts) < 2 or not parts[-1].isdigit():
        await message.answer("Использование: /pause <имя|группа> <минут>\nНапример: /pause kid 60")
        return
    target, minutes = " ".join(parts[:-1]), int(parts[-1])
    await _do_tool(message, "pause_internet", {"target": target, "minutes": minutes})


@router.message(Command("resume"))
async def cmd_resume(message: Message, command: CommandObject) -> None:
    target = (command.args or "").strip()
    if not target:
        await message.answer("Использование: /resume <имя|группа>")
        return
    await _do_tool(message, "resume_internet", {"target": target})


# ---------- кнопки под уведомлением о новом устройстве ----------

def new_device_keyboard(dev_id: int, people: list[tuple[int, str, str]]) -> InlineKeyboardMarkup:
    """people: (id, имя, роль). Callback-данные: nd:<dev_id>:<действие>[:<арг>]."""
    rows = [
        [InlineKeyboardButton(text=f"→ {name} ({GROUP_LABELS[role]})",
                              callback_data=f"nd:{dev_id}:assign:{pid}")]
        for pid, name, role in people
    ]
    rows.append([
        InlineKeyboardButton(text="⛔ Блокировать", callback_data=f"nd:{dev_id}:block"),
        InlineKeyboardButton(text="Оставить как есть", callback_data=f"nd:{dev_id}:skip"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _person_name(person_id: int) -> str | None:
    s = dbmod.session()
    try:
        person = s.get(Person, person_id)
        return person.name if person else None
    finally:
        s.close()


@router.callback_query(F.data.startswith("nd:"))
async def cb_new_device(cb: CallbackQuery) -> None:
    parts = (cb.data or "").split(":")
    dev_id, action = int(parts[1]), parts[2]
    try:
        if action == "skip":
            result = "Оставлено в «Неизвестных»"
        elif action == "block":
            result = await asyncio.to_thread(run_tool_sync, "block_device", {"device_id": dev_id})
        elif action == "assign":
            name = await asyncio.to_thread(_person_name, int(parts[3]))
            if name is None:
                await cb.answer("Этого человека уже нет в списке", show_alert=True)
                return
            result = await asyncio.to_thread(
                run_tool_sync, "assign_device", {"device_id": dev_id, "person_name": name}
            )
        else:
            await cb.answer()
            return
    except tools.ToolError as e:
        await cb.answer(str(e), show_alert=True)
        return
    await cb.answer("Готово")
    if isinstance(cb.message, Message):  # у устаревших сообщений править нечего
        await cb.message.edit_text(f"{cb.message.text}\n\n➡ {result}", reply_markup=None)


# Свободный текст (не команда): на этапе 2 сюда подключается ИИ-оркестратор.
@router.message(F.text)
async def fallback(message: Message) -> None:
    await message.answer("Пока я понимаю только команды — см. /help.")
