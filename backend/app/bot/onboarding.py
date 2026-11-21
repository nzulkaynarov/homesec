"""A new Telegram account asks for access: the owner picks who it is (a person already in the
list, a parent, a child or a guest) or turns it down, and the newcomer gets their menu."""

import asyncio
import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from .. import db as dbmod
from ..config import settings
from ..models import Person
from ..services import family as family_svc
from . import common, texts
from .texts import DANGER

log = logging.getLogger("homesec.bot.onboarding")

REFUSED = (
    "Доступ к боту HomeSec не открыт: он только для родителей.\n"
    "Если ты из семьи или гость, напиши Luna 🐾"
)


def access_request_keyboard(
    requester_id: int, unlinked: list[tuple[int, str]] | None = None
) -> InlineKeyboardMarkup:
    """Кнопки родителю при онбординге нового участника семьи.

    People already in the list but without Telegram come first: picking one links
    the account to that person, so their devices and rules apply to them."""
    linked = [
        [InlineKeyboardButton(text=f"Это {name}", callback_data=f"acc:{requester_id}:link:{pid}")]
        for pid, name in (unlinked or [])
    ]
    return InlineKeyboardMarkup(inline_keyboard=linked + [
        [
            InlineKeyboardButton(text="🛡 Родитель", callback_data=f"acc:{requester_id}:admin"),
            InlineKeyboardButton(
                text="⛔ Отклонить",
                callback_data=f"acc:{requester_id}:deny",
                style=DANGER,
            ),
        ],
    ])


def _unlinked_people_sync() -> list[tuple[int, str]]:
    s = dbmod.session()
    try:
        # Only adults: this bot is the parents', children and guests use Luna.
        query = (
            select(Person)
            .where(Person.telegram_id.is_(None), Person.role == "adult")
            .order_by(Person.name)
        )
        return [(p.id, p.name) for p in s.scalars(query)]
    finally:
        s.close()


def _link_telegram_sync(requester_id: int, person_id: int) -> tuple[str, str]:
    """Attach a Telegram account to a person already in the list; returns (message, role)."""
    s = dbmod.session()
    try:
        person = s.get(Person, person_id)
        if person is None:
            return "Этого человека уже нет в списке.", "deny"
        if s.scalar(select(Person).where(Person.telegram_id == requester_id)) is not None:
            return "Этот Telegram уже привязан к другому человеку.", "deny"
        if person.role != "adult":
            return "В этом боте только родители. Детям и гостям нужна Luna.", "deny"
        role = "admin"
        person.telegram_id, person.bot_role = requester_id, role
        s.commit()
        label = family_svc.ROLE_LABELS.get(role, role)
        return f"Telegram привязан к «{person.name}», роль: {label}.", role
    finally:
        s.close()


def _assign_telegram_access_sync(requester_id: int, role: str, requester_name: str) -> str:
    s = dbmod.session()
    try:
        if role != "admin":  # "deny", or a button from an old message offering child or guest
            return "Запрос доступа отклонён."
        person = s.scalar(select(Person).where(Person.telegram_id == requester_id))
        if person is None:
            person = s.scalar(select(Person).where(Person.name == requester_name))
        if person is None:
            person = Person(
                name=requester_name, role="adult", telegram_id=requester_id, bot_role=role
            )
            s.add(person)
        else:
            person.telegram_id = requester_id
            person.bot_role = role
            person.role = "adult"
        s.commit()
        role_label = family_svc.ROLE_LABELS.get(role, role)
        return f"Пользователю {requester_name} назначена роль: {role_label}."
    finally:
        s.close()


async def notify_owner_about_access_request(message: Message) -> None:
    if not message.bot:
        return
    user = message.from_user
    if not user:
        return
    # Name and @username tell people apart; the Telegram number is in the buttons, not the text.
    uname = f" ({texts.escape_markdown('@' + user.username)})" if user.username else ""
    text = (
        f"🔔 *Кто-то просит доступ к боту HomeSec*\n\n"
        f"Пользователь: {texts.escape_markdown(user.full_name)}{uname}\n\n"
        f"Этот бот только для родителей. Детям и гостям нужна Luna.\n"
        f"Открыть этому человеку управление домашней сетью?"
    )
    unlinked = await asyncio.to_thread(_unlinked_people_sync)
    for owner_id in settings.telegram_allowed_ids:
        try:
            await message.bot.send_message(
                owner_id,
                text,
                parse_mode="Markdown",
                reply_markup=access_request_keyboard(user.id, unlinked),
            )
        except Exception:
            log.exception("не удалось отправить запрос доступа владельцу %s", owner_id)


async def cb_access_request(cb: CallbackQuery) -> None:
    parts = (cb.data or "").split(":")
    requester_id, role = int(parts[1]), parts[2]
    requester_name = f"Пользователь {requester_id}"
    if cb.message and isinstance(cb.message, Message) and cb.message.text:
        for line in cb.message.text.split("\n"):
            if line.startswith("Пользователь:"):
                requester_name = line.replace("Пользователь:", "").split("(")[0].strip()
                break

    if role == "link" and len(parts) > 3:
        msg, role = await asyncio.to_thread(_link_telegram_sync, requester_id, int(parts[3]))
    else:
        msg = await asyncio.to_thread(
            _assign_telegram_access_sync, requester_id, role, requester_name
        )
    await cb.answer()
    if isinstance(cb.message, Message):
        await cb.message.edit_text(f"{cb.message.text}\n\n➡ {msg}", reply_markup=None)

    if not cb.bot:
        return
    if role == "admin":
        try:
            await cb.bot.send_message(
                requester_id,
                f"🎉 Доступ открыт.\n\n{texts.start_text(role)}",
                reply_markup=common.keyboard_for_role(role),
            )
        except Exception:
            log.exception(
                "не удалось уведомить пользователя %s о предоставлении доступа", requester_id
            )
    else:
        try:  # a refusal is said too: silence would leave the person waiting for nothing
            await cb.bot.send_message(requester_id, REFUSED)
        except Exception:
            log.exception("could not tell %s about the refusal", requester_id)


def register(router: Router) -> None:
    """Attach to the router that carries the access middleware."""
    router.callback_query.register(cb_access_request, F.data.startswith("acc:"))
