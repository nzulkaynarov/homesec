"""Форматирование сообщений бота — чистые функции, покрыты тестами."""

import html

from aiogram.types import Message

from ..services import plain

# The "/" menu (set_my_commands at start-up): every feature is a button, so the menu only brings
# the buttons back and explains them. The other commands still work for those used to them.
BOT_COMMANDS: list[tuple[str, str]] = [
    ("start", "Показать кнопки меню"),
    ("help", "Что делает каждая кнопка"),
]

# What each button of the parents' keyboard does, one line each, in keyboard order.
PARENT_BUTTONS = (
    "🏠 Кто дома — кто из семьи сейчас в сети и в порядке ли сама сеть",
    "⏳ Квоты — сколько экранного времени осталось детям, и добавить минут",
    "⏸ Пауза — отключить интернет ребёнку, его устройству или телевизору на время",
    "📺 Общие устройства — телевизор и приставки: выключить, детский режим или для взрослых",
    "📱 Устройства — чьё устройство, имя, блокировка, скорость, удаление",
    "📋 Правила — отбой, дневные лимиты и список людей",
    "📝 Заметки — семейная память: размеры, коды, всё важное; добавить или удалить",
)
SERVER_BUTTON = "🖥 Сервер — компьютер и Raspberry Pi, расходы на ИИ"
HELP_BUTTON = "❓ Помощь — эта подсказка"

FREE_TEXT = (
    "Можно просто написать или наговорить голосовое: «отключи Мише интернет на час», "
    "«запомни: код домофона 4589». Любое действие я попрошу подтвердить кнопкой."
)
ALERTS = (
    "Сам я пишу, когда появилось новое устройство, у ребёнка кончается время или он просит "
    "ещё, в сети что-то странное или сломалось; в 21:00 присылаю сводку за день."
)
OWNER_ALERTS = "Владельцу — ещё о перегреве, нехватке места, старых копиях и выключении компьютера."
COMMANDS = (
    "Для любителей команд: /note текст, /pause ребёнок или устройство минуты, "
    "/bonus ребёнок минуты, /block устройство."
)
GREETING = "Здравствуйте! Я — бот HomeSec 🛡, слежу за домашней сетью."


def _buttons(role: str) -> str:
    lines = list(PARENT_BUTTONS)
    if role == "owner":
        lines.append(SERVER_BUTTON)
    return "\n".join([*lines, HELP_BUTTON])


def start_text(role: str) -> str:
    """The greeting: each button in one line, and that words and voice work too."""
    return f"{GREETING}\n\n{_buttons(role)}\n\n{FREE_TEXT}"


def help_text(role: str) -> str:
    """The buttons, what the bot says by itself, and the commands only on the last line."""
    alerts = ALERTS + (f"\n{OWNER_ALERTS}" if role == "owner" else "")
    parts = ("Всё делается кнопками внизу:", _buttons(role), FREE_TEXT, alerts, COMMANDS)
    return "\n\n".join(parts)


START_UNAUTHORIZED = (
    "👋 Привет! Я — семейный ассистент HomeSec.\n\n"
    "Я отправил запрос главе семьи на предоставление доступа. "
    "Как только доступ будет подтверждён, я пришлю уведомление и открою меню!"
)


def hhmm(minutes: int) -> str:
    """Минуты -> «Ч:ММ»: 80 -> 1:20."""
    return f"{minutes // 60}:{minutes % 60:02d}"


CHANGING_ADDRESS = (
    "⚠️ Это устройство меняет свой адрес в сети (на iPhone это «Частный адрес Wi-Fi», "
    "на Android — «случайный адрес»). Чтобы правила для него не слетали, выключите это "
    "в настройках домашней сети Wi-Fi на самом устройстве."
)


def _what_it_is(dev: dict, vendor: str | None) -> list[str]:
    """What a parent recognises a device by: its name, what it calls itself, who made it. The
    MAC and IP stay on the device card in «📱 Устройства» for those who need them."""
    name = plain.device_name(dev.get("name") or "", dev.get("hostname") or "")
    lines = [f"«{name}»" if name != plain.UNNAMED else "Без имени"]
    hostname = (dev.get("hostname") or "").strip()
    if hostname and hostname != name and not plain.is_address(hostname):
        lines.append(f"Называет себя: {hostname}")
    if vendor:
        lines.append(f"Производитель: {vendor}")
    if dev.get("random_mac"):
        lines.append(CHANGING_ADDRESS)
    return lines


def format_new_device(dev: dict, vendor: str | None = None) -> str:
    lines = ["🆕 В сети новое устройство", *_what_it_is(dev, vendor)]
    return "\n".join(lines) + "\n\nКому оно принадлежит?"


def format_registration(dev: dict, vendor: str | None = None) -> str:
    """Someone on an unknown device introduced themselves on the welcome page."""
    lines = ["📨 Просят доступ в интернет"]
    if dev.get("request_name"):
        lines.append(f"Представился: {dev['request_name']}")
    if dev.get("request_comment"):
        lines.append(f"Пишет: «{dev['request_comment']}»")
    lines += ["", "Устройство:", *_what_it_is(dev, vendor)]
    return "\n".join(lines) + "\n\nКому назначить устройство?"


def format_maybe_same(dev: dict) -> str:
    """A new record whose network name matches a known device: a phone that changed its address."""
    new = plain.device_name(dev.get("name") or "", dev.get("hostname") or "")
    old = dev.get("twin_name") or plain.UNNAMED
    return (
        f"🔁 Похоже, новое устройство «{new}» — это снова «{old}»: они называют себя "
        "одинаково. Так бывает, когда телефон меняет свой адрес в сети.\n\n"
        f"Объединить их в одно? Имя, владелец и правила останутся от «{old}»."
    )


def progress_bar(used: int, limit: int, length: int = 8) -> str:
    if limit <= 0:
        return "[────────]"
    pct = min(1.0, max(0.0, used / limit))
    filled = int(round(pct * length))
    return f"[{'█' * filled}{'░' * (length - filled)}]"


# Button colours (Bot API 9.4): green agrees or gives time, red refuses, blocks or deletes.
SUCCESS = "success"
DANGER = "danger"

TELEGRAM_MESSAGE_LIMIT = 4000  # Telegram rejects messages above 4096 characters


def split_message(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    """Split long text for Telegram, preferring line breaks."""
    parts: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = rest.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        parts.append(rest[:cut])
        rest = rest[cut:].lstrip("\n")
    if rest or not parts:
        parts.append(rest)
    return parts


def alert_html(text: str) -> str:
    """A notice as Telegram HTML: its first line, who and what, in bold, so it is found at a glance
    among the others; the rest stays as written."""
    head, sep, rest = text.partition("\n")
    return f"<b>{html.escape(head, quote=False)}</b>{sep}{html.escape(rest, quote=False)}"


def with_result(message: Message, result: str) -> str:
    """A notice that was answered: its own text, bold first line kept, and the outcome under it."""
    return f"{message.html_text}\n\n{html.escape(result, quote=False)}"


def escape_markdown(text: str) -> str:
    """Escape user or model text for Telegram legacy Markdown."""
    for char in ("\\", "_", "*", "`", "["):
        text = text.replace(char, "\\" + char)
    return text


FEATURE_LABELS = {
    "orchestrator": "ассистент",
    "tutor": "репетитор",
    "quiz": "викторина",
    "digest": "вечерняя сводка",
    "watchdog": "предупреждения",
    "stt": "голос",
}
OTHER_FEATURE = "другое"  # a spend recorded without a known feature
