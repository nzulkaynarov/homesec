"""The child's side of the HomeSec bot: the AI tutor, the daily quiz for +15 minutes,
«Чем заняться?», asking parents for time and the photo report of a finished task, with the
parents' answers.

Kept apart so it can leave HomeSec in one piece once every child talks to Luna, which has its
own tutor and quiz; HomeSec itself is about the network."""

import asyncio
import logging
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .. import db as dbmod
from ..ai import client as ai_client
from ..config import settings
from ..models import Person, log_event
from ..services import family as family_svc
from ..services import time_grants
from . import common, texts

log = logging.getLogger("homesec.bot.kid")

TIME_WORDS = ("время", "времени", "добавь", "разблокируй", "интернет", "продли", "минут")
ASKING_WORDS = ("дай", "добавь", "пожалуйста", "прошу", "еще", "ещё")


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
        with ai_client.attribute(common.caller(person, user_id, "quiz", role)):
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


def _resolve_bonus_direct_sync(person_id: int, choice: str) -> tuple[str, str | None, str]:
    """(text for the parent, text for the child or None, child's name). One answer for every
    "yes": time_grants acts on what really holds the child back."""
    s = dbmod.session()
    try:
        person = s.get(Person, person_id)
        if person is None:
            return "Пользователь не найден.", None, ""
        if choice == "deny":
            kid = (
                "Родители пока не могут добавить время. "
                "Можно позаниматься или сыграть в викторину! 🎒"
            )
            return f"Отказано в добавлении времени для {person.name}.", kid, person.name
        minutes = None if choice == "day" else int(choice)
        try:
            done = time_grants.grant(s, person_id, minutes, "Одобрено родителем в Telegram")
        except time_grants.GrantError as exc:
            return f"⚠️ {exc}", None, person.name
        return f"✅ {person.name}: {done}.", f"🎉 Родители ответили: {done}.", person.name
    finally:
        s.close()


def _tutor_ask_sync(chat_id: int, user_text: str) -> str:
    s = dbmod.session()
    try:
        messages = list(common.CHAT_HISTORY[chat_id]) + [{"role": "user", "content": user_text}]
        resp = ai_client.ask_with_fallback(
            s,
            ai_client.model_ladder(settings.ai_model_tutor, settings.ai_model_support),
            messages=messages,
            system=(
                "Ты — умный, добрый и вдохновляющий семейный ИИ-помощник LunaGPT для детей. "
                "Ты общаешься с ребёнком дружелюбно, с поддержкой и юмором на русском языке.\n\n"
                "ТВОИ ПРАВИЛА:\n"
                "1. ШКОЛА И УРОКИ: Помогай полноценно и объясняй простыми словами.\n"
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


def _can_take_quiz_sync(person_id: int) -> bool:
    s = dbmod.session()
    try:
        return family_svc.can_take_quiz_today(s, person_id)
    finally:
        s.close()


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
    for parent_id in await asyncio.to_thread(common.parent_ids_sync):
        try:
            await message.bot.send_message(
                parent_id, text, parse_mode="Markdown", reply_markup=kb
            )
        except Exception:
            log.exception("не удалось отправить запрос родителю %s", parent_id)
    text = "✅ Запрос отправлен родителям! Я пришлю ответ, как только они нажмут кнопку."
    if await asyncio.to_thread(_can_take_quiz_sync, person_id):
        text += "\n\nПока ждёшь, можно заработать +15 минут в викторине: /quiz"
    await message.answer(text, reply_markup=common.keyboard_for_role("kid"))


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
    for parent_id in await asyncio.to_thread(common.parent_ids_sync):
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
    role, person = await asyncio.to_thread(common.get_user_role_sync, message.chat.id)
    if family_svc.needs_adult_attention(user_text):
        await _alert_parents_about_kid(message, person, user_text)
        await message.answer(family_svc.SAFETY_REPLY)
        return
    try:
        with ai_client.attribute(common.caller(person, message.chat.id, "tutor", role)):
            reply = await asyncio.to_thread(_tutor_ask_sync, message.chat.id, user_text)
    except ai_client.AiError as e:
        await message.answer(str(e))
        return
    except Exception:
        log.exception("ошибка репетитора")
        await message.answer("Что-то пошло не так, попробуй задать вопрос ещё раз!")
        return
    common.CHAT_HISTORY[message.chat.id].append({"role": "user", "content": user_text})
    common.CHAT_HISTORY[message.chat.id].append({"role": "assistant", "content": reply})
    await common.send_long(message, reply)


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


async def btn_task_done(message: Message) -> None:
    await message.answer(
        "Молодец! 📸 Пришли фото того, что сделал, и подпиши его: "
        "например, «убрал комнату». Родители посмотрят и начислят минуты."
    )


async def handle_task_photo(message: Message) -> None:
    """A child's proof of a finished task goes to parents, who award minutes with one tap."""
    user_id = message.from_user.id if message.from_user else 0
    role, person = await asyncio.to_thread(common.get_user_role_sync, user_id)
    if role != "kid" or person is None or message.bot is None or not message.photo:
        await message.answer("Фото принимаются только как отчёт ребёнка о сделанном деле.")
        return
    if not common.allow_ai_request(user_id):
        await message.answer("Слишком много сообщений подряд. Подождите минуту 🙂")
        return
    task = (message.caption or "без описания")[:500]
    caption = f"✅ {person.name} сделал(а) дело: {task}\n\nСколько минут начислить?"
    delivered = False
    for parent_id in await asyncio.to_thread(common.parent_ids_sync):
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


async def btn_homework_help(message: Message) -> None:
    await message.answer(
        "Привет! Я готов помочь тебе разобраться с уроками 🎒\n\n"
        "Напиши мне вопрос или условие задачи (или пришли голосовое сообщение).\n"
        "Я объясню сложные правила и подскажу ход решения!"
    )


async def cb_bonus_direct(cb: CallbackQuery) -> None:
    parts = (cb.data or "").split(":")
    person_id, choice = int(parts[1]), parts[2]
    msg, kid_msg, _person_name = await asyncio.to_thread(
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

    if kid_tg and cb.bot and kid_msg:
        try:
            await cb.bot.send_message(kid_tg, kid_msg)
        except Exception:
            log.exception("не удалось уведомить ребёнка %s", kid_tg)


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
        for parent_id in await asyncio.to_thread(common.parent_ids_sync):
            try:
                await cb.bot.send_message(parent_id, parent_text, parse_mode="Markdown")
            except Exception:
                log.exception(
                    "не удалось отправить родителю %s уведомление о победе в викторине",
                    parent_id,
                )



async def handle_text(message: Message, person: Person | None, user_text: str) -> None:
    """A child's free text: a plea for more time goes to the parents, anything else to the tutor."""
    lower = user_text.lower()
    is_asking_time = any(k in lower for k in TIME_WORDS) and any(k in lower for k in ASKING_WORDS)
    if is_asking_time and person:
        await _forward_time_request_to_parents(message, person.id, person.name, user_text)
        return
    await _handle_kid_tutor(message, user_text)


def register(router: Router) -> None:
    """Attach to the router that carries the access middleware, before catch-all text handlers."""
    router.message.register(cmd_quiz, Command("quiz"))
    router.message.register(cmd_quiz, F.text == "🧠 Заработать +15м")
    router.message.register(cmd_kid_explore, Command("explore"))
    router.message.register(cmd_kid_explore, F.text == "🚀 Чем заняться?")
    router.callback_query.register(cb_kid_explore, F.data.startswith("kexp:"))
    router.message.register(cmd_ask_time, Command("ask"))
    router.message.register(cmd_ask_time, F.text == "🙏 Попросить время")
    router.message.register(btn_task_done, F.text == "✅ Сделал дело")
    router.message.register(handle_task_photo, F.photo)
    router.message.register(btn_homework_help, F.text == "🎓 Помощь с уроками")
    router.callback_query.register(cb_bonus_direct, F.data.startswith("bdir:"))
    router.callback_query.register(cb_quiz, F.data.startswith("qz:"))
