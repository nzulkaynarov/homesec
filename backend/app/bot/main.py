"""Точка входа бота: `python -m app.bot`. Отдельный от панели процесс
(systemd-юнит homesec-bot) — переживает рестарты панели и умеет сообщить,
если панель упала. С базой работает через WAL (см. app/db.py).

Исходящее long-polling-соединение к Telegram обходит NAT — это и есть
канал удалённого доступа к дому."""

import asyncio
import logging
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import BotCommand, InlineKeyboardMarkup, ReplyKeyboardRemove
from sqlalchemy.orm import Session

from .. import db as dbmod
from ..ai import watchdog
from ..config import settings
from ..db import Base, engine
from ..migrations import ensure_schema
from ..services import family as family_svc
from ..services import followups, night_watch, plain, time_notices, uptime
from . import access, alerts, digest, handlers, luna_watch, notify, server_menu, texts
from .health import default_monitor
from .triage import vendor_of

log = logging.getLogger("homesec.bot")

NOTIFY_INTERVAL = 10  # опрос журнала на новые устройства, сек
HEALTH_INTERVAL = 60  # health-проверки, сек
WATCHDOG_INTERVAL = 15 * 60  # эвристики аномалий, сек
DIGEST_HOUR = 21  # ежедневный дайджест в 21:00 локального времени
FOLLOWUP_INTERVAL = 5 * 60  # напоминания о заявках и недельная сводка, сек
UPTIME_INTERVAL = 60  # watched devices dropping off the network, seconds


def _parent_ids() -> set[int]:
    s = dbmod.session()
    try:
        return family_svc.parent_chat_ids(s)
    finally:
        s.close()


async def _broadcast(bot: Bot, chat_ids: set[int], text: str, keyboard=None) -> int:
    """Send to the configured chats and to every parent linked in the bot since; returns how many
    chats got the whole message."""
    parts = texts.split_message(texts.alert_html(text))
    delivered = 0
    for chat_id in chat_ids | await asyncio.to_thread(_parent_ids):
        try:
            for part in parts[:-1]:
                await bot.send_message(chat_id, part, parse_mode="HTML")
            await bot.send_message(chat_id, parts[-1], parse_mode="HTML", reply_markup=keyboard)
            delivered += 1
        except Exception:
            log.exception("не удалось отправить сообщение в чат %s", chat_id)
    return delivered


PendingNotice = tuple[
    str, dict, str, int | None, list, int | None, int, time_notices.Notice | None
]


def _parent_notice(
    s: Session, n: notify.Notification, now: datetime
) -> time_notices.Notice | None:
    """The text parents read about a child's time, rendered from the request and the device:
    the journal line keeps the MAC and the request number for the linkage, not for people."""
    from ..models import BonusRequest

    if n.kind == "bonus_request" and n.request_id is not None:
        req = s.get(BonusRequest, n.request_id)
        return time_notices.request_notice(s, req, now) if req is not None else None
    if n.kind in ("quota_warn", "quota_block"):
        return time_notices.quota_notice(s, n.device, n.kind, n.category, now)
    return None


def _registration_requests(
    s: Session, items: list[notify.Notification]
) -> dict[int, tuple[str, str]]:
    """{device id: (the name, the comment)} of the latest welcome-page request per device: the
    journal line carries the device's addresses, the parent reads who asked and why."""
    from ..models import RegistrationRequest

    ids = {n.device.id for n in items if n.kind == "register_request"}
    if not ids:
        return {}
    rows = s.query(RegistrationRequest).filter(RegistrationRequest.device_id.in_(ids))
    found: dict[int, tuple[str, str]] = {}
    for row in rows.order_by(RegistrationRequest.id):
        found[row.device_id] = (row.name, row.comment or "")
    return found


async def _vendor(dev: dict) -> str | None:
    """Who made it, a hint for recognising a new device; a changing address tells nothing."""
    return None if dev["random_mac"] else await vendor_of(dev["mac"])


def _collect_notifications() -> tuple[int, list[PendingNotice]]:
    """(latest journal id, [(kind, устройство, текст события, id оригинала, люди, id заявки,
    id события, текст для родителей)]). The cursor does not move here: only after delivery."""
    s = dbmod.session()
    try:
        latest = notify.latest_event_id(s)
        items = notify.collect_notifications(s, advance=False, until=latest)
        if not items:
            return latest, []
        from ..models import Person

        people = [(p.id, p.name, p.role) for p in s.query(Person).order_by(Person.name)]
        requests = _registration_requests(s, items)
        now = datetime.now()
        pending: list[PendingNotice] = []
        switched_off: set[int] = set()
        for n in items:
            if n.kind == "quota_block" and n.device.person_id is not None:
                # One child's devices go off one by one; the parent needs to hear it once.
                if n.device.person_id in switched_off:
                    continue
                switched_off.add(n.device.person_id)
            request = requests.get(n.device.id) if n.kind == "register_request" else None
            twin = n.extra_device
            pending.append((
                n.kind,
                {"id": n.device.id, "name": n.device.name, "hostname": n.device.hostname,
                 "mac": n.device.mac, "random_mac": n.device.mac_is_random,
                 "person_id": n.device.person_id,
                 "twin_name": plain.device_name(twin.name, twin.hostname) if twin else None,
                 "request_name": request[0] if request else "",
                 "request_comment": request[1] if request else ""},
                n.message,
                n.extra_device.id if n.extra_device else None,
                people,
                n.request_id,
                n.event_id,
                _parent_notice(s, n, now),
            ))
        return latest, pending
    finally:
        s.close()


async def notify_loop(bot: Bot, chat_ids: set[int]) -> None:
    while True:
        try:
            latest, items = await asyncio.to_thread(_collect_notifications)
            done_until = latest
            for kind, dev, _message, extra_id, people, request_id, event_id, parent in items:
                kb: InlineKeyboardMarkup | None
                if kind == "device_maybe_same" and extra_id is not None:
                    text = texts.format_maybe_same(dev)
                    kb = alerts.merge_keyboard(dev["id"], extra_id)
                elif kind == "register_request":
                    text = texts.format_registration(dev, await _vendor(dev))
                    kb = alerts.new_device_keyboard(dev["id"], people)
                elif kind in ("quota_block", "quota_warn") and parent is not None:
                    text = parent.text
                    kb = alerts.bonus_keyboard(dev["person_id"], parent.hold)
                elif kind == "bonus_request" and request_id is not None and parent is not None:
                    text = parent.text
                    kb = alerts.request_keyboard(request_id, parent)
                elif kind in ("quota_block", "quota_warn", "bonus_request"):
                    continue  # the request is gone: nothing to tell or answer
                else:
                    text = texts.format_new_device(dev, await _vendor(dev))
                    kb = alerts.new_device_keyboard(dev["id"], people)
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


def _followups() -> tuple[list[tuple[int, time_notices.Notice]], str | None]:
    """Reminders for waiting time requests, expired ones closed, and the weekly summary when due."""
    s = dbmod.session()
    try:
        reminders = [(req.id, notice) for req, notice in followups.due_reminders(s)]
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
            for request_id, notice in reminders:
                keyboard = alerts.request_keyboard(request_id, notice)
                await _broadcast(bot, chat_ids, notice.text, keyboard)
            if weekly:
                await _broadcast(bot, chat_ids, weekly)
        except Exception:
            log.exception("followup_loop")
        await asyncio.sleep(FOLLOWUP_INTERVAL)


def _uptime_changes() -> list[uptime.Change]:
    s = dbmod.session()
    try:
        return uptime.changes(s)
    finally:
        s.close()


def _uptime_record(change: uptime.Change) -> None:
    s = dbmod.session()
    try:
        uptime.record(s, change)
    finally:
        s.close()


NIGHT_WATCH_INTERVAL = 300


def _awake() -> list[night_watch.Awake]:
    s = dbmod.session()
    try:
        return night_watch.awake_devices(s)
    finally:
        s.close()


def _awake_told(awake: night_watch.Awake) -> None:
    s = dbmod.session()
    try:
        night_watch.remember(s, awake)
    finally:
        s.close()


async def night_watch_loop(bot: Bot, chat_ids: set[int]) -> None:
    """A child's device in use during bedtime, once a night; remembered only once delivered."""
    while True:
        try:
            for awake in await asyncio.to_thread(_awake):
                if await _broadcast(bot, chat_ids, awake.text):
                    await asyncio.to_thread(_awake_told, awake)
        except Exception:
            log.exception("night_watch_loop")
        await asyncio.sleep(NIGHT_WATCH_INTERVAL)


async def uptime_loop(bot: Bot, chat_ids: set[int]) -> None:
    """Network equipment and marked devices: a message when one goes silent and when it is
    back. Remembered only once delivered, so an outage of Telegram does not swallow it."""
    while True:
        try:
            for change in await asyncio.to_thread(_uptime_changes):
                if await _broadcast(bot, chat_ids, change.text):
                    await asyncio.to_thread(_uptime_record, change)
        except Exception:
            log.exception("uptime_loop")
        await asyncio.sleep(UPTIME_INTERVAL)


async def digest_loop(bot: Bot, chat_ids: set[int]) -> None:
    while True:
        now = datetime.now()
        target = now.replace(hour=DIGEST_HOUR, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        try:
            await digest.send(bot, chat_ids | await asyncio.to_thread(_parent_ids))
        except Exception:
            log.exception("digest_loop")


MOVED_TOLD_KEY = "moved_to_luna:"  # per person: the one-time "your place is Luna" was delivered


def _children_to_tell() -> list[tuple[int, int]]:
    from ..models import Person, kv_get

    s = dbmod.session()
    try:
        linked = s.query(Person).filter(
            Person.telegram_id.isnot(None), Person.bot_role.in_(access.NOT_SERVED)
        )
        return [
            (p.id, int(p.telegram_id or 0))
            for p in linked
            if not kv_get(s, f"{MOVED_TOLD_KEY}{p.id}", "")
        ]
    finally:
        s.close()


def _child_told(person_id: int) -> None:
    from ..models import kv_set

    s = dbmod.session()
    try:
        kv_set(s, f"{MOVED_TOLD_KEY}{person_id}", "1")
    finally:
        s.close()


async def _tell_children_they_moved(bot: Bot) -> None:
    """Children and guests who used this bot hear once that their menu now lives in Luna."""
    for person_id, chat_id in await asyncio.to_thread(_children_to_tell):
        try:
            await bot.send_message(chat_id, access.MOVED, reply_markup=ReplyKeyboardRemove())
        except (TelegramBadRequest, TelegramForbiddenError):
            # Never opened this bot, or blocked it: there is no old menu to take away.
            log.info("person %s has no chat with this bot, nothing to tell", person_id)
        except Exception:
            log.warning("could not tell person %s about the move to Luna", person_id, exc_info=True)
            continue
        await asyncio.to_thread(_child_told, person_id)


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
        # The "/" menu: every feature is a button, so it only brings the buttons back and the help
        await bot.set_my_commands(
            [BotCommand(command=c, description=d) for c, d in texts.BOT_COMMANDS]
        )
    except Exception:
        log.warning("не удалось зарегистрировать меню команд", exc_info=True)
    await _tell_children_they_moved(bot)
    dp = Dispatcher()
    # Ролевой доступ и онбординг новых членов семьи контролируются внутри handlers.router
    dp.include_router(handlers.router)

    tasks = [
        asyncio.create_task(notify_loop(bot, allowed)),
        asyncio.create_task(health_loop(bot, allowed)),
        asyncio.create_task(watchdog_loop(bot, allowed)),
        asyncio.create_task(digest_loop(bot, allowed)),
        asyncio.create_task(followup_loop(bot, allowed)),
        asyncio.create_task(uptime_loop(bot, allowed)),
        asyncio.create_task(night_watch_loop(bot, allowed)),
        asyncio.create_task(luna_watch.watch_loop()),
        asyncio.create_task(server_menu.watch_loop(bot, allowed)),
    ]
    log.info("бот запущен, чатов в allowlist: %d", len(allowed))
    try:
        await dp.start_polling(bot)
    finally:
        for t in tasks:
            t.cancel()
