"""Every device on one screen per owner, with buttons to move it somewhere else."""

import asyncio
from typing import Any

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from .. import db as dbmod
from ..ai.tools import ToolError
from ..models import DEVICE_KIND_LABELS, Device, Person
from ..services import device_owners
from .triage import vendor_of

BTN_DEVICES = "📱 Устройства"
PAGE_SIZE = 10
TEXT_LIMIT = 4000  # Telegram rejects messages above 4096 characters
GONE = "Этого устройства уже нет."
Card = tuple[str, InlineKeyboardMarkup]


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


def _overview_rows(db: Any) -> list[dict[str, Any]]:
    return device_owners.overview(db)


async def _overview(notice: str = "") -> Card:
    sections = await _call(_overview_rows)
    rows = [
        [_button(f"{s['label']}: {s['count']}", f"dms:{s['key']}:0")]
        for s in sections
        if s["count"] or s["key"].startswith("p")
    ]
    prefix = f"{notice}\n\n" if notice else ""
    blocks = [
        f"{s['label']}: {s['count']}\n" + "\n".join(f"• {name}" for name in s["devices"])
        for s in sections
        if s["devices"]
    ]
    listing = "\n\n".join(blocks) or "Устройств пока нет."
    text = f"{prefix}📱 Все устройства\n\n{listing}\n\nЧтобы сменить владельца, нажми на группу."
    return text[:TEXT_LIMIT], InlineKeyboardMarkup(inline_keyboard=rows)


def _section_rows(db: Any, section: str) -> tuple[str, list[tuple[int, str]]]:
    title = next((s["label"] for s in device_owners.overview(db) if s["key"] == section), "")
    rows = [(d.id, device_owners.describe(d)) for d in device_owners.devices_in(db, section)]
    return title, rows


async def _section(section: str, page: int, notice: str = "") -> Card:
    title, devices = await _call(_section_rows, section)
    last_page = max(len(devices) - 1, 0) // PAGE_SIZE
    page = max(0, min(page, last_page))
    shown = devices[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
    rows = [[_button(label, f"dmd:{device_id}:{section}:{page}")] for device_id, label in shown]
    navigation = []
    if page > 0:
        navigation.append(_button("←", f"dms:{section}:{page - 1}"))
    if page < last_page:
        navigation.append(_button("→", f"dms:{section}:{page + 1}"))
    if navigation:
        rows.append(navigation)
    rows.append([_button("← Все устройства", "dm")])
    prefix = f"{notice}\n\n" if notice else ""
    text = f"{prefix}{title}: {len(devices)}" if devices else f"{prefix}{title}: пока пусто"
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def _device_view(db: Any, device_id: int) -> dict[str, Any] | None:
    device = db.get(Device, device_id)
    if device is None:
        return None
    owner = device.person.name if device.person else "нет"
    return {
        "name": device.name,
        "hostname": device.hostname,
        "mac": device.mac,
        "ip": device.ip,
        "kind": device.kind,
        "kind_label": DEVICE_KIND_LABELS.get(device.kind, device.kind),
        "owner": owner,
        "has_owner": device.person is not None,
        "random": device.mac_is_random,
        "is_pi": device_owners.is_this_pi(device),
    }


async def _device(device_id: int, section: str, page: int) -> Card:
    view = await _call(_device_view, device_id)
    if view is None:
        return await _section(section, page, GONE)
    lines = [view["name"], f"Тип: {view['kind_label']}", f"Владелец: {view['owner']}"]
    if view["hostname"] and view["hostname"] != view["name"]:
        lines.append(f"Имя в сети: {view['hostname']}")
    lines.append(f"Адрес: {view['ip'] or 'нет в сети'}")
    lines.append(f"MAC: {view['mac']}")
    if not view["random"]:
        vendor = await vendor_of(view["mac"])
        if vendor:
            lines.append(f"Производитель: {vendor}")
    back = _button("← Назад", f"dms:{section}:{page}")
    if view["is_pi"]:
        lines.append("\nЭто сама Pi, менять её нельзя.")
        return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=[[back]])
    tail = f"{device_id}:{{}}:{section}:{page}"
    kinds = [
        _button(label, "dmc:" + tail.format(kind))
        for kind, label in (("shared", "📺 Общее"), ("iot", "🔌 Умный дом"))
        if view["kind"] != kind
    ]
    rows = [[_button("👤 Сменить владельца", f"dmp:{device_id}:{section}:{page}")]]
    if kinds:
        rows.append(kinds)
    if view["kind"] != "infra":
        rows.append([_button("🛠 Инфраструктура", "dmc:" + tail.format("infra"))])
    if view["has_owner"] or view["kind"] != "personal":
        rows.append([_button("❓ Без владельца", "dmc:" + tail.format(device_owners.NO_OWNER))])
    rows.append([back])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def _people_rows(db: Any) -> list[tuple[int, str]]:
    return [(p.id, p.name) for p in db.scalars(select(Person).order_by(Person.name))]


async def _people(device_id: int, section: str, page: int) -> Card:
    people = await _call(_people_rows)
    buttons = [_button(name, f"dmc:{device_id}:{pid}:{section}:{page}") for pid, name in people]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    rows.append([_button("← Назад", f"dmd:{device_id}:{section}:{page}")])
    return "Чьё это устройство?", InlineKeyboardMarkup(inline_keyboard=rows)


def _move(db: Any, device_id: int, destination: str) -> dict[str, str]:
    return device_owners.move(db, device_id, destination)


async def _apply(device_id: int, destination: str, section: str, page: int) -> Card:
    try:
        result = await _call(_move, device_id, destination)
    except LookupError:
        return await _section(section, page, GONE)
    except ToolError as exc:
        return await _section(section, page, str(exc))
    return await _section(section, page, f"✅ «{result['name']}» → {result['label']}")


async def _choose(device_id: int, destination: str, section: str, page: int) -> Card:
    """Network equipment under a person's limits can take the whole home offline, so it asks."""
    view = await _call(_device_view, device_id)
    if view is None:
        return await _section(section, page, GONE)
    if view["kind"] != "infra" or destination == "infra":
        return await _apply(device_id, destination, section, page)
    text = (
        f"«{view['name']}» сейчас инфраструктура и не блокируется. "
        "Если перенести, на него начнут действовать правила "
        "и он может потерять интернет. Перенести?"
    )
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [_button("Да, перенести", f"dmy:{device_id}:{destination}:{section}:{page}")],
            [_button("← Назад", f"dmd:{device_id}:{section}:{page}")],
        ]
    )
    return text, markup


async def _show(cb: CallbackQuery, card: Card) -> None:
    await cb.answer()
    if isinstance(cb.message, Message):
        await cb.message.edit_text(card[0], reply_markup=card[1])


def _parts(cb: CallbackQuery) -> list[str]:
    return (cb.data or "").split(":")[1:]


async def btn_devices(message: Message) -> None:
    text, markup = await _overview()
    await message.answer(text, reply_markup=markup)


async def cb_overview(cb: CallbackQuery) -> None:
    await _show(cb, await _overview())


async def cb_section(cb: CallbackQuery) -> None:
    section, page = _parts(cb)
    await _show(cb, await _section(section, int(page)))


async def cb_device(cb: CallbackQuery) -> None:
    device_id, section, page = _parts(cb)
    await _show(cb, await _device(int(device_id), section, int(page)))


async def cb_people(cb: CallbackQuery) -> None:
    device_id, section, page = _parts(cb)
    await _show(cb, await _people(int(device_id), section, int(page)))


async def cb_choose(cb: CallbackQuery) -> None:
    device_id, destination, section, page = _parts(cb)
    await _show(cb, await _choose(int(device_id), destination, section, int(page)))


async def cb_apply(cb: CallbackQuery) -> None:
    device_id, destination, section, page = _parts(cb)
    await _show(cb, await _apply(int(device_id), destination, section, int(page)))


def register(router: Router) -> None:
    """Attach to the router that carries the access middleware, never to a separate one."""
    router.message.register(btn_devices, F.text == BTN_DEVICES)
    router.callback_query.register(cb_overview, F.data == "dm")
    router.callback_query.register(cb_section, F.data.startswith("dms:"))
    router.callback_query.register(cb_device, F.data.startswith("dmd:"))
    router.callback_query.register(cb_people, F.data.startswith("dmp:"))
    router.callback_query.register(cb_choose, F.data.startswith("dmc:"))
    router.callback_query.register(cb_apply, F.data.startswith("dmy:"))
