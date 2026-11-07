"""Команды и кнопки бота. Хендлеры тонкие: вся логика — в app.ai.tools (общий
реестр инструментов с панелью и ИИ), здесь только парсинг и форматирование.
Явная команда человека (/block и т.п.) — это уже подтверждение, поэтому
мутации исполняются сразу; подтверждение кнопкой нужно только действиям ИИ."""

import asyncio
import json
import logging
from collections import defaultdict, deque
from datetime import datetime, timedelta
from typing import Any

import httpx
from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from .. import db as dbmod
from ..ai import client as ai_client
from ..ai import orchestrator, tools
from ..config import settings
from ..models import GROUP_LABELS, GROUPS, Device, Person, kv_get
from . import texts

log = logging.getLogger("homesec.bot")
router = Router()

# Мутации, предложенные ИИ и ждущие кнопку, лежат в таблице pending_actions
# (tools.save_pending/pop_pending) — кнопки переживают рестарты и деплой.

# Короткая память диалога с ИИ на чат (только текстовые реплики, без tool call'ов)
CHAT_HISTORY: dict[int, deque] = defaultdict(lambda: deque(maxlen=6))


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
    await message.answer(texts.START)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(texts.HELP)


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    status = await asyncio.to_thread(run_tool_sync, "get_status", {})
    await message.answer(texts.format_status(status))


@router.message(Command("devices"))
async def cmd_devices(message: Message) -> None:
    rows = await asyncio.to_thread(run_tool_sync, "list_devices", {})
    await message.answer(texts.format_devices(rows))


def _digest_sync() -> str:
    from ..ai import analyst

    s = dbmod.session()
    try:
        return analyst.daily_digest(s)
    finally:
        s.close()


@router.message(Command("digest"))
async def cmd_digest(message: Message) -> None:
    await message.answer(await asyncio.to_thread(_digest_sync))


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
    await _device_command(message, command, "block_device")


@router.message(Command("unblock"))
async def cmd_unblock(message: Message, command: CommandObject) -> None:
    await _device_command(message, command, "unblock_device")


def pause_presets_keyboard() -> InlineKeyboardMarkup:
    """Быстрые пресеты для /pause без аргументов."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⏸ Дети 30м", callback_data="pz:kid:30"),
            InlineKeyboardButton(text="⏸ Дети 1ч", callback_data="pz:kid:60"),
        ],
        [
            InlineKeyboardButton(text="🍽 Ужин (30м)", callback_data="pz:kid:30:dinner"),
            InlineKeyboardButton(text="🌙 До утра (8ч)", callback_data="pz:kid:480"),
        ],
        [
            InlineKeyboardButton(text="👥 Гости 1ч", callback_data="pz:guest:60"),
            InlineKeyboardButton(text="🔍 Выбрать устройство", callback_data="pz:select"),
        ],
        [
            InlineKeyboardButton(text="✖ Отмена", callback_data="pick:cancel"),
        ],
    ])


@router.callback_query(F.data.startswith("pz:"))
async def cb_pause_preset(cb: CallbackQuery) -> None:
    parts = (cb.data or "").split(":")
    if len(parts) < 2:
        await cb.answer()
        return
    action = parts[1]
    if action == "select":
        await cb.answer()
        candidates = await asyncio.to_thread(_find_candidates, "")
        if isinstance(cb.message, Message):
            await cb.message.edit_text(
                "Выберите устройство для паузы на 30 мин:",
                reply_markup=device_pick_keyboard(candidates, "pause_internet", extra=":30"),
            )
        return
    target, minutes = action, int(parts[2])
    reason = "ужин" if len(parts) > 3 and parts[3] == "dinner" else ""
    args: dict[str, Any] = {"target": target, "minutes": minutes}
    if reason:
        args["reason"] = reason
    try:
        result = await asyncio.to_thread(run_tool_sync, "pause_internet", args)
    except tools.ToolError as e:
        result = f"Не получилось: {e}"
    except Exception:
        log.exception("pause_internet упал")
        result = "Ошибка при выполнении, подробности в журнале."
    await cb.answer()
    if isinstance(cb.message, Message):
        await cb.message.edit_text(f"➡ {result}", reply_markup=None)


@router.message(Command("pause"))
async def cmd_pause(message: Message, command: CommandObject) -> None:
    raw = (command.args or "").strip()
    if not raw:
        await message.answer(
            "Быстрая пауза интернета. Выберите пресет или укажите время:",
            reply_markup=pause_presets_keyboard(),
        )
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
    target = (command.args or "").strip()
    await _target_command(message, "resume_internet", target, {})


@router.message(Command("bonus"))
async def cmd_bonus(message: Message, command: CommandObject) -> None:
    from ..services.quota import QUOTA_CATEGORIES

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


def _find_merge_candidates_sync() -> list[tuple[dict, dict, str]]:
    """Находит пары (дубль, оригинал) устройств-кандидатов на объединение.
    Возвращает: [(dup_dict, target_dict, причина)]."""
    s = dbmod.session()
    try:
        devices = list(s.scalars(select(Device).order_by(Device.id)))
        candidates: list[tuple[dict, dict, str]] = []
        seen_pairs: set[tuple[int, int]] = set()

        for d1 in devices:
            for d2 in devices:
                if d1.id >= d2.id:
                    continue
                reason = ""
                h1 = (d1.hostname or "").strip().lower()
                h2 = (d2.hostname or "").strip().lower()
                n1 = (d1.name or "").strip().lower()
                n2 = (d2.name or "").strip().lower()
                if h1 and h2 and h1 == h2:
                    reason = f"совпадает hostname «{d1.hostname}»"
                elif n1 and n2 and n1 == n2:
                    reason = f"совпадает имя «{d1.name}»"
                if reason:
                    # Решаем, кто вероятнее дубль: без владельца или со случайным MAC
                    if d1.person_id is None and d2.person_id is not None:
                        dup, tgt = d1, d2
                    elif d2.person_id is None and d1.person_id is not None:
                        dup, tgt = d2, d1
                    elif d1.mac_is_random and not d2.mac_is_random:
                        dup, tgt = d1, d2
                    elif d2.mac_is_random and not d1.mac_is_random:
                        dup, tgt = d2, d1
                    else:
                        dup, tgt = (d2, d1) if d2.id > d1.id else (d1, d2)
                    pair = (dup.id, tgt.id)
                    if pair not in seen_pairs:
                        seen_pairs.add(pair)
                        candidates.append((
                            {"id": dup.id, "name": dup.name, "mac": dup.mac, "ip": dup.ip},
                            {"id": tgt.id, "name": tgt.name, "mac": tgt.mac, "ip": tgt.ip},
                            reason,
                        ))
        return candidates
    finally:
        s.close()


@router.message(Command("merge"))
async def cmd_merge(message: Message) -> None:
    candidates = await asyncio.to_thread(_find_merge_candidates_sync)
    if not candidates:
        await message.answer(
            "Подозрительных дублей не найдено: у всех устройств уникальные имена и hostnames."
        )
        return
    await message.answer(f"Найдено подозрительных дублей: {len(candidates)}")
    for dup, tgt, reason in candidates:
        text = (
            f"🔁 Кандидат на объединение ({reason}):\n"
            f"Дубль: #{dup['id']} {dup['name']} ({dup['mac']}, {dup['ip'] or '—'})\n"
            f"Оригинал: #{tgt['id']} {tgt['name']} ({tgt['mac']}, {tgt['ip'] or '—'})"
        )
        await message.answer(text, reply_markup=merge_keyboard(dup["id"], tgt["id"]))


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


@router.message(Command("usage"))
async def cmd_usage(message: Message) -> None:
    today, week_tokens, left, gw = await asyncio.to_thread(_collect_usage_sync)
    await message.answer(texts.format_usage(today, week_tokens, left, gw))


# ---------- кнопки под уведомлением о новом устройстве ----------

def new_device_keyboard(dev_id: int, people: list[tuple[int, str, str]]) -> InlineKeyboardMarkup:
    """people: (id, имя, роль). Callback-данные: nd:<dev_id>:<действие>[:<арг>]."""
    rows = [
        [InlineKeyboardButton(text=f"→ {name} ({GROUP_LABELS[role]})",
                              callback_data=f"nd:{dev_id}:assign:{pid}")]
        for pid, name, role in people
    ]
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


def bonus_request_keyboard(request_id: int) -> InlineKeyboardMarkup:
    """Кнопки под заявкой ребёнка: +15 / +30 / до конца дня / Отказать."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="➕ 15 мин", callback_data=f"br:{request_id}:15"),
            InlineKeyboardButton(text="➕ 30 мин", callback_data=f"br:{request_id}:30"),
        ],
        [
            InlineKeyboardButton(text="🌞 До конца дня", callback_data=f"br:{request_id}:day"),
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


# ---------- свободный текст -> ИИ-оркестратор ----------

def _orchestrate(chat_id: int, text: str) -> orchestrator.Answer:
    s = dbmod.session()
    try:
        return orchestrator.handle(s, text, history=list(CHAT_HISTORY[chat_id]))
    finally:
        s.close()


def confirm_keyboard(pending_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Выполнить", callback_data=f"act:{pending_id}:yes"),
        InlineKeyboardButton(text="❌ Отмена", callback_data=f"act:{pending_id}:no"),
    ]])


async def _process_user_nl_prompt(message: Message, user_text: str) -> None:
    """Обрабатывает текстовый ввод (набранный или распознанный из аудио) через ИИ."""
    if not ai_client.is_configured():
        await message.answer(
            "Свободный текст понимает ИИ, а он не настроен "
            "(HS_ANTHROPIC_API_KEY в .env). Пока доступны команды — /help."
        )
        return
    try:
        answer = await asyncio.to_thread(_orchestrate, message.chat.id, user_text)
    except ai_client.AiError as e:
        await message.answer(str(e))
        return
    except Exception:
        log.exception("оркестратор упал")
        await message.answer("ИИ споткнулся, подробности в журнале бота. Команды /help работают.")
        return

    CHAT_HISTORY[message.chat.id].append({"role": "user", "content": user_text})
    CHAT_HISTORY[message.chat.id].append({"role": "assistant", "content": answer.text})
    await message.answer(answer.text)
    for action in answer.pending:
        pending_id = await asyncio.to_thread(_save_pending, action)
        await message.answer(
            f"Подтвердите действие:\n{action.description}",
            reply_markup=confirm_keyboard(pending_id),
        )


@router.message(F.voice)
async def handle_voice(message: Message) -> None:
    """Голосовое сообщение от пользователя: скачивает ogg аудиофайл,
    транскрибирует через LunaGPT STT и передаёт в ИИ-оркестратор."""
    if not settings.stt_url:
        await message.answer(
            "Голосовое управление не настроено: укажите HS_STT_URL в .env (шлюз LunaGPT)."
        )
        return

    if not message.voice or message.bot is None:
        return

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

        async with httpx.AsyncClient(timeout=35.0) as client:
            files = {
                "file": ("voice.oga", voice_bytes, message.voice.mime_type or "audio/ogg")
            }
            data = {"language": "ru"}
            resp = await client.post(settings.stt_url, files=files, data=data)
            resp.raise_for_status()
            transcribed_text = resp.json().get("text", "").strip()
    except Exception as exc:
        log.exception("ошибка транскрипции голосового сообщения")
        await message.answer(f"Не удалось распознать голос: {exc}")
        return

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
