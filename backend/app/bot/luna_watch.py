"""Dead man's switch for Luna AI Assistant: its heartbeat stops, the family is told.

Telegram lets a bot message only people who started it, and the family talks to
Luna rather than to this bot, so the notice is sent with Luna's own token."""

import asyncio
import json
import logging
from datetime import datetime, timedelta

from aiogram import Bot
from sqlalchemy.orm import Session

from .. import db as dbmod
from ..config import settings
from ..models import kv_get, kv_set

log = logging.getLogger("homesec.bot.luna_watch")

HEARTBEAT_KEY = "luna_last_heartbeat"
MEMBERS_KEY = "luna_member_chat_ids"
ALERTED_KEY = "luna_down_alerted"
HEARTBEAT_TIMEOUT = timedelta(minutes=3)
CHECK_INTERVAL_SECONDS = 30
DOWN_TEXT = "⚠️ Luna AI Assistant временно не работает. Как только вернётся, я сообщу."
UP_TEXT = "✅ Luna AI Assistant снова работает."


def evaluate(db: Session, now: datetime) -> str | None:
    """Return "down" or "up" on a state change; the state survives restarts."""
    raw = kv_get(db, HEARTBEAT_KEY, "")
    if not raw:
        return None
    down = now - datetime.fromisoformat(raw) > HEARTBEAT_TIMEOUT
    alerted = kv_get(db, ALERTED_KEY, "") == "1"
    if down and not alerted:
        kv_set(db, ALERTED_KEY, "1")
        return "down"
    if not down and alerted:
        kv_set(db, ALERTED_KEY, "")
        return "up"
    return None


def member_chat_ids(db: Session) -> list[int]:
    return list(json.loads(kv_get(db, MEMBERS_KEY, "[]")))


def _check_sync() -> tuple[str | None, list[int]]:
    session = dbmod.session()
    try:
        return evaluate(session, datetime.now()), member_chat_ids(session)
    finally:
        session.close()


async def watch_loop() -> None:
    if not settings.luna_telegram_bot_token:
        log.info("Luna token is not set; outage notices are off")
        return
    bot = Bot(token=settings.luna_telegram_bot_token)
    while True:
        try:
            event, chat_ids = await asyncio.to_thread(_check_sync)
            if event is not None:
                log.warning("Luna state changed: %s", event)
                for chat_id in chat_ids:
                    try:
                        await bot.send_message(chat_id, DOWN_TEXT if event == "down" else UP_TEXT)
                    except Exception:
                        log.exception("could not notify a Luna member")
        except Exception:
            log.exception("luna watch loop failed")
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)
