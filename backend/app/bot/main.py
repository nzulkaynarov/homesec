"""Точка входа бота: `python -m app.bot`. Отдельный от панели процесс
(systemd-юнит homesec-bot) — переживает рестарты панели и умеет сообщить,
если панель упала. С базой работает через WAL (см. app/db.py).

Исходящее long-polling-соединение к Telegram обходит NAT — это и есть
канал удалённого доступа к дому."""

import asyncio
import logging
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F

from .. import db as dbmod
from ..ai import analyst, watchdog
from ..config import settings
from ..db import Base, engine
from ..migrations import run_migrations
from . import handlers, notify, texts
from .health import default_monitor

log = logging.getLogger("homesec.bot")

NOTIFY_INTERVAL = 10  # опрос журнала на новые устройства, сек
HEALTH_INTERVAL = 60  # health-проверки, сек
WATCHDOG_INTERVAL = 15 * 60  # эвристики аномалий, сек
DIGEST_HOUR = 21  # ежедневный дайджест в 21:00 локального времени


async def _broadcast(bot: Bot, chat_ids: set[int], text: str, keyboard=None) -> None:
    for chat_id in chat_ids:
        try:
            await bot.send_message(chat_id, text, reply_markup=keyboard)
        except Exception:
            log.exception("не удалось отправить сообщение в чат %s", chat_id)


def _collect_notifications() -> list[tuple[str, dict, str, list[tuple[int, str, str]]]]:
    """(kind, устройство, текст события, люди для кнопок) по каждому событию."""
    s = dbmod.session()
    try:
        items = notify.collect_notifications(s)
        if not items:
            return []
        from ..models import Person

        people = [(p.id, p.name, p.role) for p in s.query(Person).order_by(Person.name)]
        return [
            (n.kind,
             {"id": n.device.id, "name": n.device.name,
              "mac": n.device.mac, "ip": n.device.ip},
             n.message, people)
            for n in items
        ]
    finally:
        s.close()


async def notify_loop(bot: Bot, chat_ids: set[int]) -> None:
    while True:
        try:
            for kind, dev, message, people in await asyncio.to_thread(_collect_notifications):
                kb = handlers.new_device_keyboard(dev["id"], people)
                if kind == "register_request":
                    text = texts.format_registration(message)
                else:
                    text = texts.format_new_device(dev)
                await _broadcast(bot, chat_ids, text, kb)
        except Exception:
            log.exception("notify_loop")
        await asyncio.sleep(NOTIFY_INTERVAL)


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
    if not settings.telegram_bot_token:
        log.info("HS_TELEGRAM_BOT_TOKEN не задан — бот выключен, выходим")
        return
    allowed = settings.telegram_allowed_ids
    if not allowed:
        log.warning("HS_TELEGRAM_CHAT_IDS пуст — бот некому отвечать, выходим")
        return

    Base.metadata.create_all(engine)
    run_migrations(engine)

    bot = Bot(token=settings.telegram_bot_token)
    dp = Dispatcher()
    # Доступ только из разрешённых чатов; остальные игнорируются молча
    handlers.router.message.filter(F.chat.id.in_(allowed))
    handlers.router.callback_query.filter(F.message.chat.id.in_(allowed))
    dp.include_router(handlers.router)

    tasks = [
        asyncio.create_task(notify_loop(bot, allowed)),
        asyncio.create_task(health_loop(bot, allowed)),
        asyncio.create_task(watchdog_loop(bot, allowed)),
        asyncio.create_task(digest_loop(bot, allowed)),
    ]
    log.info("бот запущен, чатов в allowlist: %d", len(allowed))
    try:
        await dp.start_polling(bot)
    finally:
        for t in tasks:
            t.cancel()
