"""Telegram rich messages (Bot API 10.1+): tables, headings and collapsible blocks.

Screens are built as Rich HTML from escaped pieces. If Telegram rejects one, the same content
goes out as plain text, so a formatting mistake never costs the family the answer.
"""

import html
import logging
import re
from collections.abc import Iterable, Sequence

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup, InputRichMessage, Message

from . import texts

log = logging.getLogger("homesec.bot.rich")

RICH_LIMIT = 32000  # Telegram allows 32768 characters in a rich message


def esc(value: object) -> str:
    """Text from people, devices or models, safe inside Rich HTML."""
    return html.escape(str(value), quote=True)


def heading(text: str, level: int = 3) -> str:
    return f"<h{level}>{esc(text)}</h{level}>"


def paragraph(text: str) -> str:
    return f"<p>{esc(text)}</p>"


def table(
    rows: Iterable[Sequence[object]],
    header: Sequence[str] | None = None,
    caption: str | None = None,
) -> str:
    """A compact striped table; every cell is escaped."""
    parts = ["<table compact striped>"]
    if caption:
        parts.append(f"<caption>{esc(caption)}</caption>")
    if header:
        parts.append("<tr>" + "".join(f"<th>{esc(h)}</th>" for h in header) + "</tr>")
    for row in rows:
        parts.append("<tr>" + "".join(f"<td>{esc(c)}</td>" for c in row) + "</tr>")
    parts.append("</table>")
    return "".join(parts)


def bullets(items: Iterable[str], ordered: bool = False) -> str:
    tag = "ol" if ordered else "ul"
    return f"<{tag}>" + "".join(f"<li>{esc(i)}</li>" for i in items) + f"</{tag}>"


def details(summary: str, body: str, is_open: bool = False) -> str:
    """A collapsible block; body is already Rich HTML."""
    attr = " open" if is_open else ""
    return f"<details{attr}><summary>{esc(summary)}</summary>{body}</details>"


_BREAKS = re.compile(r"<br\s*/?>|<hr\s*/?>|</(p|h\d|tr|li|summary|caption|blockquote|footer)>")
_CELL_END = re.compile(r"</t[dh]>")
_TAG = re.compile(r"<[^>]+>")


def to_plain(rich_html: str) -> str:
    """The same content as plain text: rows on lines, cells split by « · »."""
    text = _CELL_END.sub(" · ", rich_html)
    text = _BREAKS.sub("\n", text)
    text = _TAG.sub("", text)
    lines = [line.strip().removesuffix("·").strip() for line in html.unescape(text).split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


async def send(
    bot: Bot,
    chat_id: int,
    rich_html: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> Message | None:
    """Send as a rich message; fall back to plain text parts if Telegram refuses it."""
    if len(rich_html) <= RICH_LIMIT:
        try:
            return await bot.send_rich_message(
                chat_id=chat_id,
                rich_message=InputRichMessage(html=rich_html),
                reply_markup=reply_markup,
            )
        except TelegramBadRequest as e:
            log.warning("rich message refused, sending plain text: %s", e)
    sent = None
    parts = texts.split_message(to_plain(rich_html))
    for i, part in enumerate(parts):
        last = i == len(parts) - 1
        sent = await bot.send_message(chat_id, part, reply_markup=reply_markup if last else None)
    return sent


async def answer(
    message: Message, rich_html: str, reply_markup: InlineKeyboardMarkup | None = None
) -> Message | None:
    if message.bot is None:
        return None
    return await send(message.bot, message.chat.id, rich_html, reply_markup)


async def edit(
    message: Message, rich_html: str, reply_markup: InlineKeyboardMarkup | None = None
) -> None:
    """Redraw a screen in place; a plain message is redrawn as plain text."""
    try:
        if message.text is None:
            await message.edit_text(
                rich_message=InputRichMessage(html=rich_html), reply_markup=reply_markup
            )
            return
    except TelegramBadRequest as e:
        if "not modified" in str(e):
            return
        log.warning("rich edit refused, editing as plain text: %s", e)
    try:
        await message.edit_text(to_plain(rich_html)[:4000], reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            raise
