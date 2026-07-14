"""Форматирование сообщений бота — чистые функции, покрыты тестами."""

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
    return "\n".join(lines)


def format_devices(rows: list[dict]) -> str:
    if not rows:
        return "Устройств пока нет."
    lines = ["Устройства"]
    for r in rows:
        marks = ""
        if r["blocked_manual"]:
            marks += " ⛔"
        if r["paused_until"]:
            marks += f" ⏸ до {r['paused_until'][11:16]}"
        if r["speed_limit"]:
            marks += f" 🐢 {r['speed_limit']}"
        owner = f", {r['owner']}" if r["owner"] else ""
        lines.append(f"• #{r['id']} {r['name']} ({r['group_label']}{owner}){marks}")
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
