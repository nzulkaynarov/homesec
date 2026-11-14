"""What the HomeSec bot's menus share: the bottom keyboard per role, who someone is,
the AI rate limit and the short AI chat memory."""

import time
from collections import defaultdict, deque

from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup

from .. import db as dbmod
from ..ai import client as ai_client
from ..models import Person
from ..services import family as family_svc
from . import texts
from .device_menu import BTN_DEVICES
from .rules_menu import BTN_RULES

# Короткая память диалога с ИИ на чат (только текстовые реплики, без tool call'ов)
CHAT_HISTORY: dict[int, deque] = defaultdict(lambda: deque(maxlen=6))


def keyboard_for_role(role: str) -> ReplyKeyboardMarkup | None:
    """Нижнее меню Telegram-кнопок в зависимости от роли в семье."""
    if role in ("owner", "admin"):
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="🏠 Кто дома"), KeyboardButton(text="⏳ Квоты")],
                [KeyboardButton(text="⏸ Пауза"), KeyboardButton(text="📺 Общие устройства")],
                [KeyboardButton(text=BTN_DEVICES), KeyboardButton(text=BTN_RULES)],
                [KeyboardButton(text="📝 Заметки"), KeyboardButton(text="❓ Помощь")],
            ],
            resize_keyboard=True,
        )
    if role == "kid":
        return ReplyKeyboardMarkup(
            keyboard=[
                [
                    KeyboardButton(text="⏳ Моё время"),
                    KeyboardButton(text="🙏 Попросить время"),
                ],
                [
                    KeyboardButton(text="🧠 Заработать +15м"),
                    KeyboardButton(text="🚀 Чем заняться?"),
                ],
                [
                    KeyboardButton(text="✅ Сделал дело"),
                    KeyboardButton(text="❓ Помощь"),
                ],
            ],
            resize_keyboard=True,
        )
    if role == "guest":
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="📶 Wi-Fi"), KeyboardButton(text="❓ Помощь")],
            ],
            resize_keyboard=True,
        )
    return None


def parent_ids_sync() -> set[int]:
    s = dbmod.session()
    try:
        return family_svc.parent_chat_ids(s)
    finally:
        s.close()


def get_user_role_sync(telegram_id: int) -> tuple[str, Person | None]:
    s = dbmod.session()
    try:
        return family_svc.get_user_role(s, telegram_id)
    finally:
        s.close()


AI_RATE_LIMIT = 8  # requests per window for one person
AI_RATE_WINDOW_SECONDS = 60
ai_request_times: dict[int, deque] = defaultdict(lambda: deque(maxlen=AI_RATE_LIMIT))


def allow_ai_request(user_id: int) -> bool:
    """Sliding-window limit that keeps one person from flooding paid model calls."""
    now = time.monotonic()
    times = ai_request_times[user_id]
    if len(times) == AI_RATE_LIMIT and now - times[0] < AI_RATE_WINDOW_SECONDS:
        return False
    times.append(now)
    return True


def caller(person: Person | None, telegram_id: int, feature: str, role: str) -> ai_client.Caller:
    user = f"person:{person.id}" if person is not None else f"tg:{telegram_id}"
    return ai_client.Caller(user=user, session=f"tg:{telegram_id}", feature=feature, role=role)


async def send_long(message: Message, text: str) -> None:
    for part in texts.split_message(text):
        await message.answer(part)

