"""One typed answer at a time: the waits of every screen that asks a parent to type something.

A device's new name, a pause length, a person's name and a note each wait in a table made here.
A person has one wait at most: starting a prompt ends the others, and a menu button, a command, a
message without text or another inline button ends them all before its own handler runs,
whatever order the screens were registered in. So the next free text never renames a device or
adds a person on a screen the parent has already left."""

from typing import Any

from aiogram import Router
from aiogram.types import CallbackQuery, Message

from . import access

MENU_TEXTS = access.ADULT_BUTTONS | {"❓ Помощь"}
Key = tuple[int, int]  # (chat, user)

_tables: list[dict[Key, Any]] = []


def table() -> dict[Key, Any]:
    """A screen's own waits: (chat, user) -> whatever the screen needs to finish the answer."""
    waits: dict[Key, Any] = {}
    _tables.append(waits)
    return waits


def forget(key: Key) -> None:
    """End every wait of this person in this chat."""
    for waits in _tables:
        waits.pop(key, None)


def start(waits: dict[Key, Any], key: Key, value: Any) -> None:
    """Wait for a typed answer on this screen; any other wait of the person ends."""
    forget(key)
    waits[key] = value


def leaves(text: str | None) -> bool:
    """A menu button, a command or a message without text: not an answer to a prompt."""
    return not text or text.startswith("/") or text in MENU_TEXTS


def message_moved_on(message: Message) -> bool:
    """Ends the waits on a menu button or a command; never takes the message itself."""
    if message.from_user is not None and leaves(message.text):
        forget((message.chat.id, message.from_user.id))
    return False


def press_moved_on(cb: CallbackQuery) -> bool:
    """Ends the waits on any inline button; the button that starts a prompt starts its own
    wait again in its handler. Never takes the press itself."""
    if isinstance(cb.message, Message):
        forget((cb.message.chat.id, cb.from_user.id))
    return False


async def _never_message(message: Message) -> None:  # message_moved_on never lets one reach it
    return None


async def _never_press(cb: CallbackQuery) -> None:  # press_moved_on never lets one reach it
    await cb.answer()


def register(router: Router) -> None:
    """Attach first to the router that carries the access middleware, before every screen."""
    router.message.register(_never_message, message_moved_on)
    router.callback_query.register(_never_press, press_moved_on)
