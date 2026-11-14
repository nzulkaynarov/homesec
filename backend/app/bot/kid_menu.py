"""The child's side of the HomeSec bot: asking parents for time and the photo report of a
finished task, with the parents' answers. HomeSec is about the network; the tutor, the quiz
and talk live in Luna, which awards the quiz minutes through the Luna API."""

import asyncio
import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .. import db as dbmod
from ..models import Person, log_event
from ..services import family as family_svc
from ..services import time_grants
from . import common, texts

log = logging.getLogger("homesec.bot.kid")

TIME_WORDS = ("время", "времени", "добавь", "разблокируй", "интернет", "продли", "минут")
ASKING_WORDS = ("дай", "добавь", "пожалуйста", "прошу", "еще", "ещё")
ELSEWHERE = (
    "Здесь я помогаю только со временем в интернете: ⏳ Моё время, 🙏 Попросить время "
    "и ✅ Сделал дело. Поболтать, разобраться с уроками или сыграть в викторину "
    "можно у Luna 🐾"
)


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
                "Можно позаниматься или сыграть в викторину у Luna! 🎒"
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
        text += "\n\nПока ждёшь, можно заработать +15 минут в викторине у Luna 🧠"
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


async def handle_text(message: Message, person: Person | None, user_text: str) -> None:
    """A child's free text: a plea for more time goes to the parents, a worrying message
    alerts them, anything else points to Luna."""
    lower = user_text.lower()
    is_asking_time = any(k in lower for k in TIME_WORDS) and any(k in lower for k in ASKING_WORDS)
    if is_asking_time and person:
        await _forward_time_request_to_parents(message, person.id, person.name, user_text)
        return
    if family_svc.needs_adult_attention(user_text):
        await _alert_parents_about_kid(message, person, user_text)
        await message.answer(family_svc.SAFETY_REPLY)
        return
    await message.answer(ELSEWHERE, reply_markup=common.keyboard_for_role("kid"))


def register(router: Router) -> None:
    """Attach to the router that carries the access middleware, before catch-all text handlers."""
    router.message.register(cmd_ask_time, Command("ask"))
    router.message.register(cmd_ask_time, F.text == "🙏 Попросить время")
    router.message.register(btn_task_done, F.text == "✅ Сделал дело")
    router.message.register(handle_task_photo, F.photo)
    router.callback_query.register(cb_bonus_direct, F.data.startswith("bdir:"))
