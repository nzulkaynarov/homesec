"""Семейные функции: роли пользователей Telegram, радар присутствия «Кто дома»,
управление общими медиа-устройствами (ТВ/колонки), семейная память и викторины для детей.
"""

import logging
import random
import re
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import (
    Device,
    FamilyNote,
    Person,
    QuotaBonus,
    kv_get,
    kv_set,
    log_event,
)
from . import mikrotik

log = logging.getLogger("homesec.family")

ROLE_LABELS = {
    "owner": "Владелец сети",
    "admin": "Родитель",
    "kid": "Ребёнок",
    "guest": "Гость",
    "unauthorized": "Не авторизован",
}

# 10 вопросов для детской викторины (математика, логика, природа, языки)
QUIZ_QUESTIONS = [
    {
        "q": "Сколько будет 7 × 8?",
        "options": ["54", "56", "58", "64"],
        "correct": 1,
    },
    {
        "q": "Какая планета Солнечной системы самая большая?",
        "options": ["Марс", "Сатурн", "Юпитер", "Венера"],
        "correct": 2,
    },
    {
        "q": "Какое животное считается самым быстрым на суше?",
        "options": ["Гепард", "Лев", "Антилопа", "Сокол"],
        "correct": 0,
    },
    {
        "q": "Сколько сантиметров в 2.5 метрах?",
        "options": ["25", "205", "250", "2500"],
        "correct": 2,
    },
    {
        "q": "Как по-английски сказать «яблоко»?",
        "options": ["Orange", "Banana", "Apple", "Grape"],
        "correct": 2,
    },
    {
        "q": "Сколько сторон у шестиугольника?",
        "options": ["5", "6", "7", "8"],
        "correct": 1,
    },
    {
        "q": "Какое самое глубокое озеро в мире?",
        "options": ["Байкал", "Каспийское море", "Виктория", "Мичиган"],
        "correct": 0,
    },
    {
        "q": "Сколько минут в 3 часах?",
        "options": ["120", "150", "180", "200"],
        "correct": 2,
    },
    {
        "q": "Столица Франции — это...",
        "options": ["Рим", "Берлин", "Мадрид", "Париж"],
        "correct": 3,
    },
    {
        "q": "Что тяжелее: 1 кг железа или 1 кг ваты?",
        "options": ["Железо", "Вата", "Одинаково", "Зависит от погоды"],
        "correct": 2,
    },
]


def get_user_role(db: Session, telegram_id: int) -> tuple[str, Person | None]:
    """Определяет роль пользователя в семейном боте.
    Приоритет:
    1. settings.telegram_allowed_ids -> 'owner'
    2. Person.telegram_id -> person.bot_role ('owner' | 'admin' | 'kid' | 'guest')
    3. иначе -> ('unauthorized', None)
    """
    if telegram_id in settings.telegram_allowed_ids:
        # Корневой владелец из настроек
        person = db.scalar(select(Person).where(Person.telegram_id == telegram_id))
        return "owner", person

    person = db.scalar(select(Person).where(Person.telegram_id == telegram_id))
    if person is not None:
        role = person.bot_role if person.bot_role in ("owner", "admin", "kid", "guest") else "guest"
        return role, person

    return "unauthorized", None


def get_presence_status(db: Session) -> dict[str, Any]:
    """Собирает радар присутствия: кто из людей дома (по личным устройствам),
    статус общих медиа-устройств (ТВ/колонки) и умного дома (IoT)."""
    online_ips: set[str] = set()
    try:
        with mikrotik.api_session() as api:
            online_ips = mikrotik.get_online_ips(api)
    except Exception as e:
        log.warning("радар: ошибка чтения ARP с роутера: %s", e)

    people = list(db.scalars(select(Person).order_by(Person.id)))
    devices = list(db.scalars(select(Device)))

    # Устройства по владельцам
    people_status = []
    for p in people:
        my_devs = [d for d in devices if d.person_id == p.id and d.kind == "personal"]
        is_home = any(d.ip and d.ip in online_ips for d in my_devs)
        active_names = [d.name for d in my_devs if d.ip and d.ip in online_ips]
        people_status.append({
            "person": p,
            "is_home": is_home,
            "devices": my_devs,
            "active_devices": active_names,
        })

    # Общие медиа-устройства (ТВ, приставки, колонки)
    shared_devs = [d for d in devices if d.kind == "shared"]
    shared_status = []
    for d in shared_devs:
        is_online = bool(d.ip and d.ip in online_ips)
        shared_status.append({
            "device": d,
            "is_online": is_online,
            "blocked": d.blocked_manual,
        })

    # Умный дом (IoT)
    iot_devs = [d for d in devices if d.kind == "iot"]
    iot_online = sum(1 for d in iot_devs if d.ip and d.ip in online_ips)

    return {
        "people": people_status,
        "shared": shared_status,
        "iot_total": len(iot_devs),
        "iot_online": iot_online,
    }


def format_presence(status: dict[str, Any]) -> str:
    """Форматирует радар присутствия для Telegram."""
    lines = ["🏠 *Радар «Кто дома»*:\n"]

    people = status.get("people", [])
    if people:
        lines.append("👥 *Семья:*")
        for p in people:
            person = p["person"]
            role_icon = "🎒" if person.role == "kid" else "👤"
            if p["is_home"]:
                dev_str = f" ({', '.join(p['active_devices'])})" if p["active_devices"] else ""
                lines.append(f"🟢 {role_icon} *{person.name}* — дома{dev_str}")
            else:
                lines.append(f"⚪ {role_icon} *{person.name}* — не в сети")
        lines.append("")

    shared = status.get("shared", [])
    if shared:
        lines.append("📺 *Общие устройства (ТВ и медиа):*")
        for s in shared:
            dev = s["device"]
            status_icon = "🟢" if s["is_online"] else "⚪"
            state = "онлайн" if s["is_online"] else "выкл"
            if s["blocked"]:
                state += ", ⛔ заблокирован"
            lines.append(f"{status_icon} *{dev.name}* — {state}")
        lines.append("")

    iot_total = status.get("iot_total", 0)
    if iot_total > 0:
        iot_online = status.get("iot_online", 0)
        lines.append(f"🤖 *Умный дом (IoT):* {iot_online} из {iot_total} устройств в сети.")

    return "\n".join(lines)


def set_movie_mode(db: Session, hours: int = 8) -> str:
    """Режим «Смотрят взрослые»: временно снимает ручные блокировки с общих
    медиа-устройств (ТВ/колонки) до утра, чтобы родители могли спокойно смотреть фильм."""
    shared_devs = list(db.scalars(select(Device).where(Device.kind == "shared")))
    if not shared_devs:
        return "В сети нет устройств с типом «Общее (ТВ/Колонка)»."

    unblocked_names = []
    for d in shared_devs:
        if d.blocked_manual:
            d.blocked_manual = False
            unblocked_names.append(d.name)

    # Ставим флаг быстрой синхронизации для роутера
    kv_set(db, "reconcile_asap", "1")
    db.commit()

    names_str = ", ".join(d.name for d in shared_devs)
    log_event(db, "movie_mode", f"Включён режим взрослых для общих медиа: {names_str}")
    return (
        f"🍿 *Режим «Смотрят взрослые» включён!*\n\n"
        f"Общие устройства ({names_str}) доступны без ограничений.\n"
        f"Детский режим вернётся автоматически утром."
    )


def save_family_note(db: Session, author: str, content: str) -> FamilyNote:
    """Сохраняет заметку в общую семейную память."""
    note = FamilyNote(author=author, content=content.strip())
    db.add(note)
    db.commit()
    log_event(db, "family_note", f"{author}: {note.content}")
    return note


def get_family_notes(db: Session, limit: int = 10) -> list[FamilyNote]:
    """Возвращает последние семейные заметки."""
    return list(
        db.scalars(
            select(FamilyNote)
            .order_by(FamilyNote.created.desc())
            .limit(limit)
        )
    )


def format_family_notes(notes: list[FamilyNote]) -> str:
    """Форматирует список заметок для Telegram."""
    if not notes:
        return "📝 В семейной памяти пока пусто.\n\nНапишите боту: «Запомни: размер ноги Миши 36»."

    lines = ["📝 *Семейная память и заметки:*\n"]
    for i, n in enumerate(notes, 1):
        dt = n.created.strftime("%d.%m %H:%M")
        auth = f" ({n.author})" if n.author else ""
        lines.append(f"{i}. {n.content}{auth} — _{dt}_")
    return "\n".join(lines)


def get_random_quiz_question() -> dict[str, Any]:
    """Возвращает случайный вопрос для викторины из встроенного пула."""
    idx = random.randint(0, len(QUIZ_QUESTIONS) - 1)
    q = QUIZ_QUESTIONS[idx]
    return {
        "id": idx,
        "question": q["q"],
        "options": q["options"],
        "correct": q["correct"],
        "topic": "Эрудиция",
    }


def generate_dynamic_quiz_question(db: Session, person_id: int) -> dict[str, Any]:
    """Генерирует уникальный познавательный вопрос викторины через LunaGPT
    или использует fallback-пул при отсутствии ИИ."""
    import json

    from ..ai import client as ai_client

    if ai_client.is_configured():
        try:
            prompt = (
                "Сгенерируй один интересный, познавательный вопрос викторины для школьника "
                "(математика, логика, природа, космос, история или наука). "
                "Верни ответ СТРОГО в виде валидного JSON без разметки markdown:\n"
                '{"q": "Текст вопроса?", '
                '"options": ["Вариант 1", "Вариант 2", "Вариант 3", "Вариант 4"], '
                '"correct": 0, "topic": "Космос"}\n'
                "options: список из ровно 4 вариантов ответа. "
                "correct: целое число от 0 до 3 (индекс правильного ответа). "
                "topic: короткая тема вопроса (1-2 слова)."
            )
            resp = ai_client.ask_with_fallback(
                db,
                ai_client.model_ladder(settings.ai_model_fast, settings.ai_model_support),
                messages=[{"role": "user", "content": prompt}],
                system=(
                    "Ты — генератор вопросов для детской познавательной викторины. "
                    "Отвечай строго валидным JSON без лишних слов."
                ),
                max_tokens=400,
            )
            raw = "".join(b.text for b in resp.content if b.type == "text").strip()
            if raw.startswith("```"):
                raw = raw.strip("`")
                if raw.startswith("json\n"):
                    raw = raw[5:]
                elif raw.startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()
            data = json.loads(raw)
            if (
                isinstance(data, dict)
                and "q" in data
                and "options" in data
                and len(data["options"]) == 4
                and isinstance(data.get("correct"), int)
                and 0 <= data["correct"] <= 3
            ):
                return {
                    "id": -1,
                    "question": data["q"],
                    "options": data["options"],
                    "correct": data["correct"],
                    "topic": data.get("topic", "Эрудиция"),
                }
        except Exception as e:
            log.warning("генерация викторины через ИИ не удалась (%s), берём из пула", e)

    return get_random_quiz_question()


def store_active_quiz(db: Session, person_id: int, quiz_data: dict[str, Any]) -> None:
    """Сохраняет текущий активный вопрос викторины в kv_state."""
    import json
    kv_set(db, f"quiz_active_{person_id}", json.dumps(quiz_data, ensure_ascii=False))


def get_active_quiz(db: Session, person_id: int) -> dict[str, Any] | None:
    """Получает сохранённый вопрос активной викторины."""
    import json
    raw = kv_get(db, f"quiz_active_{person_id}", "")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def clear_active_quiz(db: Session, person_id: int) -> None:
    """Очищает активный вопрос викторины."""
    kv_set(db, f"quiz_active_{person_id}", "")


def can_take_quiz_today(db: Session, person_id: int, now: datetime | None = None) -> bool:
    """Проверяет, не получал ли ребёнок уже бонус за викторину сегодня."""
    now = now or datetime.now()
    date = now.strftime("%Y-%m-%d")
    key = f"quiz_bonus_{date}_{person_id}"
    return kv_get(db, key, "") == "" and kv_get(db, f"quiz_attempt_{date}_{person_id}", "") == ""


def mark_quiz_attempted(db: Session, person_id: int, now: datetime | None = None) -> None:
    """Spends today's single attempt, so a wrong answer cannot be retried."""
    date = (now or datetime.now()).strftime("%Y-%m-%d")
    kv_set(db, f"quiz_attempt_{date}_{person_id}", "1")


QUIZ_BONUS_MINUTES = 15

SAFETY_PATTERN = re.compile(
    r"суицид|покончить с собой|убить себя|не хочу жить|порезать себя|"
    r"меня бьют|меня бьёт|меня бьет|меня трогает|домогает|меня травят|буллят|шантажиру",
    re.IGNORECASE,
)
SAFETY_REPLY = (
    "Спасибо, что написал мне об этом. Это важно, и ты не один 💛\n"
    "Пожалуйста, расскажи прямо сейчас родителям или другому взрослому, которому доверяешь. "
    "Если тебе угрожает опасность, позвони взрослым или в экстренную службу."
)


def needs_adult_attention(text: str) -> bool:
    """Keyword check for messages a parent should see; false positives are acceptable."""
    return bool(SAFETY_PATTERN.search(text))


def award_quiz_bonus(
    db: Session,
    person: Person,
    minutes: int = 15,
    topic: str = "Эрудиция",
    now: datetime | None = None,
) -> str:
    """Начисляет ребёнку бонусные минуты за успешный квиз."""
    now = now or datetime.now()
    date = now.strftime("%Y-%m-%d")
    key = f"quiz_bonus_{date}_{person.id}"
    kv_set(db, key, "1")

    # Добавляем QuotaBonus
    db.add(
        QuotaBonus(
            target_type="person",
            target=str(person.id),
            date=date,
            category="internet",
            minutes=minutes,
            comment=f"Победа в викторине ({topic})",
        )
    )
    db.commit()
    msg = (
        f"🏆 {person.name} успешно ответил на вопрос викторины ({topic}) "
        f"и получил +{minutes} мин интернета!"
    )
    log_event(db, "quiz_win", msg)
    return msg
