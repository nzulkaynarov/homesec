"""Every device on one screen per owner, and a card to rename, merge, block or delete it."""

import asyncio
import time
from typing import Any

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from .. import db as dbmod
from ..ai.tools import ToolError
from ..models import DEVICE_KIND_LABELS, Device, Person
from ..services import device_owners, device_triage
from . import access
from .triage import vendor_of

BTN_DEVICES = "📱 Устройства"
PAGE_SIZE = 10
TEXT_LIMIT = 4000  # Telegram rejects messages above 4096 characters
PURGE_LIST_LIMIT = 30
RENAME_SECONDS = 300
GONE = "Этого устройства уже нет."
STILL_ONLINE = (
    "Устройство сейчас в сети: после удаления оно сразу появится снова, уже без владельца."
)
STALE_TITLE = f"🧹 Не в сети больше {device_owners.STALE_DAYS} дней"
MENU_TEXTS = access.ADULT_BUTTONS | {"❓ Помощь"}
# upload/download, as RouterOS simple queues take them
SPEED_PRESETS = ("2M/2M", "5M/5M", "10M/10M", "20M/20M")
Card = tuple[str, InlineKeyboardMarkup]

# (chat, user) -> (device, section, page, deadline): who is typing a new name for what.
_renames: dict[tuple[int, int], tuple[int, str, int, float]] = {}


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


def _overview_data(db: Any) -> tuple[list[dict[str, Any]], int, int]:
    unsorted = sum(group["count"] for group in device_triage.list_groups(db))
    return device_owners.overview(db), len(device_owners.stale(db)), unsorted


async def _overview(notice: str = "") -> Card:
    sections, stale, unsorted = await _call(_overview_data)
    rows = [
        [_button(f"{s['label']}: {s['count']}", f"dms:{s['key']}:0")]
        for s in sections
        if s["count"] or s["key"].startswith("p")
    ]
    if unsorted:
        rows.append([_button(f"🗂 Разобрать без владельца: {unsorted}", "trg:0")])
    if stale:
        rows.append([_button(f"🧹 Давно не в сети: {stale}", f"dms:{device_owners.STALE}:0")])
    prefix = f"{notice}\n\n" if notice else ""
    blocks = [
        f"{s['label']}: {s['count']}\n" + "\n".join(f"• {name}" for name in s["devices"])
        for s in sections
        if s["devices"]
    ]
    listing = "\n\n".join(blocks) or "Устройств пока нет."
    text = f"{prefix}📱 Все устройства\n\n{listing}\n\n🟢 — сейчас в сети. Нажми на группу."
    return text[:TEXT_LIMIT], InlineKeyboardMarkup(inline_keyboard=rows)


def _section_rows(db: Any, section: str) -> tuple[str, list[tuple[int, str]], int]:
    if section == device_owners.STALE:
        title, purge = STALE_TITLE, len(device_owners.purgeable(db))
    else:
        sections = device_owners.overview(db)
        title, purge = next((s["label"] for s in sections if s["key"] == section), ""), 0
    rows = [(d.id, device_owners.label(d)) for d in device_owners.devices_in(db, section)]
    return title, rows, purge


async def _section(section: str, page: int, notice: str = "") -> Card:
    title, devices, purge = await _call(_section_rows, section)
    last_page = max(len(devices) - 1, 0) // PAGE_SIZE
    page = max(0, min(page, last_page))
    shown = devices[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
    rows = [[_button(text, f"dmd:{device_id}:{section}:{page}")] for device_id, text in shown]
    navigation = []
    if page > 0:
        navigation.append(_button("←", f"dms:{section}:{page - 1}"))
    if page < last_page:
        navigation.append(_button("→", f"dms:{section}:{page + 1}"))
    if navigation:
        rows.append(navigation)
    if purge:
        rows.append([_button(f"🗑 Удалить все без владельца: {purge}", "dmz")])
    rows.append([_button("← Все устройства", "dm")])
    prefix = f"{notice}\n\n" if notice else ""
    text = f"{prefix}{title}: {len(devices)}" if devices else f"{prefix}{title}: пока пусто"
    if section == device_owners.STALE and devices:
        text += (
            "\n\nУдалённое устройство при следующем подключении появится как новое. "
            "Устройства людей и заблокированные удаляйте по одному из карточки."
        )
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def _device_view(db: Any, device_id: int) -> dict[str, Any] | None:
    device = db.get(Device, device_id)
    if device is None:
        return None
    return {
        "name": device.name,
        "hostname": device.hostname,
        "mac": device.mac,
        "ip": device.ip,
        "kind": device.kind,
        "kind_label": DEVICE_KIND_LABELS.get(device.kind, device.kind),
        "owner": device.person.name if device.person else "нет",
        "has_owner": device.person is not None,
        "random": device.mac_is_random,
        "is_pi": device_owners.is_this_pi(device),
        "online": device_owners.is_online(device),
        "seen": device_owners.seen_text(device),
        "blocked": device.blocked_manual,
        "speed": device.speed_limit,
    }


def _card_actions(view: dict[str, Any], device_id: int, section: str, page: int) -> list[list]:
    at = f"{device_id}:{{}}:{section}:{page}"
    here = f"{device_id}:{section}:{page}"
    rows = [
        [
            _button("✏️ Переименовать", f"dmr:{here}"),
            _button("🔁 Это же устройство…", f"dmm:{here}"),
        ],
        [_button("👤 Сменить владельца", f"dmp:{here}")],
    ]
    kinds = [
        _button(text, "dmc:" + at.format(kind))
        for kind, text in (("shared", "📺 Общее"), ("iot", "🔌 Умный дом"))
        if view["kind"] != kind
    ]
    if kinds:
        rows.append(kinds)
    if view["kind"] != "infra":
        rows.append([_button("🛠 Инфраструктура", "dmc:" + at.format("infra"))])
    if view["has_owner"] or view["kind"] != "personal":
        rows.append([_button("❓ Без владельца", "dmc:" + at.format(device_owners.NO_OWNER))])
    actions = []
    if view["kind"] != "infra":
        if view["blocked"]:
            actions.append(_button("✅ Разблокировать", "dmb:" + at.format(0)))
        else:
            actions.append(_button("⛔ Заблокировать", "dmb:" + at.format(1)))
    if not view["online"]:
        actions.append(_button("🗑 Удалить", f"dmx:{here}"))
    if actions:
        rows.append(actions)
    if view["kind"] != "infra":
        rows.append([_button("🐢 Скорость", f"dmv:{here}")])
    return rows


async def _device(device_id: int, section: str, page: int, notice: str = "") -> Card:
    view = await _call(_device_view, device_id)
    if view is None:
        return await _section(section, page, GONE)
    lines = [view["name"], f"Тип: {view['kind_label']}", f"Владелец: {view['owner']}"]
    if view["hostname"] and view["hostname"] != view["name"]:
        lines.append(f"Имя в сети: {view['hostname']}")
    lines.append(f"В сети: {view['seen']}")
    lines.append(f"Адрес: {view['ip'] or 'нет'}")
    lines.append(f"MAC: {view['mac']}")
    if view["blocked"]:
        lines.append("⛔ Заблокировано вручную")
    if view["speed"]:
        lines.append(f"🐢 Скорость: {view['speed']}")
    if not view["random"]:
        vendor = await vendor_of(view["mac"])
        if vendor:
            lines.append(f"Производитель: {vendor}")
    text = (f"{notice}\n\n" if notice else "") + "\n".join(lines)
    back = _button("← Назад", f"dms:{section}:{page}")
    if view["is_pi"]:
        text += "\n\nЭто сама Pi, менять её нельзя."
        return text, InlineKeyboardMarkup(inline_keyboard=[[back]])
    rows = _card_actions(view, device_id, section, page)
    rows.append([back])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


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


def _candidate_rows(db: Any, device_id: int) -> list[tuple[int, str]]:
    return [
        (d.id, f"{device_owners.label(d)} · {d.person.name if d.person else 'без владельца'}")
        for d in device_owners.merge_candidates(db, device_id)
    ]


async def _merge_pick(device_id: int, section: str, page: int) -> Card:
    view = await _call(_device_view, device_id)
    if view is None:
        return await _section(section, page, GONE)
    candidates = await _call(_candidate_rows, device_id)
    rows = [
        [_button(text, f"dmg:{device_id}:{other}:{section}:{page}")] for other, text in candidates
    ]
    rows.append([_button("← Назад", f"dmd:{device_id}:{section}:{page}")])
    text = (
        f"С какой записью «{view['name']}» — одно и то же устройство?\n\n"
        "Сверху записи с тем же именем в сети. Выбранная запись войдёт в эту."
    )
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def _merge_confirm(keep: int, fold: int, section: str, page: int) -> Card:
    kept, folded = await _call(_device_view, keep), await _call(_device_view, fold)
    if kept is None or folded is None:
        return await _section(section, page, GONE)
    text = (
        f"Объединить «{folded['name']}» ({folded['mac']}) с «{kept['name']}»?\n\n"
        f"Останется «{kept['name']}», владелец: {kept['owner']}. "
        "Адреса, минуты и просьбы второй записи перейдут к ней. Отменить нельзя."
    )
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [_button("✅ Объединить", f"dmgy:{keep}:{fold}:{section}:{page}")],
            [_button("← Назад", f"dmm:{keep}:{section}:{page}")],
        ]
    )
    return text, markup


async def _merge(keep: int, fold: int, section: str, page: int) -> Card:
    try:
        name = await _call(device_owners.merge, keep, fold)
    except LookupError:
        return await _section(section, page, GONE)
    except ToolError as exc:
        return await _device(keep, section, page, f"⚠️ {exc}")
    return await _device(keep, section, page, f"✅ Объединено в «{name}»")


async def _delete_confirm(device_id: int, section: str, page: int) -> Card:
    view = await _call(_device_view, device_id)
    if view is None:
        return await _section(section, page, GONE)
    if view["online"] or view["is_pi"]:
        return await _device(device_id, section, page, f"⚠️ {STILL_ONLINE}")
    lines = [f"Удалить «{view['name']}»? В сети: {view['seen']}."]
    if view["has_owner"]:
        lines.append(f"Если вернётся, появится как новое, без владельца ({view['owner']}).")
    if view["blocked"]:
        lines.append("Сейчас оно заблокировано, после удаления блокировка пропадёт.")
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [_button("🗑 Да, удалить", f"dmxy:{device_id}:{section}:{page}")],
            [_button("← Назад", f"dmd:{device_id}:{section}:{page}")],
        ]
    )
    return "\n".join(lines), markup


async def _delete(device_id: int, section: str, page: int) -> Card:
    view = await _call(_device_view, device_id)
    if view is None:
        return await _section(section, page, GONE)
    if view["online"] or view["is_pi"]:
        return await _device(device_id, section, page, f"⚠️ {STILL_ONLINE}")
    name = await _call(device_owners.delete, device_id)
    return await _section(section, page, f"🗑 «{name}» удалено")


def _purge_list(db: Any) -> list[tuple[int, str]]:
    return [(d.id, d.name) for d in device_owners.purgeable(db)]


async def _purge_confirm() -> Card:
    devices = await _call(_purge_list)
    if not devices:
        return await _section(device_owners.STALE, 0, "Удалять нечего.")
    names = [f"• {name}" for _, name in devices[:PURGE_LIST_LIMIT]]
    if len(devices) > PURGE_LIST_LIMIT:
        names.append(f"… и ещё {len(devices) - PURGE_LIST_LIMIT}")
    text = f"Удалить устройств без владельца: {len(devices)}?\n\n" + "\n".join(names)
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [_button("🗑 Да, удалить все", "dmzy")],
            [_button("← Назад", f"dms:{device_owners.STALE}:0")],
        ]
    )
    return text, markup


def _purge_now(db: Any) -> int:
    """Re-read the list at the moment of the tap: a device may have come back since."""
    return device_owners.delete_many(db, [d.id for d in device_owners.purgeable(db)])


async def _purge() -> Card:
    count = await _call(_purge_now)
    return await _section(device_owners.STALE, 0, f"🗑 Удалено: {count}")


async def _block(device_id: int, on: bool, section: str, page: int) -> Card:
    try:
        result = await _call(device_owners.set_blocked, device_id, on)
    except LookupError:
        return await _section(section, page, GONE)
    except ToolError as exc:
        return await _device(device_id, section, page, f"⚠️ {exc}")
    return await _device(device_id, section, page, f"✅ {result}")


async def _speed_pick(device_id: int, section: str, page: int) -> Card:
    view = await _call(_device_view, device_id)
    if view is None:
        return await _section(section, page, GONE)
    buttons = [
        _button(
            limit.split("/")[0].replace("M", " Мбит/с"), f"dmvs:{device_id}:{i}:{section}:{page}"
        )
        for i, limit in enumerate(SPEED_PRESETS)
    ]
    rows = [buttons[:2], buttons[2:]]
    rows.append([_button("Без ограничения", f"dmvs:{device_id}:-1:{section}:{page}")])
    rows.append([_button("← Назад", f"dmd:{device_id}:{section}:{page}")])
    now = view["speed"] or "без ограничения"
    return f"Скорость «{view['name']}» сейчас: {now}. Какую поставить?", InlineKeyboardMarkup(
        inline_keyboard=rows
    )


async def _speed(device_id: int, preset: int, section: str, page: int) -> Card:
    limit = SPEED_PRESETS[preset] if 0 <= preset < len(SPEED_PRESETS) else ""
    try:
        result = await _call(device_owners.set_speed, device_id, limit)
    except LookupError:
        return await _section(section, page, GONE)
    except ToolError as exc:
        return await _device(device_id, section, page, f"⚠️ {exc}")
    return await _device(device_id, section, page, f"✅ {result}")


def waiting_for_name(message: Message) -> bool:
    """True when this sender was asked for a device name and is now typing it."""
    if message.from_user is None:
        return False
    key = (message.chat.id, message.from_user.id)
    pending = _renames.get(key)
    if pending is None:
        return False
    text = message.text or ""
    if pending[3] < time.monotonic() or not text or text.startswith("/") or text in MENU_TEXTS:
        _renames.pop(key, None)  # another button or command means the parent moved on
        return False
    return True


async def _ask_name(cb: CallbackQuery, device_id: int, section: str, page: int) -> Card:
    view = await _call(_device_view, device_id)
    if view is None:
        return await _section(section, page, GONE)
    if isinstance(cb.message, Message):
        deadline = time.monotonic() + RENAME_SECONDS
        _renames[(cb.message.chat.id, cb.from_user.id)] = (device_id, section, page, deadline)
    text = f"Как назвать «{view['name']}»? Напишите новое имя одним сообщением."
    cancel = _button("← Отмена", f"dmd:{device_id}:{section}:{page}")
    return text, InlineKeyboardMarkup(inline_keyboard=[[cancel]])


async def on_name(message: Message) -> None:
    if message.from_user is None:
        return
    pending = _renames.pop((message.chat.id, message.from_user.id), None)
    if pending is None:
        return
    device_id, section, page, _ = pending
    try:
        name = await _call(device_owners.rename, device_id, message.text or "")
    except LookupError:
        text, markup = await _section(section, page, GONE)
    except ToolError as exc:
        text, markup = await _device(device_id, section, page, f"⚠️ {exc}")
    else:
        text, markup = await _device(device_id, section, page, f"✅ Теперь это «{name}»")
    await message.answer(text, reply_markup=markup)


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
    if isinstance(cb.message, Message):  # "← Отмена" while renaming lands here
        _renames.pop((cb.message.chat.id, cb.from_user.id), None)
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


async def cb_rename(cb: CallbackQuery) -> None:
    device_id, section, page = _parts(cb)
    await _show(cb, await _ask_name(cb, int(device_id), section, int(page)))


async def cb_merge_pick(cb: CallbackQuery) -> None:
    device_id, section, page = _parts(cb)
    await _show(cb, await _merge_pick(int(device_id), section, int(page)))


async def cb_merge_confirm(cb: CallbackQuery) -> None:
    keep, fold, section, page = _parts(cb)
    await _show(cb, await _merge_confirm(int(keep), int(fold), section, int(page)))


async def cb_merge(cb: CallbackQuery) -> None:
    keep, fold, section, page = _parts(cb)
    await _show(cb, await _merge(int(keep), int(fold), section, int(page)))


async def cb_block(cb: CallbackQuery) -> None:
    device_id, on, section, page = _parts(cb)
    await _show(cb, await _block(int(device_id), on == "1", section, int(page)))


async def cb_delete_confirm(cb: CallbackQuery) -> None:
    device_id, section, page = _parts(cb)
    await _show(cb, await _delete_confirm(int(device_id), section, int(page)))


async def cb_delete(cb: CallbackQuery) -> None:
    device_id, section, page = _parts(cb)
    await _show(cb, await _delete(int(device_id), section, int(page)))


async def cb_speed_pick(cb: CallbackQuery) -> None:
    device_id, section, page = _parts(cb)
    await _show(cb, await _speed_pick(int(device_id), section, int(page)))


async def cb_speed(cb: CallbackQuery) -> None:
    device_id, preset, section, page = _parts(cb)
    await _show(cb, await _speed(int(device_id), int(preset), section, int(page)))


async def cb_purge_confirm(cb: CallbackQuery) -> None:
    await _show(cb, await _purge_confirm())


async def cb_purge(cb: CallbackQuery) -> None:
    await _show(cb, await _purge())


def register(router: Router) -> None:
    """Attach to the router that carries the access middleware, never to a separate one.

    It must run before any catch-all text handler, so a typed name reaches on_name."""
    router.message.register(btn_devices, F.text == BTN_DEVICES)
    router.message.register(on_name, waiting_for_name)
    router.callback_query.register(cb_overview, F.data == "dm")
    router.callback_query.register(cb_section, F.data.startswith("dms:"))
    router.callback_query.register(cb_device, F.data.startswith("dmd:"))
    router.callback_query.register(cb_people, F.data.startswith("dmp:"))
    router.callback_query.register(cb_choose, F.data.startswith("dmc:"))
    router.callback_query.register(cb_apply, F.data.startswith("dmy:"))
    router.callback_query.register(cb_rename, F.data.startswith("dmr:"))
    router.callback_query.register(cb_merge_pick, F.data.startswith("dmm:"))
    router.callback_query.register(cb_merge_confirm, F.data.startswith("dmg:"))
    router.callback_query.register(cb_merge, F.data.startswith("dmgy:"))
    router.callback_query.register(cb_block, F.data.startswith("dmb:"))
    router.callback_query.register(cb_delete_confirm, F.data.startswith("dmx:"))
    router.callback_query.register(cb_delete, F.data.startswith("dmxy:"))
    router.callback_query.register(cb_speed_pick, F.data.startswith("dmv:"))
    router.callback_query.register(cb_speed, F.data.startswith("dmvs:"))
    router.callback_query.register(cb_purge_confirm, F.data == "dmz")
    router.callback_query.register(cb_purge, F.data == "dmzy")
