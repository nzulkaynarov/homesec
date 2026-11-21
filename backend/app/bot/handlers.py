"""Команды и кнопки бота. Хендлеры тонкие: вся логика — в app.ai.tools (общий
реестр инструментов с панелью и ИИ), здесь только парсинг и форматирование.
Явная команда человека (/block и т.п.) — это уже подтверждение, поэтому
мутации исполняются сразу; подтверждение кнопкой нужно только действиям ИИ."""

import asyncio
import html
import logging
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from .. import db as dbmod
from ..ai import client as ai_client
from ..ai import orchestrator, tools
from ..config import settings
from ..models import GROUPS
from ..services import family as family_svc
from ..services import house_rules, pauses
from . import (
    alerts,
    charts_menu,
    common,
    device_menu,
    home_menu,
    notes_menu,
    onboarding,
    pause_menu,
    reports,
    rules_menu,
    server_menu,
    shared_menu,
    texts,
    triage,
)
from .access import AccessMiddleware
from .texts import SUCCESS

log = logging.getLogger("homesec.bot")
router = Router()

# Мутации, предложенные ИИ и ждущие кнопку, лежат в таблице pending_actions
# (tools.save_pending/pop_pending) — кнопки переживают рестарты и деплой.


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
        result = await asyncio.to_thread(common.run_tool_sync, name, args)
    except tools.ToolError as e:
        await message.answer(str(e))
        return
    except Exception:
        log.exception("инструмент %s упал", name)
        await message.answer("Что-то пошло не так, подробности в журнале бота.")
        return
    await message.answer(str(result))


# ---------- семейные мелочи ----------


def _movie_sync() -> str:
    s = dbmod.session()
    try:
        return family_svc.set_movie_mode(s)
    finally:
        s.close()


_access = AccessMiddleware(onboarding.notify_owner_about_access_request, texts.START_UNAUTHORIZED)
router.message.outer_middleware(_access)
router.callback_query.outer_middleware(_access)
notes_menu.register(router)  # first: its wait for a typed note ends on any other button
triage.register(router)
device_menu.register(router)
pause_menu.register(router)
rules_menu.register(router)
shared_menu.register(router)
alerts.register(router)
onboarding.register(router)
reports.register(router)
server_menu.register(router)
charts_menu.register(router)
home_menu.register(router)


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
        await message.answer(f"Не нашёл устройство «{query}». Все устройства — в «📱 Устройства».")
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
        result = await asyncio.to_thread(common.run_tool_sync, tool_name, args)
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
    user_id = message.from_user.id if message.from_user else 0
    role, _ = await asyncio.to_thread(common.get_user_role_sync, user_id)
    kb = common.keyboard_for_role(role)
    if role == "unauthorized":
        await message.answer(texts.START_UNAUTHORIZED)
        await onboarding.notify_owner_about_access_request(message)
        return
    await message.answer(texts.start_text(role), reply_markup=kb)


@router.message(Command("help"))
@router.message(F.text == "❓ Помощь")
async def cmd_help(message: Message) -> None:
    user_id = message.from_user.id if message.from_user else 0
    role, _ = await asyncio.to_thread(common.get_user_role_sync, user_id)
    await message.answer(texts.help_text(role), reply_markup=common.keyboard_for_role(role))


@router.message(Command("devices"))
async def cmd_devices(message: Message) -> None:
    await device_menu.btn_devices(message)


@router.message(Command("movie"))
async def cmd_movie(message: Message) -> None:
    await message.answer(await asyncio.to_thread(_movie_sync))


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


def _resume_all_sync() -> int:
    s = dbmod.session()
    try:
        return house_rules.resume_all(s)
    finally:
        s.close()


@router.message(Command("pause"))
async def cmd_pause(message: Message, command: CommandObject) -> None:
    raw = (command.args or "").strip()
    if not raw:
        text, markup = await pause_menu.overview()
        await message.answer(text, reply_markup=markup)
        return
    parts = raw.split()
    if len(parts) == 1 and parts[0].isdigit():
        minutes = int(parts[0])
        await _pick_device(message, "", "pause_internet", extra=f":{minutes}")
        return
    if not parts[-1].isdigit():
        await message.answer(
            "Использование: /pause [кто] <минут>\n"
            "Например: /pause дети 60 или /pause 30 — выбор кнопками\n"
            "Либо просто /pause — кнопки «⏸ Пауза»."
        )
        return
    target, minutes = _kids_word(" ".join(parts[:-1])), int(parts[-1])
    await _target_command(message, "pause_internet", target,
                          {"minutes": minutes}, extra=f":{minutes}")


def _kids_word(target: str) -> str:
    """«дети», «всем детям» and the like mean every child, as the «👶 Все дети» button does."""
    return "kid" if target.casefold() in pauses.KIDS_WORDS else target


@router.message(Command("resume"))
async def cmd_resume(message: Message, command: CommandObject) -> None:
    target = _kids_word((command.args or "").strip())
    await _target_command(message, "resume_internet", target, {})


# The limits a /bonus adds to, by the words parents use; the codes still work for old habits.
BONUS_WORDS = {"интернет": "internet", "игры": "games", "видео": "video", "соцсети": "social"}


@router.message(Command("bonus"))
async def cmd_bonus(message: Message, command: CommandObject) -> None:
    from ..services.quota import QUOTA_CATEGORIES

    parts = (command.args or "").split()
    usage = ("Использование: /bonus [кто] <минут> [на что]\n"
             "Например: /bonus Миша 30 игры (без имени — выбор кнопками)\n"
             f"На что: {', '.join(BONUS_WORDS)} (по умолчанию интернет)")
    category = "internet"
    if parts and parts[-1].lower() in QUOTA_CATEGORIES:
        category = parts.pop().lower()
    elif parts and parts[-1].lower() in BONUS_WORDS:
        category = BONUS_WORDS[parts.pop().lower()]
    if not parts or not parts[-1].isdigit():
        await message.answer(usage)
        return
    target, minutes = " ".join(parts[:-1]), int(parts[-1])
    await _target_command(message, "add_bonus_time", target,
                          {"minutes": minutes, "category": category},
                          extra=f":{minutes}:{category}")


# ---------- свободный текст -> ИИ-оркестратор ----------

def _orchestrate(chat_id: int, text: str) -> orchestrator.Answer:
    s = dbmod.session()
    try:
        return orchestrator.handle(s, text, history=list(common.CHAT_HISTORY[chat_id]))
    finally:
        s.close()


def confirm_keyboard(pending_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="✅ Выполнить",
            callback_data=f"act:{pending_id}:yes",
            style=SUCCESS,
        ),
        InlineKeyboardButton(text="❌ Отмена", callback_data=f"act:{pending_id}:no"),
    ]])


async def _process_user_nl_prompt(message: Message, user_text: str) -> None:
    """Обрабатывает текстовый ввод (набранный или распознанный из аудио) через ИИ."""
    user_id = message.from_user.id if message.from_user else 0
    role, person = await asyncio.to_thread(common.get_user_role_sync, user_id)
    if role == "unauthorized":
        await message.answer(texts.START_UNAUTHORIZED)
        await onboarding.notify_owner_about_access_request(message)
        return

    if not common.allow_ai_request(user_id):
        await message.answer("Слишком много сообщений подряд. Подождите минуту 🙂")
        return

    if not ai_client.is_configured():
        await message.answer(
            "Свободный текст понимает ИИ, а он сейчас не настроен. "
            "Всё работает кнопками внизу, подсказка — «❓ Помощь»."
        )
        return
    try:
        with ai_client.attribute(common.caller(person, user_id, "orchestrator", role)):
            answer = await asyncio.to_thread(_orchestrate, message.chat.id, user_text)
    except ai_client.AiError as e:
        await message.answer(str(e))
        return
    except Exception:
        log.exception("оркестратор упал")
        await message.answer(
            "ИИ споткнулся, подробности в журнале бота. Кнопки внизу работают как обычно."
        )
        return

    common.CHAT_HISTORY[message.chat.id].append({"role": "user", "content": user_text})
    common.CHAT_HISTORY[message.chat.id].append({"role": "assistant", "content": answer.text})
    await common.send_long(message, answer.text)
    for action in answer.pending:
        pending_id = await asyncio.to_thread(_save_pending, action)
        await message.answer(
            f"Подтвердите действие:\n{action.description}",
            reply_markup=confirm_keyboard(pending_id),
        )


VOICE_UNAVAILABLE = (
    "Голосовые сейчас не понимаю: ИИ недоступен. "
    "Напишите текстом или нажмите кнопку внизу."
)
VOICE_NOT_RECEIVED = "Голосовое до меня не дошло, отправьте его ещё раз."
VOICE_FAILED = (
    "Не получилось разобрать голосовое: ИИ сейчас недоступен. "
    "Напишите текстом или нажмите кнопку внизу."
)


def _record_voice_spend_sync(caller: ai_client.Caller, seconds: int) -> None:
    # Gemini bills roughly 32 tokens per second of audio at the input price.
    s = dbmod.session()
    try:
        ai_client.record_spend(s, caller, ai_client.estimate_usd("haiku", seconds * 32, 0))
    finally:
        s.close()


@router.message(F.voice)
async def handle_voice(message: Message) -> None:
    """Голосовое сообщение от пользователя: скачивает ogg аудиофайл,
    транскрибирует через LunaGPT STT и передаёт в ИИ-оркестратор."""
    if not ai_client.is_configured() or not settings.ai_base_url:
        log.warning("a voice message arrived, but speech to text is not configured")
        await message.answer(VOICE_UNAVAILABLE)
        return

    if not message.voice or message.bot is None:
        return
    if message.voice.duration > settings.ai_max_voice_seconds:
        await message.answer(
            f"Слишком длинное голосовое, максимум {settings.ai_max_voice_seconds} секунд."
        )
        return

    user_id = message.from_user.id if message.from_user else 0
    role, person = await asyncio.to_thread(common.get_user_role_sync, user_id)
    caller = common.caller(person, user_id, "stt", role)

    try:
        await message.bot.send_chat_action(message.chat.id, "typing")
        file_info = await message.bot.get_file(message.voice.file_id)
        if not file_info.file_path:
            await message.answer(VOICE_NOT_RECEIVED)
            return

        voice_buf = await message.bot.download_file(file_info.file_path)
        if voice_buf is None:
            await message.answer(VOICE_NOT_RECEIVED)
            return
        voice_bytes = voice_buf.getvalue() if hasattr(voice_buf, "getvalue") else voice_buf.read()

        transcribed_text = await ai_client.transcribe(voice_bytes, "ogg", caller)
    except Exception:
        log.exception("ошибка транскрипции голосового сообщения")
        await message.answer(VOICE_FAILED)
        return

    await asyncio.to_thread(_record_voice_spend_sync, caller, message.voice.duration)

    if not transcribed_text:
        await message.answer("Не удалось разобрать речь. Попробуйте сказать четче.")
        return

    await message.answer(f"🗣 <i>«{html.escape(transcribed_text)}»</i>", parse_mode="HTML")
    await _process_user_nl_prompt(message, transcribed_text)


@router.message(F.text)
async def free_text(message: Message) -> None:
    await _process_user_nl_prompt(message, message.text or "")


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
            result = await asyncio.to_thread(common.run_tool_sync, tool_name, tool_args, "ai")
        except tools.ToolError as e:
            result = f"Не получилось: {e}"
        except Exception:
            log.exception("подтверждённое действие %s упало", tool_name)
            result = "Ошибка при выполнении, подробности в журнале."
    await cb.answer()
    if isinstance(cb.message, Message):
        await cb.message.edit_text(f"{cb.message.text}\n\n➡ {result}", reply_markup=None)
