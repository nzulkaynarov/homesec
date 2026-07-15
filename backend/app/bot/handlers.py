"""Команды и кнопки бота. Хендлеры тонкие: вся логика — в app.ai.tools (общий
реестр инструментов с панелью и ИИ), здесь только парсинг и форматирование.
Явная команда человека (/block и т.п.) — это уже подтверждение, поэтому
мутации исполняются сразу; подтверждение кнопкой нужно только действиям ИИ."""

import asyncio
import logging
from collections import defaultdict, deque
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .. import db as dbmod
from ..ai import client as ai_client
from ..ai import orchestrator, tools
from ..models import GROUP_LABELS, GROUPS, Person
from . import texts

log = logging.getLogger("homesec.bot")
router = Router()

# Мутации, предложенные ИИ и ждущие кнопку, лежат в таблице pending_actions
# (tools.save_pending/pop_pending) — кнопки переживают рестарты и деплой.

# Короткая память диалога с ИИ на чат (только текстовые реплики, без tool call'ов)
CHAT_HISTORY: dict[int, deque] = defaultdict(lambda: deque(maxlen=6))


def run_tool_sync(name: str, args: dict, source: str = "bot") -> Any:
    """Инструмент в отдельной сессии; для вызова через asyncio.to_thread."""
    s = dbmod.session()
    try:
        return tools.run_tool(s, name, args, source=source)
    finally:
        s.close()


def _save_pending(action: orchestrator.PendingAction) -> int:
    s = dbmod.session()
    try:
        return tools.save_pending(s, action.tool, action.args, action.description)
    finally:
        s.close()


def _pop_pending(pending_id: int) -> tuple[str, dict, str] | None:
    s = dbmod.session()
    try:
        return tools.pop_pending(s, pending_id)
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


# ---------- выбор устройства кнопками ----------

MAX_PICK_BUTTONS = 12  # длиннее список — нечитаемая простыня кнопок


def device_pick_keyboard(devices: list[tuple[int, str]], tool: str,
                         extra: str = "") -> InlineKeyboardMarkup:
    """Кнопки-кандидаты. devices: (id, имя). Callback-данные:
    pick:<tool>:<dev_id><extra>, где extra — доп. аргументы команды,
    напр. «:60» для /pause или «:30:games» для /bonus."""
    rows = [
        [InlineKeyboardButton(text=name, callback_data=f"pick:{tool}:{dev_id}{extra}")]
        for dev_id, name in devices[:MAX_PICK_BUTTONS]
    ]
    rows.append([InlineKeyboardButton(text="✖ Отмена", callback_data="pick:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _find_candidates(query: str) -> list[tuple[int, str]]:
    s = dbmod.session()
    try:
        return [(d.id, d.name) for d in tools.find_device_candidates(s, query)]
    finally:
        s.close()


async def _pick_device(message: Message, query: str, tool: str, extra: str = "") -> None:
    """Когда имя не задано или неоднозначно — кнопки с кандидатами вместо
    ложного «не нашёл»."""
    candidates = await asyncio.to_thread(_find_candidates, query)
    if not candidates:
        await message.answer(f"Не нашёл устройство «{query}». /devices покажет список.")
        return
    text = (f"Нашлось несколько устройств по «{query}» — выберите:" if query
            else "Выберите устройство:")
    if len(candidates) > MAX_PICK_BUTTONS:
        text += f"\n(показаны первые {MAX_PICK_BUTTONS} — уточните имя)"
    await message.answer(text, reply_markup=device_pick_keyboard(candidates, tool, extra))


async def _target_command(message: Message, tool_name: str, target: str,
                          args: dict, extra: str = "") -> None:
    """Команды с целью-строкой (пауза/резюм/бонус): группа исполняется сразу,
    устройство — с кнопками выбора при пустом или неоднозначном имени."""
    if target in GROUPS:
        await _do_tool(message, tool_name, {"target": target, **args})
        return
    if target:
        dev_id = await asyncio.to_thread(_find_device_id, target)
        if dev_id is not None:
            await _do_tool(message, tool_name, {"target": str(dev_id), **args})
            return
    await _pick_device(message, target, tool_name, extra)


@router.callback_query(F.data.startswith("pick:"))
async def cb_pick(cb: CallbackQuery) -> None:
    parts = (cb.data or "").split(":")
    if parts[1] == "cancel":
        await cb.answer()
        if isinstance(cb.message, Message):
            await cb.message.edit_text("Отменено.", reply_markup=None)
        return
    tool_name, dev_id = parts[1], int(parts[2])
    args: dict[str, Any]
    if tool_name in ("block_device", "unblock_device"):
        args = {"device_id": dev_id}
    elif tool_name == "pause_internet":
        args = {"target": str(dev_id), "minutes": int(parts[3])}
    elif tool_name == "resume_internet":
        args = {"target": str(dev_id)}
    elif tool_name == "add_bonus_time":
        args = {"target": str(dev_id), "minutes": int(parts[3]), "category": parts[4]}
    else:
        await cb.answer()
        return
    try:
        result = await asyncio.to_thread(run_tool_sync, tool_name, args)
    except tools.ToolError as e:
        result = f"Не получилось: {e}"
    except Exception:
        log.exception("инструмент %s упал", tool_name)
        result = "Ошибка при выполнении, подробности в журнале."
    await cb.answer()
    if isinstance(cb.message, Message):
        await cb.message.edit_text(f"{cb.message.text}\n\n➡ {result}", reply_markup=None)


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await message.answer(texts.START)


@router.message(Command("help"))
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
    if not query:  # без аргумента — выбор из всех устройств кнопками
        await _pick_device(message, "", tool_name)
        return
    dev_id = await asyncio.to_thread(_find_device_id, query)
    if dev_id is None:  # неоднозначно или не найдено — кнопки с кандидатами
        await _pick_device(message, query, tool_name)
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
    if not parts or not parts[-1].isdigit():
        await message.answer("Использование: /pause [имя|группа] <минут>\n"
                             "Например: /pause kid 60 или просто /pause 60 — выбор кнопками")
        return
    target, minutes = " ".join(parts[:-1]), int(parts[-1])
    await _target_command(message, "pause_internet", target,
                          {"minutes": minutes}, extra=f":{minutes}")


@router.message(Command("resume"))
async def cmd_resume(message: Message, command: CommandObject) -> None:
    target = (command.args or "").strip()
    await _target_command(message, "resume_internet", target, {})


@router.message(Command("bonus"))
async def cmd_bonus(message: Message, command: CommandObject) -> None:
    from ..services.quota import QUOTA_CATEGORIES

    parts = (command.args or "").split()
    usage = ("Использование: /bonus [кто] <минут> [категория]\n"
             "Например: /bonus Миша 30 games (без имени — выбор кнопками)\n"
             f"Категории: {', '.join(QUOTA_CATEGORIES)} (по умолчанию internet)")
    category = "internet"
    if parts and parts[-1].lower() in QUOTA_CATEGORIES:
        category = parts.pop().lower()
    if not parts or not parts[-1].isdigit():
        await message.answer(usage)
        return
    target, minutes = " ".join(parts[:-1]), int(parts[-1])
    await _target_command(message, "add_bonus_time", target,
                          {"minutes": minutes, "category": category},
                          extra=f":{minutes}:{category}")


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


def merge_keyboard(duplicate_id: int, target_id: int) -> InlineKeyboardMarkup:
    """Кнопки к подозрению «это то же устройство с новым MAC»."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔁 Объединить",
                             callback_data=f"mg:{duplicate_id}:{target_id}"),
        InlineKeyboardButton(text="Это разные устройства",
                             callback_data=f"mg:{duplicate_id}:{target_id}:no"),
    ]])


@router.callback_query(F.data.startswith("mg:"))
async def cb_merge(cb: CallbackQuery) -> None:
    parts = (cb.data or "").split(":")
    duplicate_id, target_id = int(parts[1]), int(parts[2])
    if len(parts) > 3 and parts[3] == "no":
        result = "Хорошо, оставляю как два разных устройства."
    else:
        try:
            result = await asyncio.to_thread(
                run_tool_sync, "merge_devices",
                {"duplicate_id": duplicate_id, "target_id": target_id},
            )
        except tools.ToolError as e:
            result = f"Не получилось: {e}"
        except Exception:
            log.exception("merge_devices упал")
            result = "Ошибка при объединении, подробности в журнале."
    await cb.answer()
    if isinstance(cb.message, Message):
        await cb.message.edit_text(f"{cb.message.text}\n\n➡ {result}", reply_markup=None)


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
        pending_id = await asyncio.to_thread(_save_pending, action)
        await message.answer(
            f"Подтвердите действие:\n{action.description}",
            reply_markup=confirm_keyboard(pending_id),
        )


@router.callback_query(F.data.startswith("act:"))
async def cb_confirm_action(cb: CallbackQuery) -> None:
    parts = (cb.data or "").split(":")
    pending_id, decision = int(parts[1]), parts[2]
    action = await asyncio.to_thread(_pop_pending, pending_id)
    if action is None:
        await cb.answer("Действие устарело или уже обработано — повторите запрос",
                        show_alert=True)
        return
    tool_name, tool_args, _description = action
    if decision != "yes":
        result = "Отменено."
    else:
        try:
            result = await asyncio.to_thread(run_tool_sync, tool_name, tool_args, "ai")
        except tools.ToolError as e:
            result = f"Не получилось: {e}"
        except Exception:
            log.exception("подтверждённое действие %s упало", tool_name)
            result = "Ошибка при выполнении, подробности в журнале."
    await cb.answer()
    if isinstance(cb.message, Message):
        await cb.message.edit_text(f"{cb.message.text}\n\n➡ {result}", reply_markup=None)
