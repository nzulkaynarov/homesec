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

PARENT_COMMANDS = BOT_COMMANDS

KID_COMMANDS: list[tuple[str, str]] = [
    ("quota", "Мой остаток времени на сегодня"),
    ("ask", "Попросить ещё времени у родителей"),
    ("quiz", "Заработать +15 минут (викторина)"),
    ("explore", "Чем заняться (квесты, игры, наука)"),
    ("help", "Помощь и подсказки"),
]

GUEST_COMMANDS: list[tuple[str, str]] = [
    ("wifi", "Данные гостевого Wi-Fi"),
    ("help", "Помощь"),
]

START_PARENT = (
    "Привет! Я — бот HomeSec 🛡\n\n"
    "/status — состояние сети\n"
    "/who — радар «Кто дома»\n"
    "/devices — устройства: владелец, имя, объединение, удаление\n"
    "/quota — экранное время детей\n"
    "/pause — быстрая пауза\n"
    "/movie — Общие устройства\n"
    "/notes — семейная память\n"
    "/help — все команды\n\n"
    "Можно писать свободным текстом или слать голосовые сообщения!"
)

START_KID = (
    "Привет! Я — твой умный семейный ассистент HomeSec 🎒\n\n"
    "Здесь ты можешь:\n"
    "⏳ Проверить остаток времени на сегодня (/quota)\n"
    "🙏 Попросить родителей добавить время (/ask)\n"
    "🧠 Заработать +15 минут в викторине (/quiz)\n"
    "🚀 Найти крутое занятие: текстовые квесты, игры, наука (/explore)\n"
    "🎓 Задать любой вопрос по урокам, кодингу или миру вокруг!\n\n"
    "Используй удобные кнопки внизу экрана 👇"
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
    "/block <имя|id> — заблокировать интернет устройству\n"
    "/unblock <имя|id> — снять блокировку\n"
    "/pause <имя|группа> [минут] — пауза интернета (без аргументов — быстрые кнопки)\n"
    "/resume <имя|группа> — снять паузу досрочно\n"
    "/movie — режим взрослых на ТВ до утра\n"
    "/bonus <кто> <минут> [категория] — добавить время к сегодняшней квоте\n"
    "/quota — экранное время и квоты детей сегодня\n"
    "/note <текст> — сохранить семейную заметку\n"
    "/notes — семейная память и заметки\n"
    "/usage — статистика расхода токенов ИИ\n"
    "/digest — дайджест за сутки прямо сейчас\n\n"
    "Можно писать свободным текстом или слать голосовые сообщения — "
    "разберётся ИИ, а любое действие попросит подтвердить кнопкой.\n"
    "Также я сообщаю о новых устройствах, аномалиях и падении сервисов."
)


def yes_no(ok: bool) -> str:
    return "✅" if ok else "❌"


def hhmm(minutes: int) -> str:
    """Минуты -> «Ч:ММ»: 80 -> 1:20."""
    return f"{minutes // 60}:{minutes % 60:02d}"


def format_status(s: dict) -> str:
    lines = [
        "Состояние HomeSec",
        f"{yes_no(s['router_ok'])} роутер MikroTik",
        f"{yes_no(s['adguard_ok'])} AdGuard Home",
        "",
        f"Устройств: {s['devices_total']}, онлайн: {s['devices_online']}",
        f"Заблокировано вручную: {s['devices_blocked']}, неизвестных: {s['devices_unknown']}",
    ]
    if s.get("dns_queries_today") is not None:
        lines.append(
            f"DNS сегодня: {s['dns_queries_today']} запросов, "
            f"{s.get('dns_blocked_today', 0)} заблокировано"
        )
    for p in s.get("active_pauses", []):
        until = p["until"][11:16] if len(p["until"]) >= 16 else p["until"]
        lines.append(f"⏸ пауза: {p['target']} до {until}")
    for q in s.get("screen_time", []):  # экранное время: «игры 1:20/2:00»
        lines.append(f"⏳ {q['device']}: {q['label'].lower()} "
                     f"{hhmm(q['used_minutes'])}/{hhmm(q['limit_minutes'])}")
    return "\n".join(lines)


def format_registration(event_message: str) -> str:
    return f"📨 {event_message}\n\nКому назначить устройство?"


def format_new_device(dev: dict) -> str:
    warning = ""
    if dev.get("random_mac"):
        warning = ("\n⚠️ Случайный MAC («приватный адрес»). Для домашней сети "
                   "его лучше выключить на устройстве, иначе правила будут слетать.")
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


def format_quota(items: list[dict]) -> str:
    """Форматирует экранное время детей с визуальными прогресс-барами."""
    if not items:
        return "⏳ На сегодня правил экранного времени не назначено."
    lines = ["⏳ Экранное время детей сегодня:"]
    for item in items:
        title = item["device"]
        if item.get("owner"):
            title = f"{item['owner']} ({title})"
        lines.append(f"\n📱 {title}:")
        for row in item["rows"]:
            used, limit = row["used"], row["limit"]
            bar = progress_bar(used, limit)
            left = max(0, limit - used)
            left_str = f"осталось {left} мин" if left > 0 else "лимит исчерпан ⛔"
            lines.append(
                f"  {row['label']}: {bar} {hhmm(used)}/{hhmm(limit)} ({left_str})"
            )
    return "\n".join(lines)


def format_usage(
    today: dict, week_tokens: int, budget_left: int | None, gateway: str = ""
) -> str:
    """Форматирует статистику расхода токенов ИИ-шлюза."""
    in_tok = today.get("input", 0)
    out_tok = today.get("output", 0)
    total_today = in_tok + out_tok
    reqs = today.get("requests", 0)

    lines = [
        "🤖 Расход токенов ИИ-шлюза",
        "",
        "Сегодня:",
        f"• Запросов: {reqs}",
        f"• Входных токенов: {in_tok:,}",
        f"• Выходных токенов: {out_tok:,}",
        f"• Всего сегодня: {total_today:,}",
    ]
    if budget_left is not None:
        lines.append(f"• Остаток бюджета: {budget_left:,}")
    else:
        lines.append("• Дневной бюджет: без ограничений")

    lines.extend([
        "",
        f"За последние 7 дней: {week_tokens:,} токенов",
    ])
    if gateway:
        lines.append(f"Шлюз: {gateway}")
    return "\n".join(lines)


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


def format_spend_by_person(rows: list[tuple[str, float, int, dict]], week_usd: float) -> str:
    """Today's estimated AI spend per person and feature."""
    if not rows:
        return f"Расход сегодня: $0.00\nЗа 7 дней: ${week_usd:.2f}"
    lines = ["💵 Расход сегодня (оценка):"]
    for name, usd, requests, features in rows:
        top = sorted(features.items(), key=lambda item: item[1], reverse=True)
        detail = ", ".join(f"{FEATURE_LABELS.get(k, k)} ${v:.2f}" for k, v in top)
        lines.append(f"• {name}: ${usd:.2f}, запросов {requests} ({detail})")
    lines.append(f"Итого сегодня: ${sum(r[1] for r in rows):.2f}")
    lines.append(f"За 7 дней: ${week_usd:.2f}")
    return "\n".join(lines)
