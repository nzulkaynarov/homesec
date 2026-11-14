"""«📺 Общие устройства»: the TV and other shared devices switched between off, children's mode
and grown-ups for a few hours, in taps."""

import asyncio

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from .. import db as dbmod
from ..models import Device
from ..services import family as family_svc

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


async def btn_shared_devices(message: Message) -> None:
    await _show_shared(message)


async def cb_shared_list(cb: CallbackQuery) -> None:
    await cb.answer()
    if isinstance(cb.message, Message):
        await _show_shared(cb.message, edit=True)


async def cb_shared_device(cb: CallbackQuery) -> None:
    dev_id = int((cb.data or "").split(":")[1])
    rows = await asyncio.to_thread(_shared_list_sync)
    await cb.answer()
    if isinstance(cb.message, Message):
        await cb.message.edit_text(
            _shared_status_text(rows, dev_id), reply_markup=_shared_actions_markup(dev_id)
        )


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


def register(router: Router) -> None:
    """Attach to the router that carries the access middleware, before catch-all text handlers."""
    router.message.register(btn_shared_devices, F.text == "📺 Общие устройства")
    router.callback_query.register(cb_shared_list, F.data == "shl")
    router.callback_query.register(cb_shared_device, F.data.startswith("sh:"))
    router.callback_query.register(cb_shared_state, F.data.startswith("shs:"))
