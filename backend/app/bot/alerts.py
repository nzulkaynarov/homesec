"""Buttons under the notices parents get: a new device to sort, a quota block, a child's request
for more time and a device that looks like a known one with a new MAC."""

import asyncio
import logging
from typing import NamedTuple

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .. import db as dbmod
from ..ai import tools
from ..models import GROUP_LABELS, Device, Person
from ..services import time_grants, time_notices
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


def bonus_keyboard(
    person_id: int | None, hold: str = time_notices.LIMITS
) -> InlineKeyboardMarkup | None:
    """«+15» and «+30» under a "time is running out" alert: the same rule as everywhere a parent
    gives time. Time goes to a person, so a device without an owner gets no buttons, and neither
    does a limit that is not the child's own, because a grant would not reach it."""
    from .reports import grant_button

    if person_id is None or hold in (time_notices.NO_OWNER, time_notices.OPEN):
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[[grant_button(person_id, 15), grant_button(person_id, 30)]]
    )


STATUS_WORDS = {"approved": "одобрено", "denied": "отказано", "expired": "срок вышел"}

# ---------- a child's request for more time, sent from Luna ----------

# What the parent's buttons give. "day" means until the pause ends or, without a pause, the
# limits are off until midnight (time_grants.WHOLE_DAY).
BONUS_PRESETS = {"15": 15, "30": 30}


def bonus_request_keyboard(
    request_id: int, hold: str = time_notices.LIMITS
) -> InlineKeyboardMarkup:
    """Buttons under a child's request, chosen by what holds the child back now (see
    time_notices): during a pause the third button opens it to its end, with nothing to add to
    there is one "allow", and a device nobody owns can only be refused."""
    deny = InlineKeyboardButton(
        text=time_notices.DENY, callback_data=f"br:{request_id}:no", style=DANGER
    )
    if hold == time_notices.NO_OWNER:
        return InlineKeyboardMarkup(inline_keyboard=[[deny]])
    if hold == time_notices.OPEN:
        allow = InlineKeyboardButton(
            text=time_notices.ALLOW, callback_data=f"br:{request_id}:day", style=SUCCESS
        )
        return InlineKeyboardMarkup(inline_keyboard=[[allow, deny]])
    whole = time_notices.WHOLE_PAUSE if hold == time_notices.PAUSE else time_notices.WHOLE_DAY
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=time_notices.ADD_15, callback_data=f"br:{request_id}:15", style=SUCCESS
            ),
            InlineKeyboardButton(
                text=time_notices.ADD_30, callback_data=f"br:{request_id}:30", style=SUCCESS
            ),
        ],
        [
            InlineKeyboardButton(text=whole, callback_data=f"br:{request_id}:day", style=SUCCESS),
            deny,
        ],
    ])


class Answer(NamedTuple):
    text: str
    final: bool  # the request is closed: the text goes under the notice and the buttons go


def _resolve_bonus_request(request_id: int, choice: str) -> Answer:
    """Give the time (time_grants.grant) or refuse, and close the request. Once only: a second
    press, by the same or the other parent, says it is already answered. When the time cannot
    be given now, the request stays open and the buttons stay, so the parent can still refuse."""
    from ..models import BonusRequest
    from ..services import bonus

    s = dbmod.session()
    try:
        req = s.get(BonusRequest, request_id)
        if req is None:
            return Answer("Этой просьбы больше нет.", True)
        if req.status != "pending":
            return Answer(
                f"На эту просьбу уже ответили: {STATUS_WORDS.get(req.status, 'закрыта')}.", True
            )
        dev = s.get(Device, req.device_id)
        person = dev.person if dev is not None else None
        if choice == "no":
            bonus.resolve(s, req, "denied")
            if person is None:
                return Answer("🚫 Отказано.", True)
            return Answer(f"🚫 {person.name}: отказано, Luna скажет об этом.", True)
        if person is None:
            return Answer(
                f"Устройство без владельца, время добавить некому. {time_notices.OWNER_HINT}",
                False,
            )
        minutes = time_grants.WHOLE_DAY if choice == "day" else BONUS_PRESETS.get(choice, 30)
        try:
            done = time_grants.grant(s, person.id, minutes, "Одобрено родителем")
        except time_grants.GrantError as exc:
            still_open = "Просьба ждёт ответа: можно отказать или ответить позже."
            return Answer(f"⚠️ {exc} {still_open}", False)
        given = minutes if minutes is not None else time_grants.DAY_MINUTES
        bonus.resolve(s, req, "approved", given)
        return Answer(f"✅ {person.name}: {done}.", True)
    finally:
        s.close()


async def cb_bonus_request(cb: CallbackQuery) -> None:
    parts = (cb.data or "").split(":")
    request_id, choice = int(parts[1]), parts[2]
    try:
        answer = await asyncio.to_thread(_resolve_bonus_request, request_id, choice)
    except Exception:
        log.exception("answering a time request failed")
        answer = Answer("⚠️ Не получилось ответить на просьбу, попробуйте ещё раз.", False)
    if not answer.final or not isinstance(cb.message, Message):
        await cb.answer(answer.text[:200], show_alert=True)
        return
    await cb.answer()
    await cb.message.edit_text(f"{cb.message.text}\n\n➡ {answer.text}", reply_markup=None)


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
