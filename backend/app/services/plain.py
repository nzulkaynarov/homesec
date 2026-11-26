"""Plain words for parents: what a device is called without its network address, a speed limit
in megabits, and journal lines without the machine fields the bot links events by.

The journal keeps MAC addresses, IP addresses and request numbers on purpose (bot/notify.py
finds devices and requests by them), so they are dropped only where people read the line.
"""

import re

_MAC = r"[0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5}"
_IP = r"\d{1,3}(?:\.\d{1,3}){3}"
_ADDRESS = re.compile(rf"(?:{_MAC}|{_IP})")
# "(AA:BB:CC:DD:EE:FF, 192.168.88.7)", "(192.168.88.7)" or "(AA:…, —)" after a name
_ADDRESS_GROUP = re.compile(rf"\s*\(\s*(?:{_MAC}|{_IP}|—)(?:\s*,\s*(?:{_MAC}|{_IP}|—))*\s*\)")
# An address standing for a name: "Новое устройство: AA:BB:…" or "«192.168.88.7»"
_NAMED_ADDRESS = re.compile(rf"(?<=: )(?:{_MAC}|{_IP})\b|(?<=«)(?:{_MAC}|{_IP})(?=»)")
_BARE_ADDRESS = re.compile(rf"\b(?:{_MAC}|{_IP})\b")
_REQUEST = re.compile(r"\s*\[req#\d+\]")
_SPACES = re.compile(r"[ \t]{2,}")
_SPACE_BEFORE_MARK = re.compile(r"[ \t]+([,.;:])")
_WORDS = (("случайный MAC", "меняет свой адрес в сети"),)

UNNAMED = "устройство без имени"

_SPEED_UNITS = {"": "бит/с", "K": "кбит/с", "M": "Мбит/с", "G": "Гбит/с"}
_SPEED_PART = re.compile(r"^(\d+(?:\.\d+)?)([KMG]?)$", re.IGNORECASE)


def is_address(text: str) -> bool:
    return bool(_ADDRESS.fullmatch(text.strip()))


def device_name(name: str, hostname: str = "") -> str:
    """The device's name or, without one, what it calls itself; never a bare address."""
    for candidate in (name, hostname):
        text = (candidate or "").strip()
        if text and not is_address(text):
            return text
    return UNNAMED


def journal_line(text: str) -> str:
    """A journal line as parents read it: no addresses and no request numbers."""
    text = _REQUEST.sub("", text)
    text = _ADDRESS_GROUP.sub("", text)
    text = _NAMED_ADDRESS.sub(UNNAMED, text)
    text = _BARE_ADDRESS.sub("", text)
    for technical, plain in _WORDS:
        text = text.replace(technical, plain)
    text = _SPACE_BEFORE_MARK.sub(r"\1", _SPACES.sub(" ", text))
    return text.strip()


def _speed_part(raw: str) -> str | None:
    found = _SPEED_PART.match(raw.strip())
    if found is None:
        return None
    number, unit = found.groups()
    return f"{number.replace('.', ',')} {_SPEED_UNITS[unit.upper()]}"


def speed_words(limit: str) -> str:
    """'5M/20M' (upload/download, as the router takes it) as 'до 20 Мбит/с'; equal halves are
    one number, and an unreadable limit is described without its raw form."""
    if not limit.strip():
        return "без ограничения"
    parts = limit.split("/")
    if len(parts) != 2:
        return "с ограничением"
    upload, download = (_speed_part(part) for part in parts)
    if upload is None or download is None:
        return "с ограничением"
    if upload == download:
        return f"до {download}"
    return f"до {download} на загрузку и до {upload} на отдачу"


CENT = 0.01


def money(usd: float) -> str:
    """Dollars as people read them: "$1.25", and below a dollar in cents ("0,12 ¢"), since the
    local models leave most days at a fraction of a cent and "$0.00" told nothing."""
    if usd >= 1:
        return f"${usd:.2f}"
    cents = usd / CENT
    if cents >= 10:
        return f"{cents:.0f} ¢"
    shown = f"{cents:.1f}".removesuffix(".0") if cents >= 1 else f"{cents:.2f}"
    return f"{shown.replace('.', ',')} ¢"
