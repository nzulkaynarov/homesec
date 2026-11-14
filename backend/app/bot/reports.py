"""Reports for the family: time left on quotas with quick +15/+30 buttons, the AI spend and the
daily digest."""

import asyncio
import json
from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from .. import db as dbmod
from ..ai import client as ai_client
from ..config import settings
from ..models import Device, Person, kv_get
from . import common, texts


async def btn_quota(message: Message) -> None:
    await cmd_quota(message)


def _digest_sync() -> str:
    from ..ai import analyst

    s = dbmod.session()
    try:
        return analyst.daily_digest(s)
    finally:
        s.close()


async def cmd_digest(message: Message) -> None:
    user_id = message.from_user.id if message.from_user else 0
    role, _ = await asyncio.to_thread(common.get_user_role_sync, user_id)
    if role == "kid":
        await message.answer("Эта функция доступна только родителям 🛡")
        return
    await common.send_long(message, await asyncio.to_thread(_digest_sync))


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


async def cmd_quota(message: Message) -> None:
    items = await asyncio.to_thread(_collect_quota_sync)
    kb = quota_buttons_keyboard(items)
    await message.answer(texts.format_quota(items), reply_markup=kb)


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


def _spend_by_person_sync() -> tuple[list[tuple[str, float, int, dict]], float]:
    s = dbmod.session()
    try:
        names = {f"person:{p.id}": p.name for p in s.scalars(select(Person))}
        names["system"] = "Система (алерты, дайджест)"
        ledger = ai_client.ledger_for_day(s)
        rows = sorted(
            (
                (names.get(k, "Владелец"), v["usd"], v["requests"], v["features"])
                for k, v in ledger.items()
            ),
            key=lambda row: row[1],
            reverse=True,
        )
        now = datetime.now()
        week_usd = sum(
            entry["usd"]
            for i in range(7)
            for entry in ai_client.ledger_for_day(s, now - timedelta(days=i)).values()
        )
        return rows, week_usd
    finally:
        s.close()


async def cmd_usage(message: Message) -> None:
    user_id = message.from_user.id if message.from_user else 0
    role, _ = await asyncio.to_thread(common.get_user_role_sync, user_id)
    if role == "kid":
        await message.answer("Эта функция доступна только родителям 🛡")
        return
    today, week_tokens, left, gw = await asyncio.to_thread(_collect_usage_sync)
    rows, week_usd = await asyncio.to_thread(_spend_by_person_sync)
    await message.answer(
        texts.format_usage(today, week_tokens, left, gw)
        + "\n\n"
        + texts.format_spend_by_person(rows, week_usd)
    )


def register(router: Router) -> None:
    """Attach to the router that carries the access middleware, before catch-all text handlers."""
    router.message.register(btn_quota, F.text == "⏳ Моё время")
    router.message.register(btn_quota, F.text == "⏳ Квоты")
    router.message.register(cmd_digest, Command("digest"))
    router.message.register(cmd_quota, Command("quota"))
    router.message.register(cmd_usage, Command("usage"))
