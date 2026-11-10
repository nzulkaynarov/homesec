"""«📋 Правила»: bedtime, daily limits and pauses per person, and the list of people.

Everything here works with buttons and without the AI, so it keeps working when
the Luna PC is off."""

import asyncio
import time
from typing import Any

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .. import db as dbmod
from ..services import house_rules
from ..services.quota import QUOTA_CATEGORY_LABELS
from . import access

BTN_RULES = "📋 Правила"
GONE = "Этого человека уже нет в списке."
INPUT_SECONDS = 300
BEDTIME_STARTS = ("19:30", "20:00", "20:30", "21:00", "21:30", "22:00", "22:30", "23:00", "23:30")
WAKE_TIMES = ("06:00", "06:30", "07:00", "07:30", "08:00", "08:30", "09:00", "10:00")
LIMIT_PRESETS = (30, 60, 90, 120, 180)
LIMIT_ICONS = {"games": "🎮", "video": "📺", "social": "💬"}
MENU_TEXTS = access.ADULT_BUTTONS | {"❓ Помощь"}
Card = tuple[str, InlineKeyboardMarkup]

# (chat, user) -> (what, argument, deadline); what is "new" (argument = role) or "name".
_typing: dict[tuple[int, int], tuple[str, str, float]] = {}


def _button(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text[:60], callback_data=data)


def _markup(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _in_session(work: Any, *args: Any) -> Any:
    session = dbmod.session()
    try:
        return work(session, *args)
    finally:
        session.close()


async def _call(work: Any, *args: Any) -> Any:
    return await asyncio.to_thread(_in_session, work, *args)


def _hhmm(value: str) -> str:
    return f"{value[:2]}:{value[2:]}"


def _overview_data(db: Any) -> tuple[list[tuple[int, str, list[str]]], Any]:
    kids = [
        (p.id, p.name, house_rules.summary(db, p))
        for p in house_rules.people(db)
        if p.role == "kid"
    ]
    return kids, house_rules.kids_paused_until(db)


async def _overview(notice: str = "") -> Card:
    kids, group_until = await _call(_overview_data)
    blocks = [f"🎒 {name}\n" + "\n".join(lines) for _, name, lines in kids]
    body = "\n\n".join(blocks) or "Детей в списке нет. Добавьте их в «👥 Люди»."
    if group_until is not None:
        body = f"⏸ Сейчас пауза всем детям до {group_until:%H:%M}\n\n{body}"
    text = (f"{notice}\n\n" if notice else "") + f"📋 Правила для детей\n\n{body}"
    rows = [[_button(f"🎒 {name}", f"hrp:{pid}")] for pid, name, _ in kids]
    if group_until is not None:
        rows.append([_button("▶️ Снять паузу всем детям", "hrra")])
    rows.append([_button("⏸ Пауза всем детям до утра", "hrza")])
    rows.append([_button("👥 Люди", "hrl")])
    return text, _markup(rows)


def _person_data(db: Any, person_id: int) -> tuple[dict[str, Any], list[str], bool]:
    view = house_rules.person_view(db, person_id)
    person = db.get(house_rules.Person, person_id)
    lines = house_rules.summary(db, person)
    return view, lines, house_rules.paused_until(db, person_id) is not None


async def _person(person_id: int, notice: str = "") -> Card:
    try:
        view, lines, paused = await _call(_person_data, person_id)
    except LookupError:
        return await _overview(GONE)
    text = (f"{notice}\n\n" if notice else "") + f"🎒 {view['name']}\n" + "\n".join(lines)
    text += "\n\nЛимиты общие на все устройства человека."
    pid = person_id
    rows = [
        [
            _button(f"🌙 Отбой {house_rules.NIGHTS['school'][1]}", f"hrb:{pid}:school"),
            _button(f"🌙 Отбой {house_rules.NIGHTS['weekend'][1]}", f"hrb:{pid}:weekend"),
        ],
        [
            _button(f"{LIMIT_ICONS[c]} {QUOTA_CATEGORY_LABELS[c].split()[0]}", f"hrq:{pid}:{c}")
            for c in house_rules.LIMIT_CATEGORIES
        ],
        [
            _button("⏸ 30 мин", f"hrz:{pid}:30"),
            _button("⏸ 1 ч", f"hrz:{pid}:60"),
            _button("⏸ До утра", f"hrz:{pid}:m"),
        ],
    ]
    if paused:
        rows.append([_button("▶️ Снять паузу", f"hrr:{pid}")])
    rows.append([_button("← Все дети", "hr")])
    return text, _markup(rows)


async def _bedtime_start(person_id: int, night: str) -> Card:
    _, short, meaning = house_rules.NIGHTS[night]
    buttons = [_button(t, f"hrbs:{person_id}:{night}:{t.replace(':', '')}") for t in BEDTIME_STARTS]
    rows = [buttons[i : i + 3] for i in range(0, len(buttons), 3)]
    rows.append([_button("🚫 Без отбоя", f"hrbx:{person_id}:{night}")])
    rows.append([_button("← Назад", f"hrp:{person_id}")])
    text = f"Отбой в ночи {short} ({meaning}). Во сколько выключать интернет?"
    return text, _markup(rows)


async def _bedtime_end(person_id: int, night: str, start: str) -> Card:
    buttons = [
        _button(t, f"hrbe:{person_id}:{night}:{start}:{t.replace(':', '')}") for t in WAKE_TIMES
    ]
    rows = [buttons[i : i + 4] for i in range(0, len(buttons), 4)]
    rows.append([_button("← Назад", f"hrb:{person_id}:{night}")])
    return f"Выключать в {_hhmm(start)}. Во сколько включать утром?", _markup(rows)


async def _limit_pick(person_id: int, category: str) -> Card:
    buttons = [
        _button(house_rules._minutes(m), f"hrqs:{person_id}:{category}:{m}") for m in LIMIT_PRESETS
    ]
    rows = [buttons[:3], buttons[3:], [_button("🚫 Без лимита", f"hrqs:{person_id}:{category}:0")]]
    rows.append([_button("← Назад", f"hrp:{person_id}")])
    label = QUOTA_CATEGORY_LABELS[category]
    text = (
        f"{label}: сколько в день на все устройства вместе?\n\n"
        "Время считается по обращениям к этим сервисам, поэтому оно приблизительное."
    )
    return text, _markup(rows)


async def _apply(person_id: int, work: Any, *args: Any) -> Card:
    try:
        result = await _call(work, person_id, *args)
    except LookupError:
        return await _overview(GONE)
    except house_rules.RuleError as exc:
        return await _person(person_id, f"⚠️ {exc}")
    return await _person(person_id, f"✅ {result}")


def _pause_all_kids(db: Any) -> int:
    kids = [p for p in house_rules.people(db) if p.role == "kid"]
    for kid in kids:
        house_rules.pause(db, kid.id, None)
    return len(kids)


# ---------- people ----------


def _people_data(db: Any) -> list[dict[str, Any]]:
    return [house_rules.person_view(db, p.id) for p in house_rules.people(db)]


async def _people(notice: str = "") -> Card:
    everyone = await _call(_people_data)
    lines = [
        f"{'🎒' if v['role'] == 'kid' else '👤'} {v['name']} · устройств: {v['devices']}"
        + (" · Telegram ✅" if v["linked"] else "")
        for v in everyone
    ]
    text = (f"{notice}\n\n" if notice else "") + "👥 Люди\n\n" + "\n".join(lines)
    text += (
        "\n\nЧтобы человек пользовался ботом, пусть напишет ему /start. "
        "Владельцу придёт запрос, где можно выбрать этого человека из списка."
    )
    rows = [[_button(v["name"], f"hrlp:{v['id']}")] for v in everyone]
    rows.append([_button("➕ Ребёнок", "hrla:kid"), _button("➕ Взрослый", "hrla:adult")])
    rows.append([_button("← Правила", "hr")])
    return text, _markup(rows)


async def _person_card(person_id: int, notice: str = "") -> Card:
    try:
        view = await _call(house_rules.person_view, person_id)
    except LookupError:
        return await _people(GONE)
    lines = [
        view["name"],
        f"Роль: {view['role_label']}" + (" (владелец сети)" if view["owner"] else ""),
        f"Устройств: {view['devices']}",
        "Telegram: " + ("привязан" if view["linked"] else "не привязан"),
    ]
    text = (f"{notice}\n\n" if notice else "") + "\n".join(lines)
    other = "adult" if view["role"] == "kid" else "kid"
    rows = [[_button("✏️ Переименовать", f"hrln:{person_id}")]]
    if not view["owner"]:
        rows.append([_button(f"Сделать: {house_rules.ROLES[other]}", f"hrlr:{person_id}:{other}")])
        rows.append([_button("🗑 Удалить", f"hrld:{person_id}")])
    rows.append([_button("← Люди", "hrl")])
    return text, _markup(rows)


async def _delete_confirm(person_id: int) -> Card:
    try:
        view = await _call(house_rules.person_view, person_id)
    except LookupError:
        return await _people(GONE)
    text = (
        f"Удалить «{view['name']}»? Его правила и лимиты удалятся, "
        f"устройства ({view['devices']}) останутся без владельца."
    )
    if view["linked"]:
        text += " Доступ к боту у этого человека пропадёт."
    rows = [
        [_button("🗑 Да, удалить", f"hrldy:{person_id}")],
        [_button("← Назад", f"hrlp:{person_id}")],
    ]
    return text, _markup(rows)


async def _person_change(person_id: int, work: Any, *args: Any) -> Card:
    try:
        result = await _call(work, person_id, *args)
    except LookupError:
        return await _people(GONE)
    except house_rules.RuleError as exc:
        return await _person_card(person_id, f"⚠️ {exc}")
    return await _person_card(person_id, f"✅ {result}")


def waiting_for_text(message: Message) -> bool:
    """True when this sender was asked to type a name here and is doing it now."""
    if message.from_user is None:
        return False
    key = (message.chat.id, message.from_user.id)
    pending = _typing.get(key)
    if pending is None:
        return False
    text = message.text or ""
    if pending[2] < time.monotonic() or not text or text.startswith("/") or text in MENU_TEXTS:
        _typing.pop(key, None)
        return False
    return True


def _ask(cb: CallbackQuery, what: str, argument: str) -> None:
    if isinstance(cb.message, Message):
        deadline = time.monotonic() + INPUT_SECONDS
        _typing[(cb.message.chat.id, cb.from_user.id)] = (what, argument, deadline)


async def on_text(message: Message) -> None:
    if message.from_user is None:
        return
    pending = _typing.pop((message.chat.id, message.from_user.id), None)
    if pending is None:
        return
    what, argument, _ = pending
    text = message.text or ""
    if what == "new":
        try:
            name = await _call(house_rules.add_person, text, argument)
        except house_rules.RuleError as exc:
            card = await _people(f"⚠️ {exc}")
        else:
            card = await _people(f"✅ Добавлен: {name}")
    else:
        card = await _person_change(int(argument), house_rules.rename_person, text)
    await message.answer(card[0], reply_markup=card[1])


# ---------- handlers ----------


async def _show(cb: CallbackQuery, card: Card) -> None:
    await cb.answer()
    if isinstance(cb.message, Message):
        await cb.message.edit_text(card[0], reply_markup=card[1])


def _parts(cb: CallbackQuery) -> list[str]:
    return (cb.data or "").split(":")[1:]


async def btn_rules(message: Message) -> None:
    text, markup = await _overview()
    await message.answer(text, reply_markup=markup)


async def cb_overview(cb: CallbackQuery) -> None:
    if isinstance(cb.message, Message):
        _typing.pop((cb.message.chat.id, cb.from_user.id), None)
    await _show(cb, await _overview())


async def cb_person(cb: CallbackQuery) -> None:
    await _show(cb, await _person(int(_parts(cb)[0])))


async def cb_bedtime(cb: CallbackQuery) -> None:
    pid, night = _parts(cb)
    await _show(cb, await _bedtime_start(int(pid), night))


async def cb_bedtime_start(cb: CallbackQuery) -> None:
    pid, night, start = _parts(cb)
    await _show(cb, await _bedtime_end(int(pid), night, start))


async def cb_bedtime_save(cb: CallbackQuery) -> None:
    pid, night, start, end = _parts(cb)
    card = await _apply(int(pid), house_rules.set_bedtime, night, _hhmm(start), _hhmm(end))
    await _show(cb, card)


async def cb_bedtime_off(cb: CallbackQuery) -> None:
    pid, night = _parts(cb)
    await _show(cb, await _apply(int(pid), house_rules.clear_bedtime, night))


async def cb_limit(cb: CallbackQuery) -> None:
    pid, category = _parts(cb)
    await _show(cb, await _limit_pick(int(pid), category))


async def cb_limit_save(cb: CallbackQuery) -> None:
    pid, category, minutes = _parts(cb)
    await _show(cb, await _apply(int(pid), house_rules.set_limit, category, int(minutes)))


async def cb_pause(cb: CallbackQuery) -> None:
    pid, minutes = _parts(cb)
    await _show(
        cb, await _apply(int(pid), house_rules.pause, None if minutes == "m" else int(minutes))
    )


async def cb_resume(cb: CallbackQuery) -> None:
    await _show(cb, await _apply(int(_parts(cb)[0]), house_rules.resume))


async def cb_pause_all(cb: CallbackQuery) -> None:
    count = await _call(_pause_all_kids)
    await _show(cb, await _overview(f"⏸ Пауза до утра: детей {count}"))


async def cb_resume_kids(cb: CallbackQuery) -> None:
    count = await _call(house_rules.resume_kids)
    await _show(cb, await _overview(f"▶️ Снято пауз: {count}" if count else "Пауз не было."))


async def cb_people(cb: CallbackQuery) -> None:
    if isinstance(cb.message, Message):
        _typing.pop((cb.message.chat.id, cb.from_user.id), None)
    await _show(cb, await _people())


async def cb_person_card(cb: CallbackQuery) -> None:
    if isinstance(cb.message, Message):
        _typing.pop((cb.message.chat.id, cb.from_user.id), None)
    await _show(cb, await _person_card(int(_parts(cb)[0])))


async def cb_add(cb: CallbackQuery) -> None:
    role = _parts(cb)[0]
    _ask(cb, "new", role)
    label = house_rules.ROLES.get(role, "человек").lower()
    text = f"Как зовут? Напишите имя одним сообщением (роль: {label})."
    await _show(cb, (text, _markup([[_button("← Отмена", "hrl")]])))


async def cb_rename(cb: CallbackQuery) -> None:
    pid = _parts(cb)[0]
    _ask(cb, "name", pid)
    text = "Напишите новое имя одним сообщением."
    await _show(cb, (text, _markup([[_button("← Отмена", f"hrlp:{pid}")]])))


async def cb_role(cb: CallbackQuery) -> None:
    pid, role = _parts(cb)
    await _show(cb, await _person_change(int(pid), house_rules.set_role, role))


async def cb_delete_confirm(cb: CallbackQuery) -> None:
    await _show(cb, await _delete_confirm(int(_parts(cb)[0])))


async def cb_delete(cb: CallbackQuery) -> None:
    pid = int(_parts(cb)[0])
    try:
        name = await _call(house_rules.delete_person, pid)
    except LookupError:
        card = await _people(GONE)
    except house_rules.RuleError as exc:
        card = await _person_card(pid, f"⚠️ {exc}")
    else:
        card = await _people(f"🗑 Удалён: {name}")
    await _show(cb, card)


def register(router: Router) -> None:
    """Attach to the router that carries the access middleware, before any catch-all text."""
    router.message.register(btn_rules, F.text == BTN_RULES)
    router.message.register(on_text, waiting_for_text)
    exact = {"hr": cb_overview, "hrza": cb_pause_all, "hrra": cb_resume_kids, "hrl": cb_people}
    for data, handler in exact.items():
        router.callback_query.register(handler, F.data == data)
    prefixed = {
        "hrp:": cb_person,
        "hrb:": cb_bedtime,
        "hrbs:": cb_bedtime_start,
        "hrbe:": cb_bedtime_save,
        "hrbx:": cb_bedtime_off,
        "hrq:": cb_limit,
        "hrqs:": cb_limit_save,
        "hrz:": cb_pause,
        "hrr:": cb_resume,
        "hrlp:": cb_person_card,
        "hrla:": cb_add,
        "hrln:": cb_rename,
        "hrlr:": cb_role,
        "hrld:": cb_delete_confirm,
        "hrldy:": cb_delete,
    }
    for prefix, handler in prefixed.items():
        router.callback_query.register(handler, F.data.startswith(prefix))
