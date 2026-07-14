"""Команды и кнопки бота. Хендлеры тонкие: вся логика — в app.ai.tools (общий
реестр инструментов с панелью и ИИ), здесь только парсинг и форматирование.
Явная команда человека (/block и т.п.) — это уже подтверждение, поэтому
мутации исполняются сразу; подтверждение кнопкой нужно только действиям ИИ."""

import asyncio
import itertools
import logging
from collections import defaultdict, deque
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .. import db as dbmod
from ..ai import client as ai_client
from ..ai import orchestrator, tools
from ..models import GROUP_LABELS, Person
from . import texts

log = logging.getLogger("homesec.bot")
router = Router()

# Мутации, предложенные ИИ и ждущие кнопку. Живут в памяти процесса бота:
# после рестарта кнопка вежливо попросит повторить запрос.
PENDING_ACTIONS: dict[int, orchestrator.PendingAction] = {}
_pending_seq = itertools.count(1)

# Короткая память диалога с ИИ на чат (только текстовые реплики, без tool call'ов)
CHAT_HISTORY: dict[int, deque] = defaultdict(lambda: deque(maxlen=6))


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


def _digest_sync() -> str:
    from ..ai import analyst

    s = dbmod.session()
    try:
        return analyst.daily_digest(s)
    finally:
        s.close()


@router.message(Command("digest"))
async def cmd_digest(message: Message) -> None:
    await message.answer(await asyncio.to_thread(_digest_sync))


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


@router.message(Command("bonus"))
async def cmd_bonus(message: Message, command: CommandObject) -> None:
    from ..services.quota import QUOTA_CATEGORIES

    parts = (command.args or "").split()
    usage = ("Использование: /bonus <кто> <минут> [категория]\n"
             "Например: /bonus Миша 30 games\n"
             f"Категории: {', '.join(QUOTA_CATEGORIES)} (по умолчанию internet)")
    category = "internet"
    if parts and parts[-1].lower() in QUOTA_CATEGORIES:
        category = parts.pop().lower()
    if len(parts) < 2 or not parts[-1].isdigit():
        await message.answer(usage)
        return
    target, minutes = " ".join(parts[:-1]), int(parts[-1])
    await _do_tool(message, "add_bonus_time",
                   {"target": target, "minutes": minutes, "category": category})


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


# ---------- свободный текст -> ИИ-оркестратор ----------

def _orchestrate(chat_id: int, text: str) -> orchestrator.Answer:
    s = dbmod.session()
    try:
        return orchestrator.handle(s, text, history=list(CHAT_HISTORY[chat_id]))
    finally:
        s.close()


def confirm_keyboard(pending_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Выполнить", callback_data=f"act:{pending_id}:yes"),
        InlineKeyboardButton(text="❌ Отмена", callback_data=f"act:{pending_id}:no"),
    ]])


@router.message(F.text)
async def free_text(message: Message) -> None:
    if not ai_client.is_configured():
        await message.answer(
            "Свободный текст понимает ИИ, а он не настроен "
            "(HS_ANTHROPIC_API_KEY в .env). Пока доступны команды — /help."
        )
        return
    try:
        answer = await asyncio.to_thread(_orchestrate, message.chat.id, message.text or "")
    except ai_client.AiError as e:
        await message.answer(str(e))
        return
    except Exception:
        log.exception("оркестратор упал")
        await message.answer("ИИ споткнулся, подробности в журнале бота. Команды /help работают.")
        return

    CHAT_HISTORY[message.chat.id].append({"role": "user", "content": message.text or ""})
    CHAT_HISTORY[message.chat.id].append({"role": "assistant", "content": answer.text})
    await message.answer(answer.text)
    for action in answer.pending:
        pending_id = next(_pending_seq)
        PENDING_ACTIONS[pending_id] = action
        await message.answer(
            f"Подтвердите действие:\n{action.description}",
            reply_markup=confirm_keyboard(pending_id),
        )


@router.callback_query(F.data.startswith("act:"))
async def cb_confirm_action(cb: CallbackQuery) -> None:
    parts = (cb.data or "").split(":")
    pending_id, decision = int(parts[1]), parts[2]
    action = PENDING_ACTIONS.pop(pending_id, None)
    if action is None:
        await cb.answer("Действие устарело (бот перезапускался) — повторите запрос",
                        show_alert=True)
        return
    if decision != "yes":
        result = "Отменено."
    else:
        try:
            result = await asyncio.to_thread(
                run_tool_sync, action.tool, action.args, "ai"
            )
        except tools.ToolError as e:
            result = f"Не получилось: {e}"
        except Exception:
            log.exception("подтверждённое действие %s упало", action.tool)
            result = "Ошибка при выполнении, подробности в журнале."
    await cb.answer()
    if isinstance(cb.message, Message):
        await cb.message.edit_text(f"{cb.message.text}\n\n➡ {result}", reply_markup=None)
