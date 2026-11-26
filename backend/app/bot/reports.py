"""Reports for the family: time left on quotas with quick +15/+30 buttons and the AI spend (shown
under «🖥 Сервер»). The evening summary lives in bot/digest.py."""

import asyncio
from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from .. import db as dbmod
from ..ai import client as ai_client
from ..config import settings
from ..models import Device, Person
from ..services import time_grants
from . import charts_menu, rich, screens, texts


async def btn_quota(message: Message) -> None:
    await cmd_quota(message)


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
                "owner_id": dev.person_id,
                "rows": rows,
            })
        return out
    finally:
        s.close()


GRANT = "gt"  # gt:<person id>:<minutes>: more time for a child, the same rule everywhere
LIMITS_ONLY = "l"  # gt:<person id>:<minutes>:l under a limit alert: the minutes go to the limits


def grant_button(
    person_id: int, minutes: int, label: str = "", limits_only: bool = False
) -> InlineKeyboardButton:
    text = f"➕ {minutes} мин" + (f" · {label}" if label else "")
    data = f"{GRANT}:{person_id}:{minutes}" + (f":{LIMITS_ONLY}" if limits_only else "")
    return InlineKeyboardButton(text=text[:40], callback_data=data, style=texts.SUCCESS)


def quota_buttons_keyboard(items: list[dict]) -> InlineKeyboardMarkup | None:
    """«+15» and «+30» for each child in the report, and the week's chart."""
    if not items:
        return None
    children: dict[int, str] = {}
    for item in items:
        if item.get("owner_id") is not None:
            children.setdefault(item["owner_id"], item["owner"])
    rows = [
        [grant_button(pid, 15, name), grant_button(pid, 30, name)]
        for pid, name in children.items()
    ]
    rows.append([charts_menu.button(charts_menu.SCREEN)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def cmd_quota(message: Message) -> None:
    items = await asyncio.to_thread(_collect_quota_sync)
    await rich.answer(message, screens.quota_html(items), quota_buttons_keyboard(items))


async def redraw_quota(message: Message) -> None:
    items = await asyncio.to_thread(_collect_quota_sync)
    await rich.edit(message, screens.quota_html(items), quota_buttons_keyboard(items))


def _grant_sync(person_id: int, minutes: int, limits_only: bool) -> tuple[str, bool]:
    """(what to tell the parent, whether time was given)."""
    s = dbmod.session()
    try:
        person = s.get(Person, person_id)
        name = person.name if person else "Ребёнок"
        # «➕» in the report is the same "yes" as under a request, and answers a waiting one.
        give = time_grants.grant_limits if limits_only else time_grants.give
        try:
            return f"✅ {name}: {give(s, person_id, minutes, 'Добавлено родителем').text}.", True
        except time_grants.GrantError as exc:
            return f"⚠️ {exc}", False
    finally:
        s.close()


async def cb_grant(cb: CallbackQuery) -> None:
    """A parent gives a child time from the quota report or from a "time is up" alert."""
    parts = (cb.data or "").split(":")
    person_id, minutes = int(parts[1]), int(parts[2])
    limits_only = parts[3:] == [LIMITS_ONLY]
    result, given = await asyncio.to_thread(_grant_sync, person_id, minutes, limits_only)
    if not isinstance(cb.message, Message):
        await cb.answer(result[:200], show_alert=True)
        return
    if cb.message.text is None:  # the rich quota report: fresh numbers, the buttons stay
        await cb.answer(result[:200], show_alert=True)
        await redraw_quota(cb.message)
        return
    if not given:  # nothing changed, so the buttons stay for when it can, as under a request
        await cb.answer(result[:200], show_alert=True)
        return
    await cb.answer()
    await cb.message.edit_text(
        texts.with_result(cb.message, f"➡ {result}"), parse_mode="HTML", reply_markup=None
    )


def _spend_sync() -> str:
    """The AI spend in money, per person and per feature; the owner pays, so only he sees it."""
    s = dbmod.session()
    try:
        names = {f"person:{p.id}": p.name for p in s.scalars(select(Person))}
        names["system"] = "Сам бот (предупреждения, сводка)"
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
        used_pct = None
        if settings.ai_daily_token_budget:
            today = ai_client.usage_today(s)
            spent = today.get("input", 0) + today.get("output", 0)
            used_pct = round(100 * spent / settings.ai_daily_token_budget)
        return screens.usage_html(rows, week_usd, used_pct)
    finally:
        s.close()


async def spend_screen() -> str:
    return await asyncio.to_thread(_spend_sync)


def register(router: Router) -> None:
    """Attach to the router that carries the access middleware, before catch-all text handlers."""
    router.message.register(btn_quota, F.text == "⏳ Квоты")
    router.message.register(cmd_quota, Command("quota"))
    router.callback_query.register(cb_grant, F.data.startswith(f"{GRANT}:"))
