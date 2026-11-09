"""Menu for sorting devices nobody has claimed yet, one group per card."""

import asyncio
import logging
from typing import Any

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from mac_vendor_lookup import AsyncMacLookup
from sqlalchemy import select

from .. import db as dbmod
from ..ai.tools import ToolError
from ..models import Person
from ..services import device_triage

log = logging.getLogger("homesec.bot.triage")

BTN_TRIAGE = "🗂 Разбор устройств"
KIND_LABELS = {"shared": "общее устройство", "iot": "умный дом", "infra": "инфраструктура"}
ALREADY_SORTED = "Это устройство уже разобрали."
Card = tuple[str, InlineKeyboardMarkup | None]


async def vendor_of(mac: str) -> str | None:
    """A hint only. The registry is parsed per card so the Pi keeps no copy in memory."""
    try:
        return str(await AsyncMacLookup().lookup(mac))
    except Exception:
        log.debug("no vendor for this address", exc_info=True)
        return None


def _queue_sync() -> list[dict[str, Any]]:
    session = dbmod.session()
    try:
        return device_triage.list_groups(session)
    finally:
        session.close()


def _people_sync() -> list[tuple[int, str]]:
    session = dbmod.session()
    try:
        return [(p.id, p.name) for p in session.scalars(select(Person).order_by(Person.name))]
    finally:
        session.close()


def _sort_sync(device_id: int, destination: str) -> dict[str, Any]:
    session = dbmod.session()
    person_id = None if destination in KIND_LABELS else int(destination)
    kind = destination if destination in KIND_LABELS else None
    try:
        return device_triage.sort_group(session, device_id, person_id, kind)
    finally:
        session.close()


def card_keyboard(device_id: int, index: int, total: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="👤 Чьё это?", callback_data=f"trgp:{device_id}:{index}")],
        [
            InlineKeyboardButton(text="📺 Общее", callback_data=f"trgc:{device_id}:shared:{index}"),
            InlineKeyboardButton(
                text="🔌 Умный дом", callback_data=f"trgc:{device_id}:iot:{index}"
            ),
        ],
        [
            InlineKeyboardButton(
                text="🛠 Инфраструктура", callback_data=f"trgc:{device_id}:infra:{index}"
            )
        ],
    ]
    if total > 1:
        rows.append([InlineKeyboardButton(text="⏭ Дальше", callback_data=f"trg:{index + 1}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def card_text(group: dict[str, Any], index: int, total: int, vendor: str | None) -> str:
    lines = [f"🗂 Разбор устройств: {index + 1} из {total}", "", f"«{group['name']}»"]
    if group["count"] > 1:
        lines.append(
            f"Записей: {group['count']}. Адрес приватный и меняется, "
            "поэтому устройство появлялось много раз."
        )
    elif group["mac_random"]:
        lines.append("Адрес приватный, производителя не определить.")
    if vendor:
        lines.append(f"Производитель: {vendor}")
    if group["ip"]:
        lines.append(f"Последний адрес в сети: {group['ip']}")
    lines.append(f"Последняя новая запись: {group['last_seen'].replace('T', ' ')}")
    if group["owners"]:
        names = ", ".join(owner["name"] for owner in group["owners"])
        lines.append(f"С таким именем уже есть у: {names}")
    return "\n".join(lines)


async def _card(index: int, notice: str = "") -> Card:
    prefix = f"{notice}\n\n" if notice else ""
    groups = await asyncio.to_thread(_queue_sync)
    if not groups:
        return f"{prefix}Все устройства разобраны ✅", None
    index %= len(groups)
    group = groups[index]
    vendor = None if group["mac_random"] else await vendor_of(group["mac"])
    text = prefix + card_text(group, index, len(groups), vendor)
    return text, card_keyboard(int(group["id"]), index, len(groups))


async def _find_group(device_id: int) -> dict[str, Any] | None:
    groups = await asyncio.to_thread(_queue_sync)
    return next((g for g in groups if int(g["id"]) == device_id), None)


async def _people_card(device_id: int, index: int) -> Card:
    group = await _find_group(device_id)
    if group is None:
        return await _card(index, ALREADY_SORTED)
    owners = {int(owner["id"]) for owner in group["owners"]}
    people = sorted(await asyncio.to_thread(_people_sync), key=lambda p: (p[0] not in owners, p[1]))
    buttons = [
        InlineKeyboardButton(text=name[:30], callback_data=f"trgc:{device_id}:{pid}:{index}")
        for pid, name in people
    ]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton(text="← Назад", callback_data=f"trg:{index}")])
    return f"Чьё устройство «{group['name']}»?", InlineKeyboardMarkup(inline_keyboard=rows)


async def _apply(device_id: int, destination: str, index: int) -> Card:
    try:
        result = await asyncio.to_thread(_sort_sync, device_id, destination)
    except LookupError:
        return await _card(index, ALREADY_SORTED)
    except ToolError:
        return await _card(index + 1, "Это устройство сети, его не трогаю.")
    where = result["person"] or KIND_LABELS[result["kind"]]
    return await _card(index, f"✅ «{result['name']}» → {where}")


async def _choose(device_id: int, destination: str, index: int) -> Card:
    """Folding many records cannot be undone with a button, so it asks first."""
    group = await _find_group(device_id)
    if group is None:
        return await _card(index, ALREADY_SORTED)
    if group["count"] == 1:
        return await _apply(device_id, destination, index)
    label = KIND_LABELS.get(destination)
    if label is None:
        people = dict(await asyncio.to_thread(_people_sync))
        label = people.get(int(destination), "выбранному человеку")
    text = (
        f"Объединить {group['count']} записей «{group['name']}» "
        f"в одно устройство и записать: {label}?\n\n"
        "Если такие устройства есть у нескольких людей, все записи достанутся одному."
    )
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Да", callback_data=f"trgy:{device_id}:{destination}:{index}"
                )
            ],
            [InlineKeyboardButton(text="← Назад", callback_data=f"trg:{index}")],
        ]
    )
    return text, markup


async def _show(cb: CallbackQuery, card: Card) -> None:
    await cb.answer()
    if isinstance(cb.message, Message):
        await cb.message.edit_text(card[0], reply_markup=card[1])


async def btn_triage(message: Message) -> None:
    text, markup = await _card(0)
    await message.answer(text, reply_markup=markup)


async def cb_card(cb: CallbackQuery) -> None:
    await _show(cb, await _card(int((cb.data or "").split(":")[1])))


async def cb_people(cb: CallbackQuery) -> None:
    _, device_id, index = (cb.data or "").split(":")
    await _show(cb, await _people_card(int(device_id), int(index)))


async def cb_choose(cb: CallbackQuery) -> None:
    _, device_id, destination, index = (cb.data or "").split(":")
    await _show(cb, await _choose(int(device_id), destination, int(index)))


async def cb_apply(cb: CallbackQuery) -> None:
    _, device_id, destination, index = (cb.data or "").split(":")
    await _show(cb, await _apply(int(device_id), destination, int(index)))


def register(router: Router) -> None:
    """Attach to the router that carries the access middleware, never to a separate one."""
    router.message.register(btn_triage, F.text == BTN_TRIAGE)
    router.callback_query.register(cb_card, F.data.startswith("trg:"))
    router.callback_query.register(cb_people, F.data.startswith("trgp:"))
    router.callback_query.register(cb_choose, F.data.startswith("trgc:"))
    router.callback_query.register(cb_apply, F.data.startswith("trgy:"))
