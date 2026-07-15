"""Форматирование сообщений бота — чистые функции, покрыты тестами."""

# Меню команд Telegram: бот регистрирует его при старте (set_my_commands),
# чтобы команды подсказывались по «/» и их не надо было помнить наизусть.
BOT_COMMANDS: list[tuple[str, str]] = [
    ("status", "Состояние сети и сервисов"),
    ("devices", "Устройства: кто онлайн, блокировки"),
    ("block", "Заблокировать интернет устройству"),
    ("unblock", "Снять блокировку"),
    ("pause", "Пауза интернета на время"),
    ("resume", "Снять паузу досрочно"),
    ("bonus", "Добавить время к квоте на сегодня"),
    ("digest", "Дайджест за сутки прямо сейчас"),
    ("help", "Все команды и подсказки"),
]

# /start — короткое приветствие с главным, а не простыня /help.
START = (
    "Привет! Я — бот HomeSec, слежу за домашней сетью.\n\n"
    "/status — что сейчас в сети\n"
    "/devices — устройства и кто онлайн\n"
    "/help — все команды\n\n"
    "Можно писать и обычным текстом («выключи детям интернет на час») — "
    "разберётся ИИ, а действие попросит подтвердить кнопкой."
)

HELP = (
    "Я — бот HomeSec, слежу за домашней сетью.\n\n"
    "/status — состояние сети и сервисов\n"
    "/devices — список устройств\n"
    "/block <имя|id> — заблокировать интернет устройству\n"
    "/unblock <имя|id> — снять блокировку\n"
    "/pause <имя|группа> <минут> — пауза интернета (группы: kid, guest, unknown)\n"
    "/resume <имя|группа> — снять паузу досрочно\n"
    "/bonus <кто> <минут> [категория] — добавить время к сегодняшней квоте\n"
    "/digest — дайджест за сутки прямо сейчас\n\n"
    "Можно писать свободным текстом («выключи детям интернет на час») — "
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


def format_devices(rows: list[dict]) -> str:
    """Группировка по владельцам (люди по алфавиту, безхозные — в конце под
    «Неизвестные»), метка онлайн 🟢/⚪ (online=None — роутер недоступен)."""
    if not rows:
        return "Устройств пока нет."
    by_owner: dict[str, list[dict]] = {}
    for r in rows:
        by_owner.setdefault(r.get("owner") or "", []).append(r)
    owners = sorted(k for k in by_owner if k)
    if "" in by_owner:
        owners.append("")

    lines = ["Устройства"]
    for owner in owners:
        lines += ["", f"{owner or 'Неизвестные'}:"]
        for r in by_owner[owner]:
            marks = ""
            if r["blocked_manual"]:
                marks += " ⛔"
            if r["paused_until"]:
                marks += f" ⏸ до {r['paused_until'][11:16]}"
            if r["speed_limit"]:
                marks += f" 🐢 {r['speed_limit']}"
            dot = "🟢" if r.get("online") else "⚪"
            lines.append(f"{dot} #{r['id']} {r['name']} ({r['group_label']}){marks}")
    if any(r.get("online") is None for r in rows):
        lines += ["", "⚠️ Роутер недоступен — онлайн-статус неизвестен"]
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
