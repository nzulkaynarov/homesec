"""Команды и кнопки бота. Хендлеры тонкие: вся логика — в app.ai.tools (общий
реестр инструментов с панелью и ИИ), здесь только парсинг и форматирование.
Явная команда человека (/block и т.п.) — это уже подтверждение, поэтому
мутации исполняются сразу; подтверждение кнопкой нужно только действиям ИИ."""

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import select

from .. import db as dbmod
from ..ai import client as ai_client
from ..ai import orchestrator, tools
from ..config import settings
from ..models import GROUP_LABELS, GROUPS, Device, Person, kv_get
from ..services import family as family_svc
from ..services import house_rules
from . import common, device_menu, kid_menu, pause_menu, rules_menu, texts, triage
from .access import AccessMiddleware

log = logging.getLogger("homesec.bot")
router = Router()

# Мутации, предложенные ИИ и ждущие кнопку, лежат в таблице pending_actions
# (tools.save_pending/pop_pending) — кнопки переживают рестарты и деплой.



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


def access_request_keyboard(
    requester_id: int, unlinked: list[tuple[int, str]] | None = None
) -> InlineKeyboardMarkup:
    """Кнопки родителю при онбординге нового участника семьи.

    People already in the list but without Telegram come first: picking one links
    the account to that person, so their devices and rules apply to them."""
    linked = [
        [InlineKeyboardButton(text=f"Это {name}", callback_data=f"acc:{requester_id}:link:{pid}")]
        for pid, name in (unlinked or [])
    ]
    return InlineKeyboardMarkup(inline_keyboard=linked + [
        [
            InlineKeyboardButton(text="🛡 Родитель", callback_data=f"acc:{requester_id}:admin"),
            InlineKeyboardButton(text="🎒 Ребёнок", callback_data=f"acc:{requester_id}:kid"),
        ],
        [
            InlineKeyboardButton(text="🏡 Гость", callback_data=f"acc:{requester_id}:guest"),
            InlineKeyboardButton(text="⛔ Отклонить", callback_data=f"acc:{requester_id}:deny"),
        ],
    ])


# ---------- синхронные вспомогательные функции ----------


def _unlinked_people_sync() -> list[tuple[int, str]]:
    s = dbmod.session()
    try:
        query = select(Person).where(Person.telegram_id.is_(None)).order_by(Person.name)
        return [(p.id, p.name) for p in s.scalars(query)]
    finally:
        s.close()


def _link_telegram_sync(requester_id: int, person_id: int) -> tuple[str, str]:
    """Attach a Telegram account to a person already in the list; returns (message, role)."""
    s = dbmod.session()
    try:
        person = s.get(Person, person_id)
        if person is None:
            return "Этого человека уже нет в списке.", "deny"
        if s.scalar(select(Person).where(Person.telegram_id == requester_id)) is not None:
            return "Этот Telegram уже привязан к другому человеку.", "deny"
        role = "kid" if person.role == "kid" else "admin"
        person.telegram_id, person.bot_role = requester_id, role
        s.commit()
        label = family_svc.ROLE_LABELS.get(role, role)
        return f"Telegram привязан к «{person.name}», роль: {label}.", role
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
    unlinked = await asyncio.to_thread(_unlinked_people_sync)
    for owner_id in settings.telegram_allowed_ids:
        try:
            await message.bot.send_message(
                owner_id,
                text,
                parse_mode="Markdown",
                reply_markup=access_request_keyboard(user.id, unlinked),
            )
        except Exception:
            log.exception("не удалось отправить запрос доступа владельцу %s", owner_id)


_access = AccessMiddleware(_notify_owner_about_access_request, texts.START_UNAUTHORIZED)
router.message.outer_middleware(_access)
router.callback_query.outer_middleware(_access)
triage.register(router)
device_menu.register(router)
pause_menu.register(router)
rules_menu.register(router)
kid_menu.register(router)


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
    role, _ = await asyncio.to_thread(common.get_user_role_sync, user_id)
    kb = common.keyboard_for_role(role)
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
    role, _ = await asyncio.to_thread(common.get_user_role_sync, user_id)
    if role == "kid":
        await message.answer(texts.START_KID, reply_markup=common.keyboard_for_role("kid"))
        return
    await message.answer(texts.HELP, reply_markup=common.keyboard_for_role(role))


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    status = await asyncio.to_thread(run_tool_sync, "get_status", {})
    await message.answer(texts.format_status(status))


@router.message(Command("devices"))
async def cmd_devices(message: Message) -> None:
    await device_menu.btn_devices(message)


@router.message(Command("who"))
@router.message(F.text == "🏠 Кто дома")
async def cmd_who(message: Message) -> None:
    text = await asyncio.to_thread(_who_sync)
    await message.answer(text, parse_mode="Markdown")


SHARED_STATE_LABELS = {"kid": "👶 Детский режим", "off": "⛔ Выключено", "adult": "🍿 Для взрослых"}


def _shared_list_sync() -> list[tuple[int, str, str, str]]:
    s = dbmod.session()
    try:
        devices = s.scalars(select(Device).where(Device.kind == "shared").order_by(Device.name))
        rows = []
        for d in devices:
            state = family_svc.shared_device_state(d)
            until = f"{d.adult_until:%H:%M}" if state == "adult" and d.adult_until else ""
            rows.append((d.id, d.name, state, until))
        return rows
    finally:
        s.close()


def _shared_list_markup(rows: list[tuple[int, str, str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"{SHARED_STATE_LABELS[state].split()[0]} {name}"[:60],
            callback_data=f"sh:{dev_id}",
        )]
        for dev_id, name, state, _ in rows
    ])


def _shared_actions_markup(dev_id: int) -> InlineKeyboardMarkup:
    action = f"shs:{dev_id}"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⛔ Выключить", callback_data=f"{action}:off"),
         InlineKeyboardButton(text="👶 Детский режим", callback_data=f"{action}:kid")],
        [InlineKeyboardButton(text="🍿 Взрослым на 3 часа", callback_data=f"{action}:3"),
         InlineKeyboardButton(text="🌙 Взрослым до утра", callback_data=f"{action}:m")],
        [InlineKeyboardButton(text="⏸ Отключить на время", callback_data=f"pzt:d{dev_id}")],
        [InlineKeyboardButton(text="← Все устройства", callback_data="shl")],
    ])


def _shared_status_text(rows: list[tuple[int, str, str, str]], dev_id: int) -> str:
    for rid, name, state, until in rows:
        if rid == dev_id:
            suffix = f" до {until}" if until else ""
            return f"{name}\nСейчас: {SHARED_STATE_LABELS[state]}{suffix}"
    return "Устройство не найдено."


def _set_shared_sync(dev_id: int, choice: str) -> None:
    s = dbmod.session()
    try:
        dev = s.get(Device, dev_id)
        if dev is None:
            return
        if choice in ("off", "kid"):
            family_svc.set_shared_state(s, dev, choice)
        else:
            hours = family_svc.hours_until_morning() if choice == "m" else float(choice)
            family_svc.set_shared_state(s, dev, "adult", hours)
    finally:
        s.close()


async def _show_shared(message: Message, *, edit: bool = False) -> None:
    rows = await asyncio.to_thread(_shared_list_sync)
    empty = "Общих устройств пока нет. Отметьте ТВ или колонку как «Общее»."
    text = "Выберите устройство:" if rows else empty
    markup = _shared_list_markup(rows) if rows else None
    if edit:
        await message.edit_text(text, reply_markup=markup)
    else:
        await message.answer(text, reply_markup=markup)


@router.message(F.text == "📺 Общие устройства")
async def btn_shared_devices(message: Message) -> None:
    await _show_shared(message)


@router.callback_query(F.data == "shl")
async def cb_shared_list(cb: CallbackQuery) -> None:
    await cb.answer()
    if isinstance(cb.message, Message):
        await _show_shared(cb.message, edit=True)


@router.callback_query(F.data.startswith("sh:"))
async def cb_shared_device(cb: CallbackQuery) -> None:
    dev_id = int((cb.data or "").split(":")[1])
    rows = await asyncio.to_thread(_shared_list_sync)
    await cb.answer()
    if isinstance(cb.message, Message):
        await cb.message.edit_text(
            _shared_status_text(rows, dev_id), reply_markup=_shared_actions_markup(dev_id)
        )


@router.callback_query(F.data.startswith("shs:"))
async def cb_shared_state(cb: CallbackQuery) -> None:
    parts = (cb.data or "").split(":")
    dev_id, choice = int(parts[1]), parts[2]
    await asyncio.to_thread(_set_shared_sync, dev_id, choice)
    rows = await asyncio.to_thread(_shared_list_sync)
    await cb.answer("Готово")
    if isinstance(cb.message, Message):
        await cb.message.edit_text(
            _shared_status_text(rows, dev_id), reply_markup=_shared_actions_markup(dev_id)
        )


@router.message(Command("movie"))
async def cmd_movie(message: Message) -> None:
    user_id = message.from_user.id if message.from_user else 0
    role, _ = await asyncio.to_thread(common.get_user_role_sync, user_id)
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


@router.message(F.text == "⏳ Моё время")
@router.message(F.text == "⏳ Квоты")
async def btn_quota(message: Message) -> None:
    await cmd_quota(message)


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
    role, _ = await asyncio.to_thread(common.get_user_role_sync, user_id)
    if role == "kid":
        await message.answer("Эта функция доступна только родителям 🛡")
        return
    await common.send_long(message, await asyncio.to_thread(_digest_sync))


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
    role, _ = await asyncio.to_thread(common.get_user_role_sync, user_id)
    if role == "kid":
        await message.answer("Эта функция доступна только родителям 🛡")
        return
    await _device_command(message, command, "block_device")


@router.message(Command("unblock"))
async def cmd_unblock(message: Message, command: CommandObject) -> None:
    user_id = message.from_user.id if message.from_user else 0
    role, _ = await asyncio.to_thread(common.get_user_role_sync, user_id)
    if role == "kid":
        await message.answer("Эта функция доступна только родителям 🛡")
        return
    await _device_command(message, command, "unblock_device")


def _resume_all_sync() -> int:
    s = dbmod.session()
    try:
        return house_rules.resume_all(s)
    finally:
        s.close()


@router.message(Command("pause"))
async def cmd_pause(message: Message, command: CommandObject) -> None:
    user_id = message.from_user.id if message.from_user else 0
    role, _ = await asyncio.to_thread(common.get_user_role_sync, user_id)
    if role == "kid":
        await message.answer("Эта функция доступна только родителям 🛡")
        return
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
    role, _ = await asyncio.to_thread(common.get_user_role_sync, user_id)
    if role == "kid":
        await message.answer("Эта функция доступна только родителям 🛡")
        return
    target = (command.args or "").strip()
    await _target_command(message, "resume_internet", target, {})


@router.message(Command("bonus"))
async def cmd_bonus(message: Message, command: CommandObject) -> None:
    from ..services.quota import QUOTA_CATEGORIES

    user_id = message.from_user.id if message.from_user else 0
    role, _ = await asyncio.to_thread(common.get_user_role_sync, user_id)
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
    role, _ = await asyncio.to_thread(common.get_user_role_sync, user_id)
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


def bonus_request_keyboard(request_id: int, category: str = "internet") -> InlineKeyboardMarkup:
    """Кнопки под заявкой ребёнка: +15 / +30 / до конца дня / Отказать.
    Для просьбы во время паузы третья кнопка открывает интернет до её конца."""
    from ..services.bonus import PAUSE_CATEGORY

    if category == PAUSE_CATEGORY:
        whole = InlineKeyboardButton(text="▶️ До конца паузы", callback_data=f"br:{request_id}:day")
    else:
        whole = InlineKeyboardButton(text="🌞 До конца дня", callback_data=f"br:{request_id}:day")
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="➕ 15 мин", callback_data=f"br:{request_id}:15"),
            InlineKeyboardButton(text="➕ 30 мин", callback_data=f"br:{request_id}:30"),
        ],
        [
            whole,
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
        if req.category == bonus.PAUSE_CATEGORY:
            return _open_through_pause(s, req, choice)
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


def _open_through_pause(s, req, choice: str) -> str:
    """Approve a request sent during a pause: open the child's internet for a
    while; the pause itself stays and takes over again afterwards."""
    from ..services import bonus

    dev = s.get(Device, req.device_id)
    if dev is None or dev.person_id is None:
        return "Устройство без владельца, открыть интернет некому."
    minutes = None if choice == "day" else BONUS_PRESETS.get(choice, 30)
    try:
        until = house_rules.open_through_pause(s, dev.person_id, minutes)
    except house_rules.RuleError as e:
        bonus.resolve(s, req, "approved", 0)
        return str(e)
    opened = max(1, round((until - datetime.now()).total_seconds() / 60))
    bonus.resolve(s, req, "approved", opened)
    return f"✅ Интернет открыт до {until:%H:%M}, потом пауза продолжится."


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

    if role == "link" and len(parts) > 3:
        msg, role = await asyncio.to_thread(_link_telegram_sync, requester_id, int(parts[3]))
    else:
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
                reply_markup=common.keyboard_for_role(role),
            )
        except Exception:
            log.exception(
                "не удалось уведомить пользователя %s о предоставлении доступа", requester_id
            )


# ---------- свободный текст -> ИИ-оркестратор ----------

def _orchestrate(chat_id: int, text: str) -> orchestrator.Answer:
    s = dbmod.session()
    try:
        return orchestrator.handle(s, text, history=list(common.CHAT_HISTORY[chat_id]))
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
    role, person = await asyncio.to_thread(common.get_user_role_sync, user_id)
    if role == "unauthorized":
        await message.answer(texts.START_UNAUTHORIZED)
        await _notify_owner_about_access_request(message)
        return

    if role == "guest":
        await message.answer("Гостям доступны только Wi-Fi и справка.")
        return

    if not common.allow_ai_request(user_id):
        await message.answer("Слишком много сообщений подряд. Подождите минуту 🙂")
        return

    if role == "kid":
        await kid_menu.handle_text(message, person, user_text)
        return

    if not ai_client.is_configured():
        await message.answer(
            "Свободный текст понимает ИИ, а он не настроен "
            "(HS_ANTHROPIC_API_KEY в .env). Пока доступны команды — /help."
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
        await message.answer("ИИ споткнулся, подробности в журнале бота. Команды /help работают.")
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
        await message.answer(
            "Голосовое управление не настроено: нужен шлюз LunaGPT (HS_AI_BASE_URL)."
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
    role, person = await asyncio.to_thread(common.get_user_role_sync, user_id)
    caller = common.caller(person, user_id, "stt", role)

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

        transcribed_text = await ai_client.transcribe(voice_bytes, "ogg", caller)
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
