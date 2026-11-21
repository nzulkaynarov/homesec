"""Форматирование сообщений бота — чистые функции, покрыты тестами."""

# Меню команд Telegram: бот регистрирует его при старте (set_my_commands),
# чтобы команды подсказывались по «/» и их не надо было помнить наизусть.
BOT_COMMANDS: list[tuple[str, str]] = [
    ("status", "Состояние сети и сервисов"),
    ("who", "Радар «Кто дома» и статус ТВ"),
    ("devices", "Устройства: кто онлайн, блокировки"),
    ("block", "Заблокировать интернет устройству"),
    ("unblock", "Снять блокировку"),
    ("pause", "Пауза интернета на время"),
    ("resume", "Снять паузу досрочно"),
    ("movie", "Режим взрослых на ТВ (до утра)"),
    ("bonus", "Добавить время к квоте на сегодня"),
    ("quota", "Экранное время и остаток квот"),
    ("note", "Сохранить семейную заметку"),
    ("notes", "Семейная память и заметки"),
    ("usage", "Расход токенов ИИ-шлюза"),
    ("digest", "Дайджест за сутки прямо сейчас"),
    ("help", "Все команды и подсказки"),
]

START_PARENT = (
    "Привет! Я — бот HomeSec 🛡\n\n"
    "/status — состояние сети\n"
    "/who — радар «Кто дома»\n"
    "/devices — устройства: владелец, имя, объединение, удаление\n"
    "📋 Правила — отбой, лимиты и паузы для каждого ребёнка, список людей\n"
    "/quota — экранное время детей\n"
    "⏸ Пауза — отключить всех детей, ребёнка, его устройство или ТВ на время\n"
    "/movie — Общие устройства\n"
    "/notes — семейная память\n"
    "/help — все команды\n\n"
    "Можно писать свободным текстом или слать голосовые сообщения!"
)

START_UNAUTHORIZED = (
    "👋 Привет! Я — семейный ассистент HomeSec.\n\n"
    "Я отправил запрос главе семьи на предоставление доступа. "
    "Как только доступ будет подтверждён, я пришлю уведомление и открою меню!"
)

START = START_PARENT

HELP = (
    "Я — бот HomeSec, слежу за домашней сетью.\n\n"
    "/status — состояние сети и сервисов\n"
    "/who — радар «Кто дома» и статус ТВ\n"
    "/devices — устройства: владелец, имя, объединение, удаление\n"
    "📋 Правила — отбой, лимиты и паузы для каждого ребёнка, список людей\n"
    "⏸ Пауза — отключить всех детей, одного ребёнка, его устройство или общее "
    "устройство на 15 минут, час, до утра или своё время\n"
    "/block <имя|id> — заблокировать интернет устройству\n"
    "/unblock <имя|id> — снять блокировку\n"
    "/pause <имя|группа> [минут] — пауза интернета (без аргументов — меню «⏸ Пауза»)\n"
    "/resume <имя|группа> — снять паузу досрочно\n"
    "/movie — режим взрослых на ТВ до утра\n"
    "/bonus <кто> <минут> [категория] — добавить время к сегодняшней квоте\n"
    "/quota — экранное время и квоты детей сегодня\n"
    "/note <текст> — сохранить семейную заметку\n"
    "/notes — семейная память и заметки\n"
    "/usage — статистика расхода токенов ИИ\n"
    "/digest — дайджест за сутки прямо сейчас\n"
    "🖥 Сервер — температура, место, сервисы и копии на ПК и Pi (только владелец)\n\n"
    "Можно писать свободным текстом или слать голосовые сообщения — "
    "разберётся ИИ, а любое действие попросит подтвердить кнопкой.\n"
    "Также я сообщаю о новых устройствах, аномалиях и падении сервисов, "
    "а владельцу — о перегреве, нехватке места, старых копиях и выключении ПК."
)


def yes_no(ok: bool) -> str:
    return "✅" if ok else "❌"


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
