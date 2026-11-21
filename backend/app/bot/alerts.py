"""Buttons under the notices parents get: a new device to sort, a quota block, a child's request
for more time and a device that looks like a known one with a new MAC."""

import asyncio
import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .. import db as dbmod
from ..ai import tools
from ..models import GROUP_LABELS, Device, Person
from ..services import time_grants
from . import common
from .texts import DANGER, SUCCESS

log = logging.getLogger("homesec.bot.alerts")


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
        InlineKeyboardButton(
            text="⛔ Блокировать",
            callback_data=f"nd:{dev_id}:block",
            style=DANGER,
        ),
        InlineKeyboardButton(text="Оставить как есть", callback_data=f"nd:{dev_id}:skip"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def bonus_keyboard(dev: dict) -> InlineKeyboardMarkup | None:
    """«+15» and «+30» under a "time is running out" alert: the same rule as everywhere a parent
    gives time. A device without an owner has nobody to give time to, so it gets no buttons."""
    from .reports import grant_button

    person_id = dev.get("person_id")
    if person_id is None:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[[grant_button(person_id, 15), grant_button(person_id, 30)]]
    )


STATUS_WORDS = {"approved": "одобрено", "denied": "отказано", "expired": "срок вышел"}

# ---------- запрос ребёнка «попросить ещё времени» ----------

# Пресеты одобрения родителем. "day" ≈ до конца дня (add_bonus_time режет по 720).


BONUS_PRESETS = {"15": 15, "30": 30, "day": 720}


def bonus_request_keyboard(request_id: int, category: str = "internet") -> InlineKeyboardMarkup:
    """Кнопки под заявкой ребёнка: +15 / +30 / до конца дня / Отказать.
    Для просьбы во время паузы третья кнопка открывает интернет до её конца."""
    from ..services.bonus import PAUSE_CATEGORY

    if category == PAUSE_CATEGORY:
        whole = InlineKeyboardButton(
            text="▶️ До конца паузы",
            callback_data=f"br:{request_id}:day",
            style=SUCCESS,
        )
    else:
        whole = InlineKeyboardButton(
            text="🌞 До конца дня",
            callback_data=f"br:{request_id}:day",
            style=SUCCESS,
        )
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="➕ 15 мин",
                callback_data=f"br:{request_id}:15",
                style=SUCCESS,
            ),
            InlineKeyboardButton(
                text="➕ 30 мин",
                callback_data=f"br:{request_id}:30",
                style=SUCCESS,
            ),
        ],
        [
            whole,
            InlineKeyboardButton(
                text="🚫 Отказать",
                callback_data=f"br:{request_id}:no",
                style=DANGER,
            ),
        ],
    ])


def _resolve_bonus_request(request_id: int, choice: str) -> str:
    """Одобряет (add_bonus_time) или отклоняет заявку и помечает её. Одноразово:
    повторное нажатие по обработанной заявке вернёт пометку об этом."""
    from ..models import BonusRequest
    from ..services import bonus

    s = dbmod.session()
    try:
        req = s.get(BonusRequest, request_id)
        if req is None:
            return "Заявка не найдена."
        if req.status != "pending":
            return f"На эту просьбу уже ответили: {STATUS_WORDS.get(req.status, req.status)}."
        if choice == "no":
            bonus.resolve(s, req, "denied")
            return "Отказано. Ребёнок узнает об этом от Luna."
        dev = s.get(Device, req.device_id)
        if dev is None or dev.person_id is None:
            return "Устройство без владельца, время добавить некому."
        minutes = time_grants.WHOLE_DAY if choice == "day" else BONUS_PRESETS.get(choice, 30)
        try:
            done = time_grants.grant(s, dev.person_id, minutes, "Одобрено родителем")
        except time_grants.GrantError as exc:
            return f"⚠️ {exc} Просьба осталась открытой."
        given = minutes if minutes is not None else time_grants.DAY_MINUTES
        bonus.resolve(s, req, "approved", given)
        return f"✅ Одобрено: {done}."
    finally:
        s.close()


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


async def cb_merge(cb: CallbackQuery) -> None:
    parts = (cb.data or "").split(":")
    duplicate_id, target_id = int(parts[1]), int(parts[2])
    if len(parts) > 3 and parts[3] == "no":
        result = "Хорошо, оставляю как два разных устройства."
    else:
        try:
            result = await asyncio.to_thread(
                common.run_tool_sync, "merge_devices",
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


async def cb_new_device(cb: CallbackQuery) -> None:
    parts = (cb.data or "").split(":")
    dev_id, action = int(parts[1]), parts[2]
    try:
        if action == "skip":
            result = "Оставлено в «Неизвестных»"
        elif action == "block":
            result = await asyncio.to_thread(
                common.run_tool_sync, "block_device", {"device_id": dev_id}
            )
        elif action == "kind":
            kind_val = parts[3]
            result = await asyncio.to_thread(
                common.run_tool_sync, "set_device_kind", {"device_id": dev_id, "kind": kind_val}
            )
        elif action == "assign":
            name = await asyncio.to_thread(_person_name, int(parts[3]))
            if name is None:
                await cb.answer("Этого человека уже нет в списке", show_alert=True)
                return
            result = await asyncio.to_thread(
                common.run_tool_sync, "assign_device", {"device_id": dev_id, "person_name": name}
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


async def cb_old_direct_request(cb: CallbackQuery) -> None:
    """Buttons under a child's request sent through this bot before children moved to Luna."""
    await cb.answer("Эта просьба устарела. Теперь дети просят время через Luna.", show_alert=True)
    if isinstance(cb.message, Message):
        await cb.message.edit_reply_markup(reply_markup=None)


def register(router: Router) -> None:
    """Attach to the router that carries the access middleware."""
    router.callback_query.register(cb_old_direct_request, F.data.startswith("bdir:"))
    router.callback_query.register(cb_bonus_request, F.data.startswith("br:"))
    router.callback_query.register(cb_merge, F.data.startswith("mg:"))
    router.callback_query.register(cb_new_device, F.data.startswith("nd:"))
