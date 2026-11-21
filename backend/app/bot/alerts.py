"""Buttons under the notices parents get: a new device to sort, a quota block, a child's request
for more time and a device that looks like a known one with a new MAC."""

import asyncio
import logging
from typing import Any, NamedTuple

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .. import db as dbmod
from ..ai import tools
from ..models import GROUP_LABELS, BonusRequest, Device, Person
from ..services import bonus, device_owners, followups, time_grants, time_notices
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
    """«+15» and «+30» under a "time is running out" alert: they add minutes to the limits, also
    during a pause, which they leave as it is. Time goes to a person, so a device without an owner
    gets no buttons; neither does a limit that is gone, nor the night, when nothing is added."""
    from .reports import grant_button

    if person_id is None or hold not in (time_notices.PAUSE, time_notices.LIMITS):
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            grant_button(person_id, 15, limits_only=True),
            grant_button(person_id, 30, limits_only=True),
        ]]
    )


STATUS_WORDS = {"approved": "одобрено", "denied": "отказано"}
EXPIRED_TEXT = "Просьба закрылась сама: за сутки на неё никто не ответил."
CHANGED = "Пока сообщение ждало, всё изменилось. Выше то, что сейчас: выберите ещё раз."

# ---------- a child's request for more time, sent from Luna ----------

# What the parent's buttons give. "day" means until the pause ends or, without a pause, the
# limits are off until midnight (time_grants.WHOLE_DAY); "ok" closes a request with nothing to add.
BONUS_PRESETS = {"15": 15, "30": 30}
GRANT_CHOICES = ("15", "30", "day")
UNBLOCK = "bru"  # bru:<request id>:<device id>: switch a device on from under the request
OWNER = "bro"  # bro:<request id>:<person id>: say whose the asking device is


def bonus_request_keyboard(
    request_id: int,
    hold: str = time_notices.LIMITS,
    blocked: time_notices.Choices = (),
    people: time_notices.Choices = (),
) -> InlineKeyboardMarkup:
    """Buttons under a child's request, chosen by what holds the child back now (see
    time_notices): during a pause the third button opens it to its end, with nothing to add to
    there is one "allow", a device switched off by hand can be switched on, and for a device
    nobody owns the parent says whose it is. The hold goes into the callback data, so a press
    made after it changed shows the request again instead of acting on the old promise."""
    code = time_notices.HOLD_CODES[hold]

    def choice(text: str, value: str, style: str = SUCCESS) -> InlineKeyboardButton:
        return InlineKeyboardButton(
            text=text, callback_data=f"br:{request_id}:{value}:{code}", style=style
        )

    deny = choice(time_notices.DENY, "no", DANGER)
    switch_on = [
        [
            InlineKeyboardButton(
                text=f"✅ Включить «{name}»"[:60],
                callback_data=f"{UNBLOCK}:{request_id}:{device_id}",
                style=SUCCESS,
            )
        ]
        for device_id, name in blocked
    ]
    if hold == time_notices.NO_OWNER:
        owners = [
            InlineKeyboardButton(text=f"→ {name}"[:60], callback_data=f"{OWNER}:{request_id}:{pid}")
            for pid, name in people
        ]
        rows = [owners[i : i + 2] for i in range(0, len(owners), 2)]
        return InlineKeyboardMarkup(inline_keyboard=[*rows, [deny]])
    if hold == time_notices.BLOCKED:
        return InlineKeyboardMarkup(inline_keyboard=[*switch_on, [deny]])
    if hold == time_notices.OPEN:
        return InlineKeyboardMarkup(inline_keyboard=[[choice(time_notices.ALLOW, "ok"), deny]])
    whole = time_notices.WHOLE_PAUSE if hold == time_notices.PAUSE else time_notices.WHOLE_DAY
    return InlineKeyboardMarkup(inline_keyboard=[
        [choice(time_notices.ADD_15, "15"), choice(time_notices.ADD_30, "30")],
        *switch_on,
        [choice(whole, "day"), deny],
    ])


def request_keyboard(request_id: int, notice: time_notices.Notice) -> InlineKeyboardMarkup:
    return bonus_request_keyboard(request_id, notice.hold, notice.blocked, notice.people)


class Answer(NamedTuple):
    text: str
    final: bool  # the request is closed: the text goes under the notice and the buttons go
    redraw: time_notices.Notice | None = None  # the request as it is now, with fresh buttons


def _pending(s: Any, request_id: int) -> BonusRequest | Answer:
    """The request while it waits for an answer, or what to say when it no longer does."""
    req = s.get(BonusRequest, request_id)
    if req is None:
        return Answer("Этой просьбы больше нет.", True)
    if req.status == "pending":
        return req
    if req.status == followups.EXPIRED:  # nobody answered: not "the other parent did"
        return Answer(EXPIRED_TEXT, True)
    return Answer(f"На эту просьбу уже ответили: {STATUS_WORDS.get(req.status, 'закрыта')}.", True)


def _resolve_bonus_request(request_id: int, choice: str, drawn_for: str | None = None) -> Answer:
    """Give the time (time_grants.grant) or refuse, and close the request. Once only: a second
    press, by the same or the other parent, says it is already answered. A press on buttons drawn
    for another state (`drawn_for`, a time_notices.HOLD_CODES value; None on old messages) gives
    nothing and shows the request as it is now. When the time cannot be given now, the request
    stays open and the buttons stay, so the parent can still refuse."""
    s = dbmod.session()
    try:
        req = _pending(s, request_id)
        if isinstance(req, Answer):
            return req
        dev = s.get(Device, req.device_id)
        person = dev.person if dev is not None else None
        if choice == "no":
            bonus.resolve(s, req, "denied")
            if person is None:
                return Answer("🚫 Отказано.", True)
            return Answer(f"🚫 {person.name}: отказано, Luna скажет об этом.", True)
        notice = time_notices.request_notice(s, req)
        if person is None or drawn_for != time_notices.HOLD_CODES[notice.hold]:
            return Answer(CHANGED, False, notice)
        if notice.hold == time_notices.OPEN and choice == "ok":
            bonus.resolve(s, req, "approved", 0)  # nothing was added: Luna must not say it was
            said = "ограничений сейчас нет, Luna передаст, что вы разрешили"
            return Answer(f"👍 {person.name}: {said}.", True)
        if choice not in GRANT_CHOICES or notice.hold not in (
            time_notices.BEDTIME, time_notices.PAUSE, time_notices.LIMITS
        ):
            return Answer(CHANGED, False, notice)
        minutes = time_grants.WHOLE_DAY if choice == "day" else BONUS_PRESETS[choice]
        try:
            granted = time_grants.grant(s, person.id, minutes, "Одобрено родителем")
        except time_grants.GrantError as exc:
            still_open = "Просьба ждёт ответа: можно отказать или ответить позже."
            return Answer(f"⚠️ {exc} {still_open}", False)
        bonus.resolve(s, req, "approved", granted.minutes)
        return Answer(f"✅ {person.name}: {granted.text}.", True)
    finally:
        s.close()


async def _show_request(message: Message, request_id: int, notice: time_notices.Notice) -> None:
    try:
        await message.edit_text(notice.text, reply_markup=request_keyboard(request_id, notice))
    except TelegramBadRequest:  # the same text and buttons: nothing to change
        pass


async def _answer_request(cb: CallbackQuery, request_id: int, answer: Answer) -> None:
    if answer.redraw is not None and isinstance(cb.message, Message):
        await cb.answer(answer.text[:200], show_alert=True)
        await _show_request(cb.message, request_id, answer.redraw)
        return
    if not answer.final or not isinstance(cb.message, Message):
        await cb.answer(answer.text[:200], show_alert=True)
        return
    await cb.answer()
    await cb.message.edit_text(f"{cb.message.text}\n\n➡ {answer.text}", reply_markup=None)


async def _answer_with(cb: CallbackQuery, request_id: int, work: Any, *args: Any) -> None:
    try:
        answer = await asyncio.to_thread(work, request_id, *args)
    except Exception:
        log.exception("answering a time request failed")
        answer = Answer("⚠️ Не получилось ответить на просьбу, попробуйте ещё раз.", False)
    await _answer_request(cb, request_id, answer)


async def cb_bonus_request(cb: CallbackQuery) -> None:
    parts = (cb.data or "").split(":")
    drawn_for = parts[3] if len(parts) > 3 else None
    await _answer_with(cb, int(parts[1]), _resolve_bonus_request, parts[2], drawn_for)


def _switch_on_for_request(request_id: int, device_id: int) -> Answer:
    """Switch on a device the parent blocked by hand, then show the request as it is now."""
    s = dbmod.session()
    try:
        req = _pending(s, request_id)
        if isinstance(req, Answer):
            return req
        asking = s.get(Device, req.device_id)
        device = s.get(Device, device_id)
        owner = asking.person_id if asking is not None else None
        if device is None or asking is None or (
            device.id != asking.id and (owner is None or device.person_id != owner)
        ):
            text = "Этого устройства уже нет у ребёнка."
        else:
            if device.blocked_manual:
                device_owners.set_blocked(s, device.id, False)
            text = f"✅ Включено: «{device.name}»."
        return Answer(text, False, time_notices.request_notice(s, req))
    finally:
        s.close()


def _owner_for_request(request_id: int, person_id: int) -> Answer:
    """Give the asking device to the person the parent chose, then show the request again: now
    there is someone to give the time to."""
    s = dbmod.session()
    try:
        req = _pending(s, request_id)
        if isinstance(req, Answer):
            return req
        device = s.get(Device, req.device_id)
        if device is None:
            return Answer("Этого устройства больше нет, время дать некому.", False)
        if device.person is not None:
            text = f"Владелец уже указан: {device.person.name}."
        else:
            try:
                moved = device_owners.move(s, device.id, str(person_id))
            except LookupError:
                text = "Этого человека уже нет в списке."
            except tools.ToolError as exc:
                return Answer(f"⚠️ {exc}", False)
            else:
                text = f"✅ «{moved['name']}» → {moved['label']}"
        return Answer(text, False, time_notices.request_notice(s, req))
    finally:
        s.close()


async def cb_switch_on_for_request(cb: CallbackQuery) -> None:
    _, request_id, device_id = (cb.data or "").split(":")
    await _answer_with(cb, int(request_id), _switch_on_for_request, int(device_id))


async def cb_owner_for_request(cb: CallbackQuery) -> None:
    _, request_id, person_id = (cb.data or "").split(":")
    await _answer_with(cb, int(request_id), _owner_for_request, int(person_id))


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
    router.callback_query.register(cb_switch_on_for_request, F.data.startswith(f"{UNBLOCK}:"))
    router.callback_query.register(cb_owner_for_request, F.data.startswith(f"{OWNER}:"))
    router.callback_query.register(cb_merge, F.data.startswith("mg:"))
    router.callback_query.register(cb_new_device, F.data.startswith("nd:"))
