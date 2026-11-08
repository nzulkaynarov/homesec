"""Команды и кнопки бота. Хендлеры тонкие: вся логика — в app.ai.tools (общий
реестр инструментов с панелью и ИИ), здесь только парсинг и форматирование.
Явная команда человека (/block и т.п.) — это уже подтверждение, поэтому
мутации исполняются сразу; подтверждение кнопкой нужно только действиям ИИ."""

import asyncio
import json
import logging
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta
from typing import Any

import httpx
from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from sqlalchemy import select

from .. import db as dbmod
from ..ai import client as ai_client
from ..ai import orchestrator, tools
from ..config import settings
from ..models import GROUP_LABELS, GROUPS, Device, Person, kv_get, log_event
from ..services import family as family_svc
from . import texts
from .access import AccessMiddleware

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


# ---------- клавиатуры по ролям и семейные элементы ----------

def keyboard_for_role(role: str) -> ReplyKeyboardMarkup | None:
    """Нижнее меню Telegram-кнопок в зависимости от роли в семье."""
    if role in ("owner", "admin"):
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="🏠 Кто дома"), KeyboardButton(text="⏳ Квоты")],
                [KeyboardButton(text="⏸ Пауза"), KeyboardButton(text="🍿 ТВ для взрослых")],
                [KeyboardButton(text="📝 Заметки"), KeyboardButton(text="❓ Помощь")],
            ],
            resize_keyboard=True,
        )
    if role == "kid":
        return ReplyKeyboardMarkup(
            keyboard=[
                [
                    KeyboardButton(text="⏳ Моё время"),
                    KeyboardButton(text="🙏 Попросить время"),
                ],
                [
                    KeyboardButton(text="🧠 Заработать +15м"),
                    KeyboardButton(text="🚀 Чем заняться?"),
                ],
                [
                    KeyboardButton(text="✅ Сделал дело"),
                    KeyboardButton(text="❓ Помощь"),
                ],
            ],
            resize_keyboard=True,
        )
    if role == "guest":
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="📶 Wi-Fi"), KeyboardButton(text="❓ Помощь")],
            ],
            resize_keyboard=True,
        )
    return None


def access_request_keyboard(requester_id: int) -> InlineKeyboardMarkup:
    """Кнопки родителю при онбординге нового участника семьи."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🛡 Родитель", callback_data=f"acc:{requester_id}:admin"),
            InlineKeyboardButton(text="🎒 Ребёнок", callback_data=f"acc:{requester_id}:kid"),
        ],
        [
            InlineKeyboardButton(text="🏡 Гость", callback_data=f"acc:{requester_id}:guest"),
            InlineKeyboardButton(text="⛔ Отклонить", callback_data=f"acc:{requester_id}:deny"),
        ],
    ])


def kid_explore_keyboard() -> InlineKeyboardMarkup:
    """Интерактивные сценарии для ребёнка в 1 клик."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🎲 Текстовый квест (RPG)", callback_data="kexp:quest"),
            InlineKeyboardButton(text="💻 Идея мини-игры", callback_data="kexp:game"),
        ],
        [
            InlineKeyboardButton(text="🇬🇧 Английский челлендж", callback_data="kexp:english"),
            InlineKeyboardButton(text="🤯 Факт дня", callback_data="kexp:fact"),
        ],
    ])


def quiz_keyboard(person_id: int, options: list[str]) -> InlineKeyboardMarkup:
    """Кнопки вариантов ответов для викторины ребёнка."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=opt, callback_data=f"qz:{person_id}:{idx}")]
        for idx, opt in enumerate(options)
    ])


def bonus_request_direct_keyboard(person_id: int) -> InlineKeyboardMarkup:
    """Кнопки родителям при прямом запросе времени ребёнком через Telegram."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="➕ 15 мин", callback_data=f"bdir:{person_id}:15"),
            InlineKeyboardButton(text="➕ 30 мин", callback_data=f"bdir:{person_id}:30"),
        ],
        [
            InlineKeyboardButton(text="➕ 1 час", callback_data=f"bdir:{person_id}:60"),
            InlineKeyboardButton(text="🌞 До конца дня", callback_data=f"bdir:{person_id}:day"),
        ],
        [
            InlineKeyboardButton(text="🚫 Отказать", callback_data=f"bdir:{person_id}:deny"),
        ],
    ])


# ---------- синхронные вспомогательные функции ----------

def _get_user_role_sync(telegram_id: int) -> tuple[str, Person | None]:
    s = dbmod.session()
    try:
        return family_svc.get_user_role(s, telegram_id)
    finally:
        s.close()


def _assign_telegram_access_sync(requester_id: int, role: str, requester_name: str) -> str:
    s = dbmod.session()
    try:
        if role == "deny":
            return "Запрос доступа отклонён."
        person = s.scalar(select(Person).where(Person.telegram_id == requester_id))
        if person is None:
            person = s.scalar(select(Person).where(Person.name == requester_name))
        if person is None:
            db_role = "adult" if role in ("owner", "admin") else (
                "kid" if role == "kid" else "guest"
            )
            person = Person(
                name=requester_name, role=db_role, telegram_id=requester_id, bot_role=role
            )
            s.add(person)
        else:
            person.telegram_id = requester_id
            person.bot_role = role
            if role in ("owner", "admin"):
                person.role = "adult"
            elif role == "kid":
                person.role = "kid"
            elif role == "guest":
                person.role = "guest"
        s.commit()
        role_label = family_svc.ROLE_LABELS.get(role, role)
        return f"Пользователю {requester_name} назначена роль: {role_label}."
    finally:
        s.close()


def _who_sync() -> str:
    s = dbmod.session()
    try:
        st = family_svc.get_presence_status(s)
        return family_svc.format_presence(st)
    finally:
        s.close()


def _movie_sync() -> str:
    s = dbmod.session()
    try:
        return family_svc.set_movie_mode(s)
    finally:
        s.close()


def _save_note_sync(author: str, content: str) -> str:
    s = dbmod.session()
    try:
        n = family_svc.save_family_note(s, author, content)
        return f"✅ Заметка сохранена: «{n.content}»"
    finally:
        s.close()


def _get_notes_sync() -> str:
    s = dbmod.session()
    try:
        notes = family_svc.get_family_notes(s)
        return family_svc.format_family_notes(notes)
    finally:
        s.close()


def _quiz_prepare_sync(user_id: int) -> tuple[bool, str, dict | None, int | None]:
    s = dbmod.session()
    try:
        role, person = family_svc.get_user_role(s, user_id)
        if person is None:
            return (
                False,
                "Вы не зарегистрированы в списке семьи. Обратитесь к родителям!",
                None,
                None,
            )
        if not family_svc.can_take_quiz_today(s, person.id):
            return (
                False,
                "Сегодня вы уже получали бонус за викторину! Приходите завтра 🧠",
                None,
                None,
            )
        active = family_svc.get_active_quiz(s, person.id)
        if active is not None:
            return True, "", active, person.id
        with ai_client.attribute(_caller(person, user_id, "quiz", role)):
            q = family_svc.generate_dynamic_quiz_question(s, person.id)
        family_svc.store_active_quiz(s, person.id, q)
        return True, "", q, person.id
    finally:
        s.close()


def _quiz_answer_sync(user_id: int, person_id: int, chosen_opt: int) -> dict[str, Any]:
    s = dbmod.session()
    try:
        _, person = family_svc.get_user_role(s, user_id)
        if person is None or person.id != person_id:
            return {"status": "error", "message": "Только автор вопроса может отвечать."}

        active = family_svc.get_active_quiz(s, person_id)
        if active is None:
            return {"status": "expired", "message": "Вопрос устарел или уже отвечен."}
        family_svc.clear_active_quiz(s, person_id)

        correct_idx = active.get("correct", 0)
        options = active.get("options", [])
        correct_text = options[correct_idx] if 0 <= correct_idx < len(options) else ""
        topic = active.get("topic", "Эрудиция")
        q_text = active.get("question", "")

        if chosen_opt != correct_idx:
            family_svc.mark_quiz_attempted(s, person.id)
            return {
                "status": "wrong",
                "message": (
                    f"❌ Не совсем так!\n"
                    f"Правильный ответ: *{texts.escape_markdown(str(correct_text))}*.\n\n"
                    f"Не расстраивайся, завтра будет новый вопрос и шанс заработать минуты! 🧠"
                ),
            }

        if not family_svc.can_take_quiz_today(s, person.id):
            return {
                "status": "already_taken",
                "message": "Сегодня вы уже получали бонус за викторину.",
            }

        bonus_msg = family_svc.award_quiz_bonus(s, person, minutes=15, topic=topic)
        return {
            "status": "correct",
            "message": (
                "🎉 *Абсолютно верно!* Ты большой молодец.\n\n"
                "Тебе начислено *+15 минут* интернета, "
                "а родителям отправлена весточка о твоём успехе! 🏆"
            ),
            "person_name": person.name,
            "question": q_text,
            "topic": topic,
            "bonus_msg": bonus_msg,
        }
    finally:
        s.close()


def _resolve_bonus_direct_sync(person_id: int, choice: str) -> tuple[str, int, str]:
    from ..models import QuotaBonus

    s = dbmod.session()
    try:
        person = s.get(Person, person_id)
        if person is None:
            return "Пользователь не найден.", 0, ""
        if choice == "deny":
            return f"Отказано в добавлении времени для {person.name}.", 0, person.name
        minutes = 720 if choice == "day" else int(choice)
        date = datetime.now().strftime("%Y-%m-%d")
        s.add(
            QuotaBonus(
                target_type="person",
                target=str(person.id),
                date=date,
                category="internet",
                minutes=minutes,
                comment="Одобрено родителем в Telegram",
            )
        )
        s.commit()
        label = "до конца дня" if choice == "day" else f"+{minutes} мин"
        return f"✅ Одобрено {label} интернета для {person.name}!", minutes, person.name
    finally:
        s.close()


def _tutor_ask_sync(chat_id: int, user_text: str) -> str:
    s = dbmod.session()
    try:
        messages = list(CHAT_HISTORY[chat_id]) + [{"role": "user", "content": user_text}]
        resp = ai_client.ask_with_fallback(
            s,
            ai_client.model_ladder(settings.ai_model_tutor, settings.ai_model_support),
            messages=messages,
            system=(
                "Ты — умный, добрый и вдохновляющий семейный ИИ-помощник LunaGPT для детей. "
                "Ты общаешься с ребёнком дружелюбно, с поддержкой и юмором на русском языке.\n\n"
                "ТВОИ ПРАВИЛА:\n"
                "1. ШКОЛА И УРОКИ (Метод Сократа): Если ребёнок просит решить за него задачу, "
                "уравнение или написать сочинение — НЕ давай готовый ответ! "
                "Объясни суть темы простыми словами на жизненных примерах (игры, пицца), "
                "покажи первый шаг и задай наводящий вопрос, чтобы ребёнок додумался сам. "
                "Хвали за каждую верную мысль.\n"
                "2. ТВОРЧЕСТВО, СКАЗКИ И КВЕСТЫ: Если ребёнок просит сочинить сказку, "
                "историю, сыграть в текстовую игру или придумать героя — "
                "включай творчество на полную мощность! Пиши увлекательно, живо и ярко.\n"
                "3. ПРОГРАММИРОВАНИЕ (Scratch, Python, Minecraft, Roblox): Помогай с кодом, "
                "объясняй логику и алгоритмы просто и понятно, вдохновляй делать свои проекты.\n"
                "4. НАУКА И МИР: Отвечай на вопросы («почему небо синее», «как летит ракета») "
                "наглядно и интересно, развивая любознательность.\n"
                "5. БЕЗОПАСНОСТЬ: Дружелюбный детский тон, без взрослого контента и негатива. "
                "Не давай опасных инструкций. Если ребёнку плохо, страшно или его обижают, "
                "поддержи и посоветуй сразу поговорить с родителями или взрослым. "
                "У тебя НЕТ инструментов управления роутером и блокировками домашней сети."
            ),
            max_tokens=1500,
        )
        content_text = ""
        for block in resp.content:
            if getattr(block, "type", "") == "text":
                content_text += block.text
        return content_text or "Я здесь! Задай мне любой вопрос или выбери занятие в меню."
    finally:
        s.close()


async def _notify_owner_about_access_request(message: Message) -> None:
    if not message.bot:
        return
    user = message.from_user
    if not user:
        return
    uname = f"@{user.username}" if user.username else user.full_name
    text = (
        f"🔔 *Новый запрос доступа в семейный бот!*\n\n"
        f"Пользователь: {texts.escape_markdown(user.full_name)} "
        f"({texts.escape_markdown(uname)}, ID: `{user.id}`)\n\n"
        f"Выберите роль для нового участника семьи:"
    )
    for owner_id in settings.telegram_allowed_ids:
        try:
            await message.bot.send_message(
                owner_id,
                text,
                parse_mode="Markdown",
                reply_markup=access_request_keyboard(user.id),
            )
        except Exception:
            log.exception("не удалось отправить запрос доступа владельцу %s", owner_id)


async def _forward_time_request_to_parents(
    message: Message, person_id: int, person_name: str, reason: str
) -> None:
    if not message.bot:
        return
    text = (
        f"🔔 *Запрос дополнительного времени!*\n\n"
        f"🎒 Ребёнок: *{texts.escape_markdown(person_name)}*\n"
        f"💬 Причина: {texts.escape_markdown(reason)}\n\n"
        f"Сколько добавить?"
    )
    kb = bonus_request_direct_keyboard(person_id)
    for parent_id in settings.telegram_allowed_ids:
        try:
            await message.bot.send_message(
                parent_id, text, parse_mode="Markdown", reply_markup=kb
            )
        except Exception:
            log.exception("не удалось отправить запрос родителю %s", parent_id)
    await message.answer(
        "✅ Запрос отправлен родителям! Я пришлю ответ, как только они нажмут кнопку.",
        reply_markup=keyboard_for_role("kid"),
    )


def _log_kid_safety_sync(name: str) -> None:
    s = dbmod.session()
    try:
        log_event(s, "kid_safety", f"{name}: обращение требует внимания родителей")
    finally:
        s.close()


async def _alert_parents_about_kid(message: Message, person: Person | None, text: str) -> None:
    name = person.name if person is not None else f"id {message.chat.id}"
    await asyncio.to_thread(_log_kid_safety_sync, name)
    if not message.bot:
        return
    for parent_id in settings.telegram_allowed_ids:
        try:
            await message.bot.send_message(
                parent_id, f"🚨 {name} написал(а) ассистенту, стоит поговорить:\n\n{text[:1000]}"
            )
        except Exception:
            log.exception("не удалось предупредить родителя %s", parent_id)


async def _handle_kid_tutor(message: Message, user_text: str) -> None:
    if not ai_client.is_configured():
        await message.answer("ИИ-помощник сейчас не подключён. Спроси у родителей!")
        return
    role, person = await asyncio.to_thread(_get_user_role_sync, message.chat.id)
    if family_svc.needs_adult_attention(user_text):
        await _alert_parents_about_kid(message, person, user_text)
        await message.answer(family_svc.SAFETY_REPLY)
        return
    try:
        with ai_client.attribute(_caller(person, message.chat.id, "tutor", role)):
            reply = await asyncio.to_thread(_tutor_ask_sync, message.chat.id, user_text)
    except ai_client.AiError as e:
        await message.answer(str(e))
        return
    except Exception:
        log.exception("ошибка репетитора")
        await message.answer("Что-то пошло не так, попробуй задать вопрос ещё раз!")
        return
    CHAT_HISTORY[message.chat.id].append({"role": "user", "content": user_text})
    CHAT_HISTORY[message.chat.id].append({"role": "assistant", "content": reply})
    await send_long(message, reply)


_access = AccessMiddleware(_notify_owner_about_access_request, texts.START_UNAUTHORIZED)
router.message.outer_middleware(_access)
router.callback_query.outer_middleware(_access)


AI_RATE_LIMIT = 8  # requests per window for one person
AI_RATE_WINDOW_SECONDS = 60
_ai_request_times: dict[int, deque] = defaultdict(lambda: deque(maxlen=AI_RATE_LIMIT))


def _allow_ai_request(user_id: int) -> bool:
    """Sliding-window limit that keeps one person from flooding paid model calls."""
    now = time.monotonic()
    times = _ai_request_times[user_id]
    if len(times) == AI_RATE_LIMIT and now - times[0] < AI_RATE_WINDOW_SECONDS:
        return False
    times.append(now)
    return True


def _caller(person: Person | None, telegram_id: int, feature: str, role: str) -> ai_client.Caller:
    user = f"person:{person.id}" if person is not None else f"tg:{telegram_id}"
    return ai_client.Caller(user=user, session=f"tg:{telegram_id}", feature=feature, role=role)


async def send_long(message: Message, text: str) -> None:
    for part in texts.split_message(text):
        await message.answer(part)


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
    user_id = message.from_user.id if message.from_user else 0
    role, _ = await asyncio.to_thread(_get_user_role_sync, user_id)
    kb = keyboard_for_role(role)
    if role == "unauthorized":
        await message.answer(texts.START_UNAUTHORIZED)
        await _notify_owner_about_access_request(message)
        return
    if role == "kid":
        await message.answer(texts.START_KID, reply_markup=kb)
        return
    await message.answer(texts.START, reply_markup=kb)


@router.message(Command("help"))
@router.message(F.text == "❓ Помощь")
async def cmd_help(message: Message) -> None:
    user_id = message.from_user.id if message.from_user else 0
    role, _ = await asyncio.to_thread(_get_user_role_sync, user_id)
    if role == "kid":
        await message.answer(texts.START_KID, reply_markup=keyboard_for_role("kid"))
        return
    await message.answer(texts.HELP, reply_markup=keyboard_for_role(role))


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    status = await asyncio.to_thread(run_tool_sync, "get_status", {})
    await message.answer(texts.format_status(status))


@router.message(Command("devices"))
async def cmd_devices(message: Message) -> None:
    rows = await asyncio.to_thread(run_tool_sync, "list_devices", {})
    await message.answer(texts.format_devices(rows))


@router.message(Command("who"))
@router.message(F.text == "🏠 Кто дома")
async def cmd_who(message: Message) -> None:
    text = await asyncio.to_thread(_who_sync)
    await message.answer(text, parse_mode="Markdown")


@router.message(Command("movie"))
@router.message(F.text == "🍿 ТВ для взрослых")
async def cmd_movie(message: Message) -> None:
    user_id = message.from_user.id if message.from_user else 0
    role, _ = await asyncio.to_thread(_get_user_role_sync, user_id)
    if role == "kid":
        await message.answer("Эта команда доступна только родителям 🍿")
        return
    text = await asyncio.to_thread(_movie_sync)
    await message.answer(text, parse_mode="Markdown")


@router.message(Command("note"))
async def cmd_note(message: Message, command: CommandObject) -> None:
    content = (command.args or "").strip()
    if not content:
        await message.answer(
            "Использование: /note <текст заметки>\n"
            "Например: /note размер ноги Миши 36"
        )
        return
    author = message.from_user.first_name if message.from_user else "Семья"
    res = await asyncio.to_thread(_save_note_sync, author, content)
    await message.answer(res)


@router.message(Command("notes"))
@router.message(F.text == "📝 Заметки")
async def cmd_notes(message: Message) -> None:
    text = await asyncio.to_thread(_get_notes_sync)
    await message.answer(text, parse_mode="Markdown")


@router.message(Command("quiz"))
@router.message(F.text == "🧠 Заработать +15м")
async def cmd_quiz(message: Message) -> None:
    user_id = message.from_user.id if message.from_user else 0
    ok, err_msg, q, person_id = await asyncio.to_thread(_quiz_prepare_sync, user_id)
    if not ok or q is None or person_id is None:
        await message.answer(err_msg)
        return
    topic = q.get("topic", "Эрудиция")
    text = (
        f"🧠 *Вопрос викторины на +15 минут ({topic}):*\n\n"
        f"{texts.escape_markdown(q['question'])}\n\n"
        f"Выберите правильный ответ:"
    )
    kb = quiz_keyboard(person_id, q["options"])
    await message.answer(text, reply_markup=kb, parse_mode="Markdown")


@router.message(Command("explore"))
@router.message(F.text == "🚀 Чем заняться?")
async def cmd_kid_explore(message: Message) -> None:
    await message.answer(
        "🚀 *Выбирай, чем займёмся сегодня:*\n\n"
        "🎲 *Текстовый квест* — интерактивное приключение, где ты выбираешь сюжет!\n"
        "💻 *Идея мини-игры* — задумка для твоей игры на Scratch или Python.\n"
        "🇬🇧 *Английский челлендж* — сленг и фразы из любимых игр.\n"
        "🤯 *Факт дня* — удивительный научный факт, чтобы удивить родителей!",
        reply_markup=kid_explore_keyboard(),
        parse_mode="Markdown",
    )


@router.callback_query(F.data.startswith("kexp:"))
async def cb_kid_explore(cb: CallbackQuery) -> None:
    action = (cb.data or "").split(":")[1]
    prompts = {
        "quest": (
            "Начни для меня увлекательный интерактивный текстовый мини-квест в жанре "
            "космического или фэнтези-приключения! Напиши краткую завязку (3-4 предложения) "
            "и предложи 3 пронумерованных варианта действия (1, 2, 3), "
            "чтобы я выбрал, что делать дальше."
        ),
        "game": (
            "Предложи интересную и простую идею для создания собственной мини-игры "
            "на Scratch или Python. Кратко объясни механику, фишку игры "
            "и первые 3 шага, с чего начать программировать!"
        ),
        "english": (
            "Устрой для меня веселый мини-челлендж по английскому языку на тему игр и приключений: "
            "1 классное слово или фраза, пример в диалоге и короткий вопрос с вариантами для меня!"
        ),
        "fact": (
            "Расскажи один поразительный, неожиданный и научно доказанный факт о космосе, "
            "природе или технологиях, которым я сегодня смогу удивить родителей за ужином!"
        ),
    }
    user_prompt = prompts.get(action, "Расскажи что-нибудь интересное!")
    await cb.answer()
    if isinstance(cb.message, Message):
        title = user_prompt.split("!")[0]
        await cb.message.answer(
            f"🚀 *Запускаю:* _{title}…_", parse_mode="Markdown"
        )
        await _handle_kid_tutor(cb.message, user_prompt)


@router.message(Command("ask"))
@router.message(F.text == "🙏 Попросить время")
async def cmd_ask_time(message: Message, command: CommandObject | None = None) -> None:
    user_id = message.from_user.id if message.from_user else 0
    s = dbmod.session()
    try:
        _, person = family_svc.get_user_role(s, user_id)
    finally:
        s.close()
    if person is None:
        await message.answer("Сначала необходимо привязать ваш аккаунт к ребёнку.")
        return
    reason = (command.args or "").strip() if (command and command.args) else ""
    if not reason:
        await message.answer(
            "Напишите сообщение с причиной (или отправьте голосовое):\n"
            "Например: «Пожалуйста, добавьте 30 минут доделать проект по географии»"
        )
        return
    await _forward_time_request_to_parents(message, person.id, person.name, reason)


@router.message(F.text == "✅ Сделал дело")
async def btn_task_done(message: Message) -> None:
    await message.answer(
        "Молодец! 📸 Пришли фото того, что сделал, и подпиши его: "
        "например, «убрал комнату». Родители посмотрят и начислят минуты."
    )


@router.message(F.photo)
async def handle_task_photo(message: Message) -> None:
    """A child's proof of a finished task goes to parents, who award minutes with one tap."""
    user_id = message.from_user.id if message.from_user else 0
    role, person = await asyncio.to_thread(_get_user_role_sync, user_id)
    if role != "kid" or person is None or message.bot is None or not message.photo:
        await message.answer("Фото принимаются только как отчёт ребёнка о сделанном деле.")
        return
    if not _allow_ai_request(user_id):
        await message.answer("Слишком много сообщений подряд. Подождите минуту 🙂")
        return
    task = (message.caption or "без описания")[:500]
    caption = f"✅ {person.name} сделал(а) дело: {task}\n\nСколько минут начислить?"
    delivered = False
    for parent_id in settings.telegram_allowed_ids:
        try:
            await message.bot.send_photo(
                parent_id,
                photo=message.photo[-1].file_id,
                caption=caption,
                reply_markup=bonus_request_direct_keyboard(person.id),
            )
            delivered = True
        except Exception:
            log.exception("не удалось отправить отчёт родителю %s", parent_id)
    await message.answer(
        "Отправил родителям! Сообщу, когда они решат 🙌"
        if delivered
        else "Не получилось отправить родителям, попробуй ещё раз позже."
    )


@router.message(F.text == "🎓 Помощь с уроками")
async def btn_homework_help(message: Message) -> None:
    await message.answer(
        "Привет! Я готов помочь тебе разобраться с уроками 🎒\n\n"
        "Напиши мне вопрос или условие задачи (или пришли голосовое сообщение).\n"
        "Я объясню сложные правила и подскажу ход решения!"
    )


@router.message(F.text == "⏳ Моё время")
@router.message(F.text == "⏳ Квоты")
async def btn_quota(message: Message) -> None:
    await cmd_quota(message)


@router.message(F.text == "⏸ Пауза")
async def btn_pause(message: Message) -> None:
    user_id = message.from_user.id if message.from_user else 0
    role, _ = await asyncio.to_thread(_get_user_role_sync, user_id)
    if role == "kid":
        await message.answer("Эта функция доступна только родителям 🛡")
        return
    await cmd_pause(message, CommandObject(prefix="/", command="pause", args=""))


@router.message(F.text == "📶 Wi-Fi")
async def btn_wifi(message: Message) -> None:
    await message.answer("📶 Данные домашней Wi-Fi сети для гостей уточните у хозяев дома.")


def _digest_sync() -> str:
    from ..ai import analyst

    s = dbmod.session()
    try:
        return analyst.daily_digest(s)
    finally:
        s.close()


@router.message(Command("digest"))
async def cmd_digest(message: Message) -> None:
    user_id = message.from_user.id if message.from_user else 0
    role, _ = await asyncio.to_thread(_get_user_role_sync, user_id)
    if role == "kid":
        await message.answer("Эта функция доступна только родителям 🛡")
        return
    await send_long(message, await asyncio.to_thread(_digest_sync))


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
    user_id = message.from_user.id if message.from_user else 0
    role, _ = await asyncio.to_thread(_get_user_role_sync, user_id)
    if role == "kid":
        await message.answer("Эта функция доступна только родителям 🛡")
        return
    await _device_command(message, command, "block_device")


@router.message(Command("unblock"))
async def cmd_unblock(message: Message, command: CommandObject) -> None:
    user_id = message.from_user.id if message.from_user else 0
    role, _ = await asyncio.to_thread(_get_user_role_sync, user_id)
    if role == "kid":
        await message.answer("Эта функция доступна только родителям 🛡")
        return
    await _device_command(message, command, "unblock_device")


def pause_presets_keyboard() -> InlineKeyboardMarkup:
    """Быстрые пресеты для /pause без аргументов."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⏸ Дети 30м", callback_data="pz:kid:30"),
            InlineKeyboardButton(text="⏸ Дети 1ч", callback_data="pz:kid:60"),
        ],
        [
            InlineKeyboardButton(text="🍽 Ужин (30м)", callback_data="pz:kid:30:dinner"),
            InlineKeyboardButton(text="🌙 До утра (8ч)", callback_data="pz:kid:480"),
        ],
        [
            InlineKeyboardButton(text="👥 Гости 1ч", callback_data="pz:guest:60"),
            InlineKeyboardButton(text="🔍 Выбрать устройство", callback_data="pz:select"),
        ],
        [
            InlineKeyboardButton(text="✖ Отмена", callback_data="pick:cancel"),
        ],
    ])


@router.callback_query(F.data.startswith("pz:"))
async def cb_pause_preset(cb: CallbackQuery) -> None:
    parts = (cb.data or "").split(":")
    if len(parts) < 2:
        await cb.answer()
        return
    action = parts[1]
    if action == "select":
        await cb.answer()
        candidates = await asyncio.to_thread(_find_candidates, "")
        if isinstance(cb.message, Message):
            await cb.message.edit_text(
                "Выберите устройство для паузы на 30 мин:",
                reply_markup=device_pick_keyboard(candidates, "pause_internet", extra=":30"),
            )
        return
    target, minutes = action, int(parts[2])
    reason = "ужин" if len(parts) > 3 and parts[3] == "dinner" else ""
    args: dict[str, Any] = {"target": target, "minutes": minutes}
    if reason:
        args["reason"] = reason
    try:
        result = await asyncio.to_thread(run_tool_sync, "pause_internet", args)
    except tools.ToolError as e:
        result = f"Не получилось: {e}"
    except Exception:
        log.exception("pause_internet упал")
        result = "Ошибка при выполнении, подробности в журнале."
    await cb.answer()
    if isinstance(cb.message, Message):
        await cb.message.edit_text(f"➡ {result}", reply_markup=None)


@router.message(Command("pause"))
async def cmd_pause(message: Message, command: CommandObject) -> None:
    user_id = message.from_user.id if message.from_user else 0
    role, _ = await asyncio.to_thread(_get_user_role_sync, user_id)
    if role == "kid":
        await message.answer("Эта функция доступна только родителям 🛡")
        return
    raw = (command.args or "").strip()
    if not raw:
        await message.answer(
            "Быстрая пауза интернета. Выберите пресет или укажите время:",
            reply_markup=pause_presets_keyboard(),
        )
        return
    parts = raw.split()
    if len(parts) == 1 and parts[0].isdigit():
        minutes = int(parts[0])
        await _pick_device(message, "", "pause_internet", extra=f":{minutes}")
        return
    if not parts[-1].isdigit():
        await message.answer(
            "Использование: /pause [имя|группа] <минут>\n"
            "Например: /pause kid 60 или /pause 30 — выбор кнопками\n"
            "Либо просто /pause — быстрые кнопки пресетов."
        )
        return
    target, minutes = " ".join(parts[:-1]), int(parts[-1])
    await _target_command(message, "pause_internet", target,
                          {"minutes": minutes}, extra=f":{minutes}")


@router.message(Command("resume"))
async def cmd_resume(message: Message, command: CommandObject) -> None:
    user_id = message.from_user.id if message.from_user else 0
    role, _ = await asyncio.to_thread(_get_user_role_sync, user_id)
    if role == "kid":
        await message.answer("Эта функция доступна только родителям 🛡")
        return
    target = (command.args or "").strip()
    await _target_command(message, "resume_internet", target, {})


@router.message(Command("bonus"))
async def cmd_bonus(message: Message, command: CommandObject) -> None:
    from ..services.quota import QUOTA_CATEGORIES

    user_id = message.from_user.id if message.from_user else 0
    role, _ = await asyncio.to_thread(_get_user_role_sync, user_id)
    if role == "kid":
        await message.answer("Эта функция доступна только родителям 🛡")
        return

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


def _collect_quota_sync() -> list[dict]:
    from ..services import quota as quota_svc

    s = dbmod.session()
    try:
        devices = list(s.scalars(select(Device)))
        people = {p.id: p.name for p in s.scalars(select(Person))}
        prog = quota_svc.progress(s, devices)
        out = []
        for dev in devices:
            rows = prog.get(dev.id, [])
            if not rows:
                continue
            out.append({
                "device": dev.name,
                "device_id": dev.id,
                "owner": people.get(dev.person_id) if dev.person_id else None,
                "rows": rows,
            })
        return out
    finally:
        s.close()


def quota_buttons_keyboard(items: list[dict]) -> InlineKeyboardMarkup | None:
    """Кнопки '+15м' и '+30м' под отчётом квот для каждого детского устройства."""
    rows = []
    for item in items:
        dev_id = item["device_id"]
        dev_name = item["device"]
        short_name = dev_name if len(dev_name) <= 12 else dev_name[:11] + "…"
        rows.append([
            InlineKeyboardButton(
                text=f"➕ 15м {short_name}",
                callback_data=f"pick:add_bonus_time:{dev_id}:15:internet",
            ),
            InlineKeyboardButton(
                text=f"➕ 30м {short_name}",
                callback_data=f"pick:add_bonus_time:{dev_id}:30:internet",
            ),
        ])
    if not rows:
        return None
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("quota"))
async def cmd_quota(message: Message) -> None:
    items = await asyncio.to_thread(_collect_quota_sync)
    kb = quota_buttons_keyboard(items)
    await message.answer(texts.format_quota(items), reply_markup=kb)


def _find_merge_candidates_sync() -> list[tuple[dict, dict, str]]:
    """Находит пары (дубль, оригинал) устройств-кандидатов на объединение.
    Возвращает: [(dup_dict, target_dict, причина)]."""
    s = dbmod.session()
    try:
        devices = list(s.scalars(select(Device).order_by(Device.id)))
        candidates: list[tuple[dict, dict, str]] = []
        seen_pairs: set[tuple[int, int]] = set()

        for d1 in devices:
            for d2 in devices:
                if d1.id >= d2.id:
                    continue
                reason = ""
                h1 = (d1.hostname or "").strip().lower()
                h2 = (d2.hostname or "").strip().lower()
                n1 = (d1.name or "").strip().lower()
                n2 = (d2.name or "").strip().lower()
                if h1 and h2 and h1 == h2:
                    reason = f"совпадает hostname «{d1.hostname}»"
                elif n1 and n2 and n1 == n2:
                    reason = f"совпадает имя «{d1.name}»"
                if reason:
                    # Решаем, кто вероятнее дубль: без владельца или со случайным MAC
                    if d1.person_id is None and d2.person_id is not None:
                        dup, tgt = d1, d2
                    elif d2.person_id is None and d1.person_id is not None:
                        dup, tgt = d2, d1
                    elif d1.mac_is_random and not d2.mac_is_random:
                        dup, tgt = d1, d2
                    elif d2.mac_is_random and not d1.mac_is_random:
                        dup, tgt = d2, d1
                    else:
                        dup, tgt = (d2, d1) if d2.id > d1.id else (d1, d2)
                    pair = (dup.id, tgt.id)
                    if pair not in seen_pairs:
                        seen_pairs.add(pair)
                        candidates.append((
                            {"id": dup.id, "name": dup.name, "mac": dup.mac, "ip": dup.ip},
                            {"id": tgt.id, "name": tgt.name, "mac": tgt.mac, "ip": tgt.ip},
                            reason,
                        ))
        return candidates
    finally:
        s.close()


@router.message(Command("merge"))
async def cmd_merge(message: Message) -> None:
    user_id = message.from_user.id if message.from_user else 0
    role, _ = await asyncio.to_thread(_get_user_role_sync, user_id)
    if role == "kid":
        await message.answer("Эта функция доступна только родителям 🛡")
        return
    candidates = await asyncio.to_thread(_find_merge_candidates_sync)
    if not candidates:
        await message.answer(
            "Подозрительных дублей не найдено: у всех устройств уникальные имена и hostnames."
        )
        return
    await message.answer(f"Найдено подозрительных дублей: {len(candidates)}")
    for dup, tgt, reason in candidates:
        text = (
            f"🔁 Кандидат на объединение ({reason}):\n"
            f"Дубль: #{dup['id']} {dup['name']} ({dup['mac']}, {dup['ip'] or '—'})\n"
            f"Оригинал: #{tgt['id']} {tgt['name']} ({tgt['mac']}, {tgt['ip'] or '—'})"
        )
        await message.answer(text, reply_markup=merge_keyboard(dup["id"], tgt["id"]))


def _collect_usage_sync() -> tuple[dict, int, int | None, str]:
    s = dbmod.session()
    try:
        today = ai_client.usage_today(s)
        left = ai_client.budget_left(s)
        now = datetime.now()
        week_tokens = 0
        for i in range(7):
            d_key = ai_client._usage_key(now - timedelta(days=i))
            raw = kv_get(s, d_key, "")
            if raw:
                try:
                    data = json.loads(raw)
                    week_tokens += data.get("input", 0) + data.get("output", 0)
                except ValueError:
                    pass
        gw = settings.ai_base_url or "Anthropic Cloud"
        if "192.168.88.241" in gw:
            gw = "LunaGPT Gateway (192.168.88.241:4000)"
        return today, week_tokens, left, gw
    finally:
        s.close()


def _spend_by_person_sync() -> tuple[list[tuple[str, float, int, dict]], float]:
    s = dbmod.session()
    try:
        names = {f"person:{p.id}": p.name for p in s.scalars(select(Person))}
        names["system"] = "Система (алерты, дайджест)"
        ledger = ai_client.ledger_for_day(s)
        rows = sorted(
            (
                (names.get(k, "Владелец"), v["usd"], v["requests"], v["features"])
                for k, v in ledger.items()
            ),
            key=lambda row: row[1],
            reverse=True,
        )
        now = datetime.now()
        week_usd = sum(
            entry["usd"]
            for i in range(7)
            for entry in ai_client.ledger_for_day(s, now - timedelta(days=i)).values()
        )
        return rows, week_usd
    finally:
        s.close()


@router.message(Command("usage"))
async def cmd_usage(message: Message) -> None:
    user_id = message.from_user.id if message.from_user else 0
    role, _ = await asyncio.to_thread(_get_user_role_sync, user_id)
    if role == "kid":
        await message.answer("Эта функция доступна только родителям 🛡")
        return
    today, week_tokens, left, gw = await asyncio.to_thread(_collect_usage_sync)
    rows, week_usd = await asyncio.to_thread(_spend_by_person_sync)
    await message.answer(
        texts.format_usage(today, week_tokens, left, gw)
        + "\n\n"
        + texts.format_spend_by_person(rows, week_usd)
    )


# ---------- кнопки под уведомлением о новом устройстве ----------

def new_device_keyboard(dev_id: int, people: list[tuple[int, str, str]]) -> InlineKeyboardMarkup:
    """people: (id, имя, роль). Callback-данные: nd:<dev_id>:<действие>[:<арг>]."""
    rows = [
        [InlineKeyboardButton(text=f"→ {name} ({GROUP_LABELS[role]})",
                              callback_data=f"nd:{dev_id}:assign:{pid}")]
        for pid, name, role in people
    ]
    rows.append([
        InlineKeyboardButton(
            text="📺 Общее (ТВ/Колонка)", callback_data=f"nd:{dev_id}:kind:shared"
        ),
        InlineKeyboardButton(
            text="🤖 Умный дом (IoT)", callback_data=f"nd:{dev_id}:kind:iot"
        ),
    ])
    rows.append([
        InlineKeyboardButton(text="⛔ Блокировать", callback_data=f"nd:{dev_id}:block"),
        InlineKeyboardButton(text="Оставить как есть", callback_data=f"nd:{dev_id}:skip"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def bonus_keyboard(dev_id: int, minutes: int = 30) -> InlineKeyboardMarkup:
    """Кнопка «+30 мин» под уведомлением о блокировке по квоте. Callback
    обрабатывает cb_pick — тот же путь, что и выбор устройства для /bonus."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text=f"➕ {minutes} мин",
            callback_data=f"pick:add_bonus_time:{dev_id}:{minutes}:internet",
        ),
    ]])


# ---------- запрос ребёнка «попросить ещё времени» ----------

# Пресеты одобрения родителем. "day" ≈ до конца дня (add_bonus_time режет по 720).
BONUS_PRESETS = {"15": 15, "30": 30, "day": 720}


def bonus_request_keyboard(request_id: int) -> InlineKeyboardMarkup:
    """Кнопки под заявкой ребёнка: +15 / +30 / до конца дня / Отказать."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="➕ 15 мин", callback_data=f"br:{request_id}:15"),
            InlineKeyboardButton(text="➕ 30 мин", callback_data=f"br:{request_id}:30"),
        ],
        [
            InlineKeyboardButton(text="🌞 До конца дня", callback_data=f"br:{request_id}:day"),
            InlineKeyboardButton(text="🚫 Отказать", callback_data=f"br:{request_id}:no"),
        ],
    ])


def _resolve_bonus_request(request_id: int, choice: str) -> str:
    """Одобряет (add_bonus_time) или отклоняет заявку и помечает её. Одноразово:
    повторное нажатие по обработанной заявке вернёт пометку об этом."""
    from ..models import BonusRequest
    from ..services import bonus
    from ..services.quota import QUOTA_CATEGORY_LABELS

    s = dbmod.session()
    try:
        req = s.get(BonusRequest, request_id)
        if req is None:
            return "Заявка не найдена."
        if req.status != "pending":
            return f"Заявка уже обработана ({req.status})."
        if choice == "no":
            bonus.resolve(s, req, "denied")
            return "Отказано. Ребёнок увидит это на своей странице."
        minutes = BONUS_PRESETS.get(choice, 30)
        cat_label = QUOTA_CATEGORY_LABELS.get(req.category, req.category)
        try:
            tools.run_tool(s, "add_bonus_time",
                           {"target": str(req.device_id), "minutes": minutes,
                            "category": req.category, "comment": "одобрено из Telegram"},
                           source="bot")
        except tools.ToolError as e:
            return f"Не получилось добавить время: {e}"
        bonus.resolve(s, req, "approved", minutes)
        label = "до конца дня" if choice == "day" else f"+{minutes} мин"
        return f"✅ Одобрено ({label}, {cat_label}). Время добавлено."
    finally:
        s.close()


@router.callback_query(F.data.startswith("br:"))
async def cb_bonus_request(cb: CallbackQuery) -> None:
    parts = (cb.data or "").split(":")
    request_id, choice = int(parts[1]), parts[2]
    try:
        result = await asyncio.to_thread(_resolve_bonus_request, request_id, choice)
    except Exception:
        log.exception("обработка заявки на бонус упала")
        result = "Ошибка при обработке заявки, подробности в журнале."
    await cb.answer()
    if isinstance(cb.message, Message):
        await cb.message.edit_text(f"{cb.message.text}\n\n➡ {result}", reply_markup=None)


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
        elif action == "kind":
            kind_val = parts[3]
            result = await asyncio.to_thread(
                run_tool_sync, "set_device_kind", {"device_id": dev_id, "kind": kind_val}
            )
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
        if isinstance(cb.message, Message):
            await cb.message.edit_text(f"{cb.message.text}\n\n⚠️ {e}", reply_markup=None)
        return
    await cb.answer("Готово")
    if isinstance(cb.message, Message):  # у устаревших сообщений править нечего
        await cb.message.edit_text(f"{cb.message.text}\n\n➡ {result}", reply_markup=None)


@router.callback_query(F.data.startswith("acc:"))
async def cb_access_request(cb: CallbackQuery) -> None:
    parts = (cb.data or "").split(":")
    requester_id, role = int(parts[1]), parts[2]
    requester_name = f"Пользователь {requester_id}"
    if cb.message and isinstance(cb.message, Message) and cb.message.text:
        for line in cb.message.text.split("\n"):
            if line.startswith("Пользователь:"):
                requester_name = line.replace("Пользователь:", "").split("(")[0].strip()
                break

    msg = await asyncio.to_thread(
        _assign_telegram_access_sync, requester_id, role, requester_name
    )
    await cb.answer()
    if isinstance(cb.message, Message):
        await cb.message.edit_text(f"{cb.message.text}\n\n➡ {msg}", reply_markup=None)

    if role != "deny" and cb.bot:
        start_text = texts.START_KID if role == "kid" else texts.START
        role_label = family_svc.ROLE_LABELS.get(role, role)
        try:
            await cb.bot.send_message(
                requester_id,
                f"🎉 Доступ подтверждён! Ваша роль: {role_label}.\n\n{start_text}",
                reply_markup=keyboard_for_role(role),
            )
        except Exception:
            log.exception(
                "не удалось уведомить пользователя %s о предоставлении доступа", requester_id
            )


@router.callback_query(F.data.startswith("bdir:"))
async def cb_bonus_direct(cb: CallbackQuery) -> None:
    parts = (cb.data or "").split(":")
    person_id, choice = int(parts[1]), parts[2]
    msg, minutes, _person_name = await asyncio.to_thread(
        _resolve_bonus_direct_sync, person_id, choice
    )
    await cb.answer()
    if isinstance(cb.message, Message):
        if cb.message.photo:
            await cb.message.edit_caption(
                caption=f"{cb.message.caption}\n\n➡ {msg}", reply_markup=None
            )
        else:
            await cb.message.edit_text(f"{cb.message.text}\n\n➡ {msg}", reply_markup=None)

    s = dbmod.session()
    try:
        p = s.get(Person, person_id)
        kid_tg = p.telegram_id if p else None
    finally:
        s.close()

    if kid_tg and cb.bot:
        if choice == "deny":
            kid_msg = (
                "Родители пока не могут добавить время. "
                "Попробуйте позаниматься уроками или викториной! 🎒"
            )
        else:
            label = "до конца дня" if choice == "day" else f"+{minutes} минут"
            kid_msg = f"🎉 Родители одобрили добавление времени: {label}! Приятного пользования."
        try:
            await cb.bot.send_message(kid_tg, kid_msg)
        except Exception:
            log.exception("не удалось уведомить ребёнка %s", kid_tg)


@router.callback_query(F.data.startswith("qz:"))
async def cb_quiz(cb: CallbackQuery) -> None:
    parts = (cb.data or "").split(":")
    person_id, opt_idx = int(parts[1]), int(parts[2])
    user_id = cb.from_user.id if cb.from_user else 0
    res = await asyncio.to_thread(_quiz_answer_sync, user_id, person_id, opt_idx)
    await cb.answer()
    if isinstance(cb.message, Message):
        await cb.message.edit_text(
            f"{texts.escape_markdown(cb.message.text or '')}\n\n➡ {res['message']}",
            reply_markup=None,
            parse_mode="Markdown",
        )

    # При верном ответе — шлём гордое уведомление родителям с темой вопроса
    if res.get("status") == "correct" and cb.bot:
        parent_text = (
            f"🏆 *{res['person_name']} выиграл в викторине!*\n\n"
            f"Тема: *{res['topic']}*\n"
            f"Вопрос: _{res['question']}_\n\n"
            f"Ребёнок дал верный ответ и получил бонус *+15 минут* интернета."
        )
        for parent_id in settings.telegram_allowed_ids:
            try:
                await cb.bot.send_message(parent_id, parent_text, parse_mode="Markdown")
            except Exception:
                log.exception(
                    "не удалось отправить родителю %s уведомление о победе в викторине",
                    parent_id,
                )


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


async def _process_user_nl_prompt(message: Message, user_text: str) -> None:
    """Обрабатывает текстовый ввод (набранный или распознанный из аудио) через ИИ."""
    user_id = message.from_user.id if message.from_user else 0
    role, person = await asyncio.to_thread(_get_user_role_sync, user_id)
    if role == "unauthorized":
        await message.answer(texts.START_UNAUTHORIZED)
        await _notify_owner_about_access_request(message)
        return

    if role == "guest":
        await message.answer("Гостям доступны только Wi-Fi и справка.")
        return

    if not _allow_ai_request(user_id):
        await message.answer("Слишком много сообщений подряд. Подождите минуту 🙂")
        return

    if role == "kid":
        lower = user_text.lower()
        time_keywords = ("время", "времени", "добавь", "разблокируй", "интернет", "продли", "минут")
        is_asking_time = any(k in lower for k in time_keywords) and (
            "дай" in lower or "добавь" in lower or "пожалуйста" in lower
            or "прошу" in lower or "еще" in lower or "ещё" in lower
        )
        if is_asking_time:
            if person:
                await _forward_time_request_to_parents(
                    message, person.id, person.name, user_text
                )
                return
        await _handle_kid_tutor(message, user_text)
        return

    if not ai_client.is_configured():
        await message.answer(
            "Свободный текст понимает ИИ, а он не настроен "
            "(HS_ANTHROPIC_API_KEY в .env). Пока доступны команды — /help."
        )
        return
    try:
        with ai_client.attribute(_caller(person, user_id, "orchestrator", role)):
            answer = await asyncio.to_thread(_orchestrate, message.chat.id, user_text)
    except ai_client.AiError as e:
        await message.answer(str(e))
        return
    except Exception:
        log.exception("оркестратор упал")
        await message.answer("ИИ споткнулся, подробности в журнале бота. Команды /help работают.")
        return

    CHAT_HISTORY[message.chat.id].append({"role": "user", "content": user_text})
    CHAT_HISTORY[message.chat.id].append({"role": "assistant", "content": answer.text})
    await send_long(message, answer.text)
    for action in answer.pending:
        pending_id = await asyncio.to_thread(_save_pending, action)
        await message.answer(
            f"Подтвердите действие:\n{action.description}",
            reply_markup=confirm_keyboard(pending_id),
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
    if not settings.stt_url:
        await message.answer(
            "Голосовое управление не настроено: укажите HS_STT_URL в .env (шлюз LunaGPT)."
        )
        return

    if not message.voice or message.bot is None:
        return
    if message.voice.duration > settings.ai_max_voice_seconds:
        await message.answer(
            f"Слишком длинное голосовое, максимум {settings.ai_max_voice_seconds} секунд."
        )
        return

    user_id = message.from_user.id if message.from_user else 0
    role, person = await asyncio.to_thread(_get_user_role_sync, user_id)
    caller = _caller(person, user_id, "stt", role)

    try:
        await message.bot.send_chat_action(message.chat.id, "typing")
        file_info = await message.bot.get_file(message.voice.file_id)
        if not file_info.file_path:
            await message.answer("Не удалось получить файл голосового сообщения из Telegram.")
            return

        voice_buf = await message.bot.download_file(file_info.file_path)
        if voice_buf is None:
            await message.answer("Не удалось скачать аудиозапись.")
            return
        voice_bytes = voice_buf.getvalue() if hasattr(voice_buf, "getvalue") else voice_buf.read()

        async with httpx.AsyncClient(timeout=35.0) as client:
            files = {
                "file": ("voice.oga", voice_bytes, message.voice.mime_type or "audio/ogg")
            }
            data = {"language": "ru"}
            headers = ai_client.attribution_headers(caller)
            if settings.anthropic_api_key:
                headers["Authorization"] = f"Bearer {settings.anthropic_api_key}"
            resp = await client.post(settings.stt_url, files=files, data=data, headers=headers)
            resp.raise_for_status()
            transcribed_text = resp.json().get("text", "").strip()
    except Exception as exc:
        log.exception("ошибка транскрипции голосового сообщения")
        await message.answer(f"Не удалось распознать голос: {exc}")
        return

    await asyncio.to_thread(_record_voice_spend_sync, caller, message.voice.duration)

    if not transcribed_text:
        await message.answer("Не удалось разобрать речь. Попробуйте сказать четче.")
        return

    await message.answer(f"🗣 <i>«{transcribed_text}»</i>", parse_mode="HTML")
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
            result = await asyncio.to_thread(run_tool_sync, tool_name, tool_args, "ai")
        except tools.ToolError as e:
            result = f"Не получилось: {e}"
        except Exception:
            log.exception("подтверждённое действие %s упало", tool_name)
            result = "Ошибка при выполнении, подробности в журнале."
    await cb.answer()
    if isinstance(cb.message, Message):
        await cb.message.edit_text(f"{cb.message.text}\n\n➡ {result}", reply_markup=None)
