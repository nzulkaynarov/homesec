"""«⏸ Пауза»: switch all children, one child, one of their devices or a shared device off the
internet for a chosen time, in two taps. Everything goes through services.pauses."""

import asyncio
import time
from datetime import datetime
from typing import Any

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .. import db as dbmod
from ..models import Device, Person
from ..services import house_rules, pauses
from ..services.pauses import ALL_KIDS, MORNING, PRESET_MINUTES, PauseError, Target
from . import access

BTN_PAUSE = "⏸ Пауза"
CUSTOM_SECONDS = 300
MENU_TEXTS = access.ADULT_BUTTONS | {"❓ Помощь"}
GONE = "Этого больше нет в HomeSec."
ASK_TIME = (
    "На сколько отключить «{name}»? Напишите одним сообщением: "
    "45 (минут), 2ч или до 18:30."
)
Card = tuple[str, InlineKeyboardMarkup]

# (chat, user) -> (target code, deadline): who is typing a time for what.
_custom: dict[tuple[int, int], tuple[str, float]] = {}


def _button(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text[:60], callback_data=data)


def _in_session(work: Any, *args: Any) -> Any:
    session = dbmod.session()
    try:
        return work(session, *args)
    finally:
        session.close()


async def _call(work: Any, *args: Any) -> Any:
    return await asyncio.to_thread(_in_session, work, *args)


def _duration(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes} мин"
    hours = minutes // 60
    return {1: "1 час", 2: "2 часа", 3: "3 часа", 4: "4 часа"}.get(hours, f"{hours} ч")


def _overview_data(db: Any) -> dict[str, Any]:
    return {
        "kids": [(kid.id, kid.name) for kid in pauses.kids(db)],
        "shared": [(d.id, d.name) for d in pauses.shared_devices(db) if pauses.pausable(d)],
        "active": pauses.active(db),
    }


async def overview(notice: str = "") -> Card:
    data = await _call(_overview_data)
    rows: list[list[InlineKeyboardButton]] = []
    if data["kids"]:
        rows.append([_button("👶 Все дети", f"pzt:{ALL_KIDS.code}")])
        kid_buttons = [_button(f"🧒 {name}", f"pzk:{kid_id}") for kid_id, name in data["kids"]]
        rows.extend(kid_buttons[i : i + 2] for i in range(0, len(kid_buttons), 2))
    shared = [_button(f"📺 {name}", f"pzt:d{device_id}") for device_id, name in data["shared"]]
    rows.extend(shared[i : i + 2] for i in range(0, len(shared), 2))
    if data["active"]:
        rows.append([_button("▶️ Снять все паузы", "pzra")])
        listing = "\n".join(f"• {name} — до {until:%H:%M}" for name, until in data["active"])
        status = f"Сейчас без интернета:\n{listing}"
    else:
        status = "Сейчас пауз нет."
    if not rows:
        status += "\n\nВ HomeSec нет ни детей, ни общих устройств."
    prefix = f"{notice}\n\n" if notice else ""
    text = f"{prefix}⏸ Кого отключить от интернета?\n\n{status}"
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def _kid_data(db: Any, person_id: int) -> dict[str, Any] | None:
    person = db.get(Person, person_id)
    if person is None:
        return None
    devices = [(d.id, d.name) for d in pauses.kid_devices(db, person_id) if pauses.pausable(d)]
    return {"name": person.name, "devices": devices}


async def kid(person_id: int) -> Card:
    data = await _call(_kid_data, person_id)
    if data is None:
        return await overview(GONE)
    rows = [[_button(f"Все устройства: {data['name']}", f"pzt:p{person_id}")]]
    rows.extend([_button(f"📱 {name}", f"pzt:d{device_id}")] for device_id, name in data["devices"])
    rows.append([_button("← Назад", "pzm")])
    hint = (
        ""
        if data["devices"]
        else "\n\nЛичных устройств пока нет, пауза включится, когда появятся."
    )
    text = f"🧒 {data['name']}: отключить все устройства или одно?{hint}"
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def _picker_data(db: Any, target: Target) -> dict[str, Any]:
    now = datetime.now()
    owner = None
    if target.kind == "device":
        device = db.get(Device, target.id)
        owner = device.person_id if device is not None and device.kind == "personal" else None
    return {
        "name": pauses.label(db, target),
        "until": pauses.paused_until(db, target, now),
        "morning": pauses.morning(db, target, now),
        "owner": owner,
    }


async def picker(code: str, notice: str = "") -> Card:
    target = Target.parse(code)
    if target is None:
        return await overview(GONE)
    try:
        data = await _call(_picker_data, target)
    except (LookupError, PauseError) as exc:
        return await overview(str(exc) if isinstance(exc, PauseError) else GONE)
    presets = [_button(_duration(m), f"pzs:{code}:{m}") for m in PRESET_MINUTES]
    rows = [presets[:3], presets[3:]]
    rows.append([_button(f"🌙 До утра ({data['morning']:%H:%M})", f"pzs:{code}:{MORNING}")])
    rows.append([_button("⌨️ Своё время", f"pzc:{code}")])
    if data["until"] is not None:
        rows.append([_button("▶️ Включить сейчас", f"pzr:{code}")])
    back = f"pzk:{data['owner']}" if data["owner"] is not None else "pzm"
    rows.append([_button("← Назад", back)])
    state = (
        f"Сейчас без интернета до {data['until']:%H:%M}."
        if data["until"] is not None
        else "Сейчас интернет есть."
    )
    prefix = f"{notice}\n\n" if notice else ""
    text = f"{prefix}{data['name']}\n{state}\n\nНа сколько отключить?"
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def _set(db: Any, code: str, choice: str) -> str:
    target = Target.parse(code)
    if target is None:
        raise LookupError(code)
    return pauses.pause(db, target, pauses.until_for(db, target, choice))


def _set_until(db: Any, code: str, until: datetime) -> str:
    target = Target.parse(code)
    if target is None:
        raise LookupError(code)
    return pauses.pause(db, target, until)


def _resume(db: Any, code: str) -> str:
    target = Target.parse(code)
    if target is None:
        raise LookupError(code)
    return pauses.resume(db, target)


async def _act(code: str, work: Any, *args: Any) -> Card:
    try:
        result = await _call(work, code, *args)
    except LookupError:
        return await overview(GONE)
    except PauseError as exc:
        return await picker(code, f"⚠️ {exc}")
    return await picker(code, result)


def waiting_for_time(message: Message) -> bool:
    """True when this sender was asked for a pause length and is now typing it."""
    if message.from_user is None:
        return False
    key = (message.chat.id, message.from_user.id)
    pending = _custom.get(key)
    if pending is None:
        return False
    text = message.text or ""
    if pending[1] < time.monotonic() or not text or text.startswith("/") or text in MENU_TEXTS:
        _custom.pop(key, None)  # another button or command means the parent moved on
        return False
    return True


async def on_time(message: Message) -> None:
    if message.from_user is None:
        return
    pending = _custom.pop((message.chat.id, message.from_user.id), None)
    if pending is None:
        return
    code = pending[0]
    until = pauses.parse_until(message.text or "")
    if until is None:
        _custom[(message.chat.id, message.from_user.id)] = pending
        await message.answer("Не понял время. Например: 45, 2ч или до 18:30.")
        return
    text, markup = await _act(code, _set_until, until)
    await message.answer(text, reply_markup=markup)


async def _show(cb: CallbackQuery, card: Card) -> None:
    await cb.answer()
    if isinstance(cb.message, Message):
        await cb.message.edit_text(card[0], reply_markup=card[1])


def _parts(cb: CallbackQuery) -> list[str]:
    return (cb.data or "").split(":")[1:]


async def btn_pause(message: Message) -> None:
    text, markup = await overview()
    await message.answer(text, reply_markup=markup)


async def cb_overview(cb: CallbackQuery) -> None:
    await _show(cb, await overview())


async def cb_kid(cb: CallbackQuery) -> None:
    (person_id,) = _parts(cb)
    await _show(cb, await kid(int(person_id)) if person_id.isdigit() else await overview(GONE))


async def cb_picker(cb: CallbackQuery) -> None:
    (code,) = _parts(cb)
    await _show(cb, await picker(code))


async def cb_set(cb: CallbackQuery) -> None:
    code, choice = _parts(cb)
    await _show(cb, await _act(code, _set, choice))


async def cb_custom(cb: CallbackQuery) -> None:
    (code,) = _parts(cb)
    target = Target.parse(code)
    try:
        name = await _call(pauses.label, target) if target is not None else None
    except (LookupError, PauseError):
        name = None
    if name is None:
        await _show(cb, await overview(GONE))
        return
    if isinstance(cb.message, Message):
        _custom[(cb.message.chat.id, cb.from_user.id)] = (code, time.monotonic() + CUSTOM_SECONDS)
    cancel = InlineKeyboardMarkup(inline_keyboard=[[_button("← Отмена", f"pzt:{code}")]])
    await _show(cb, (ASK_TIME.format(name=name), cancel))


async def cb_resume(cb: CallbackQuery) -> None:
    (code,) = _parts(cb)
    await _show(cb, await _act(code, _resume))


async def cb_resume_all(cb: CallbackQuery) -> None:
    removed = await _call(house_rules.resume_all)
    await _show(cb, await overview(f"▶️ Сняты все паузы: {removed}" if removed else "Пауз не было."))


def register(router: Router) -> None:
    """Attach to the router that carries the access middleware, before catch-all text handlers."""
    router.message.register(btn_pause, F.text == BTN_PAUSE)
    router.message.register(on_time, waiting_for_time)
    router.callback_query.register(cb_overview, F.data == "pzm")
    router.callback_query.register(cb_kid, F.data.startswith("pzk:"))
    router.callback_query.register(cb_picker, F.data.startswith("pzt:"))
    router.callback_query.register(cb_set, F.data.startswith("pzs:"))
    router.callback_query.register(cb_custom, F.data.startswith("pzc:"))
    router.callback_query.register(cb_resume, F.data.startswith("pzr:"))
    router.callback_query.register(cb_resume_all, F.data == "pzra")
