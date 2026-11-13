"""Точка входа бота: `python -m app.bot`. Отдельный от панели процесс
(systemd-юнит homesec-bot) — переживает рестарты панели и умеет сообщить,
если панель упала. С базой работает через WAL (см. app/db.py).

Исходящее long-polling-соединение к Telegram обходит NAT — это и есть
канал удалённого доступа к дому."""

import asyncio
import logging
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand

from .. import db as dbmod
from ..ai import analyst, watchdog
from ..config import settings
from ..db import Base, engine
from ..migrations import ensure_schema
from ..services import family as family_svc
from ..services import followups
from . import handlers, luna_watch, notify, texts
from .health import default_monitor

log = logging.getLogger("homesec.bot")

NOTIFY_INTERVAL = 10  # опрос журнала на новые устройства, сек
HEALTH_INTERVAL = 60  # health-проверки, сек
WATCHDOG_INTERVAL = 15 * 60  # эвристики аномалий, сек
DIGEST_HOUR = 21  # ежедневный дайджест в 21:00 локального времени
FOLLOWUP_INTERVAL = 5 * 60  # напоминания о заявках и недельная сводка, сек


def _parent_ids() -> set[int]:
    s = dbmod.session()
    try:
        return family_svc.parent_chat_ids(s)
    finally:
        s.close()


async def _broadcast(bot: Bot, chat_ids: set[int], text: str, keyboard=None) -> int:
    """Send to the configured chats and to every parent linked in the bot since; returns how many
    chats got the whole message."""
    parts = texts.split_message(text)
    delivered = 0
    for chat_id in chat_ids | await asyncio.to_thread(_parent_ids):
        try:
            for part in parts[:-1]:
                await bot.send_message(chat_id, part)
            await bot.send_message(chat_id, parts[-1], reply_markup=keyboard)
            delivered += 1
        except Exception:
            log.exception("не удалось отправить сообщение в чат %s", chat_id)
    return delivered


PendingNotice = tuple[str, dict, str, int | None, list, int | None, int]


def _collect_notifications() -> tuple[int, list[PendingNotice]]:
    """(latest journal id, [(kind, устройство, текст события, id оригинала, люди, id заявки,
    id события)]). The cursor does not move here: only after delivery."""
    s = dbmod.session()
    try:
        latest = notify.latest_event_id(s)
        items = notify.collect_notifications(s, advance=False, until=latest)
        if not items:
            return latest, []
        from ..models import Person

        people = [(p.id, p.name, p.role) for p in s.query(Person).order_by(Person.name)]
        return latest, [
            (n.kind,
             {"id": n.device.id, "name": n.device.name, "mac": n.device.mac,
              "ip": n.device.ip, "random_mac": n.device.mac_is_random},
             n.message,
             n.extra_device.id if n.extra_device else None,
             people,
             n.request_id,
             n.event_id)
            for n in items
        ]
    finally:
        s.close()


def _request_category(request_id: int) -> str:
    from ..services import bonus

    s = dbmod.session()
    try:
        return bonus.category_of(s, request_id)
    finally:
        s.close()


async def notify_loop(bot: Bot, chat_ids: set[int]) -> None:
    while True:
        try:
            latest, items = await asyncio.to_thread(_collect_notifications)
            done_until = latest
            for kind, dev, message, extra_id, people, request_id, event_id in items:
                if kind == "device_maybe_same" and extra_id is not None:
                    text = f"🔁 {message}"
                    kb = handlers.merge_keyboard(dev["id"], extra_id)
                elif kind == "register_request":
                    text = texts.format_registration(message)
                    kb = handlers.new_device_keyboard(dev["id"], people)
                elif kind == "quota_block":
                    text = f"⏳ {message}\nМожно добавить время кнопкой:"
                    kb = handlers.bonus_keyboard(dev["id"])
                elif kind == "quota_warn":
                    text = f"⚠️ {message}\nМожно добавить время заранее:"
                    kb = handlers.bonus_keyboard(dev["id"])
                elif kind == "bonus_request" and request_id is not None:
                    text = f"🙏 {message}"
                    category = await asyncio.to_thread(_request_category, request_id)
                    kb = handlers.bonus_request_keyboard(request_id, category)
                else:
                    text = texts.format_new_device(dev)
                    kb = handlers.new_device_keyboard(dev["id"], people)
                if not await _broadcast(bot, chat_ids, text, kb):
                    # Nobody got it (no internet, Telegram down): keep it for the next round.
                    done_until = event_id - 1
                    break
            await asyncio.to_thread(_mark_sent, done_until)
        except Exception:
            log.exception("notify_loop")
        await asyncio.sleep(NOTIFY_INTERVAL)


def _mark_sent(event_id: int) -> None:
    s = dbmod.session()
    try:
        notify.mark_sent(s, event_id)
    finally:
        s.close()


async def health_loop(bot: Bot, chat_ids: set[int]) -> None:
    monitor = default_monitor()
    while True:
        try:
            for message in await asyncio.to_thread(monitor.tick):
                await _broadcast(bot, chat_ids, message)
        except Exception:
            log.exception("health_loop")
        await asyncio.sleep(HEALTH_INTERVAL)


def _watchdog_check() -> list[str]:
    s = dbmod.session()
    try:
        return watchdog.check(s)
    finally:
        s.close()


async def watchdog_loop(bot: Bot, chat_ids: set[int]) -> None:
    while True:
        try:
            for alert in await asyncio.to_thread(_watchdog_check):
                await _broadcast(bot, chat_ids, alert)
        except Exception:
            log.exception("watchdog_loop")
        await asyncio.sleep(WATCHDOG_INTERVAL)


def _followups() -> tuple[list[tuple[int, str, str]], str | None]:
    """Reminders for waiting time requests, expired ones closed, and the weekly summary when due."""
    s = dbmod.session()
    try:
        reminders = [(req.id, req.category, text) for req, text in followups.due_reminders(s)]
        followups.expire_stale(s)
        weekly = None
        if followups.weekly_due(s):
            weekly = followups.weekly_summary(s)
            followups.mark_weekly_sent(s, datetime.now().date())
        return reminders, weekly
    finally:
        s.close()


async def followup_loop(bot: Bot, chat_ids: set[int]) -> None:
    while True:
        try:
            reminders, weekly = await asyncio.to_thread(_followups)
            for request_id, category, text in reminders:
                keyboard = handlers.bonus_request_keyboard(request_id, category)
                await _broadcast(bot, chat_ids, text, keyboard)
            if weekly:
                await _broadcast(bot, chat_ids, weekly)
        except Exception:
            log.exception("followup_loop")
        await asyncio.sleep(FOLLOWUP_INTERVAL)


def _build_digest() -> str:
    s = dbmod.session()
    try:
        return analyst.daily_digest(s)
    finally:
        s.close()


async def digest_loop(bot: Bot, chat_ids: set[int]) -> None:
    while True:
        now = datetime.now()
        target = now.replace(hour=DIGEST_HOUR, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        try:
            await _broadcast(bot, chat_ids, await asyncio.to_thread(_build_digest))
        except Exception:
            log.exception("digest_loop")


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per request otherwise
    if not settings.telegram_bot_token:
        log.info("HS_TELEGRAM_BOT_TOKEN не задан — бот выключен, выходим")
        return
    allowed = settings.telegram_allowed_ids
    if not allowed:
        log.warning("HS_TELEGRAM_CHAT_IDS пуст — бот некому отвечать, выходим")
        return

    ensure_schema(engine, Base.metadata)

    bot = Bot(token=settings.telegram_bot_token)
    try:
        # Меню команд по «/» — обнаруживаемость команд без чтения /help
        await bot.set_my_commands(
            [BotCommand(command=c, description=d) for c, d in texts.BOT_COMMANDS]
        )
    except Exception:
        log.warning("не удалось зарегистрировать меню команд", exc_info=True)
    dp = Dispatcher()
    # Ролевой доступ и онбординг новых членов семьи контролируются внутри handlers.router
    dp.include_router(handlers.router)

    tasks = [
        asyncio.create_task(notify_loop(bot, allowed)),
        asyncio.create_task(health_loop(bot, allowed)),
        asyncio.create_task(watchdog_loop(bot, allowed)),
        asyncio.create_task(digest_loop(bot, allowed)),
        asyncio.create_task(followup_loop(bot, allowed)),
        asyncio.create_task(luna_watch.watch_loop()),
    ]
    log.info("бот запущен, чатов в allowlist: %d", len(allowed))
    try:
        await dp.start_polling(bot)
    finally:
        for t in tasks:
            t.cancel()
