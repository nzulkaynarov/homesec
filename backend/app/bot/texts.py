"""Форматирование сообщений бота — чистые функции, покрыты тестами."""

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
    "Для любителей команд: /note текст, /pause устройство минуты, "
    "/bonus устройство минуты, /block устройство."
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


def format_registration(event_message: str) -> str:
    return f"📨 {event_message}\n\nКому назначить устройство?"


def format_new_device(dev: dict) -> str:
    warning = ""
    if dev.get("random_mac"):
        warning = (
            "\n⚠️ Случайный MAC («приватный адрес»). Для домашней сети "
            "его лучше выключить на устройстве, иначе правила будут слетать."
        )
    return (
        "🆕 Новое устройство в сети\n"
        f"{dev['name']}\n"
        f"MAC: {dev['mac']}\nIP: {dev['ip'] or '—'}{warning}\n\n"
        "Кому оно принадлежит?"
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


def escape_markdown(text: str) -> str:
    """Escape user or model text for Telegram legacy Markdown."""
    for char in ("\\", "_", "*", "`", "["):
        text = text.replace(char, "\\" + char)
    return text


FEATURE_LABELS = {
    "orchestrator": "ассистент",
    "tutor": "репетитор",
    "quiz": "викторина",
    "digest": "дайджест",
    "watchdog": "алерты",
    "stt": "голос",
}
