"""Логика бота без Telegram: форматирование, антидребезг health-мониторинга,
курсор уведомлений о новых устройствах."""

import re

import pytest

from app.bot import texts
from app.bot.health import HealthMonitor
from app.bot.notify import CURSOR_KEY, collect_notifications
from app.db import Base, engine, session
from app.models import Device, EventLog, KVState, kv_get, log_event


@pytest.fixture
def db():
    Base.metadata.create_all(engine)
    s = session()
    for model in (EventLog, Device, KVState):
        s.query(model).delete()
    s.commit()
    yield s
    s.close()


def test_bot_commands_menu():
    commands = [c for c, _ in texts.BOT_COMMANDS]
    assert len(commands) == len(set(commands))  # без дублей
    for cmd, desc in texts.BOT_COMMANDS:
        assert re.fullmatch(r"[a-z0-9_]{1,32}", cmd)  # формат Telegram
        assert 1 <= len(desc) <= 256 and desc[0].isupper()  # описания по-русски
        if cmd != "help":
            assert f"/{cmd}" in texts.HELP  # меню не расходится со справкой


def test_start_is_short_greeting():
    # /start — короткое приветствие с главными командами, а не простыня /help
    assert len(texts.START) < len(texts.HELP)
    for cmd in ("/status", "/devices", "/help"):
        assert cmd in texts.START


def test_format_status_and_devices():
    text = texts.format_status({
        "router_ok": True, "adguard_ok": False,
        "devices_total": 5, "devices_online": 3,
        "devices_blocked": 1, "devices_unknown": 2,
        "dns_queries_today": 1000, "dns_blocked_today": 50,
        "active_pauses": [{"target_type": "group", "target": "kid",
                           "until": "2026-07-14T21:30", "reason": ""}],
        "screen_time": [{"device": "Планшет", "category": "games", "label": "Игры",
                         "used_minutes": 80, "limit_minutes": 120}],
    })
    assert "✅ роутер" in text and "❌ AdGuard" in text
    assert "kid до 21:30" in text
    assert "⏳ Планшет: игры 1:20/2:00" in text  # прогресс квоты экранного времени
    assert texts.hhmm(0) == "0:00" and texts.hhmm(65) == "1:05"



def test_health_debounce():
    up = {"ok": False}
    monitor = HealthMonitor(checks={"svc": lambda: up["ok"]}, fail_after=3, ok_after=2)
    assert monitor.tick() == [] and monitor.tick() == []  # 2 фейла — рано
    assert any("не отвечает" in m for m in monitor.tick())  # 3-й — алерт
    assert monitor.tick() == []  # алерт не повторяется
    up["ok"] = True
    assert monitor.tick() == []  # 1 успех — рано
    assert any("снова работает" in m for m in monitor.tick())
    assert monitor.tick() == []


def test_health_probe_exception_counts_as_fail():
    def boom():
        raise RuntimeError("нет сети")

    monitor = HealthMonitor(checks={"svc": boom}, fail_after=1)
    assert any("не отвечает" in m for m in monitor.tick())


def test_notification_cursor(db):
    dev = Device(mac="AA:00:00:00:00:07", ip="192.168.88.77", name="tv")
    db.add(dev)
    db.commit()
    log_event(db, "device_new", f"Новое устройство: tv ({dev.mac}, {dev.ip})")

    # первый запуск: история пропускается, курсор встаёт на текущий максимум
    assert collect_notifications(db) == []
    assert kv_get(db, CURSOR_KEY) != ""

    dev2 = Device(mac="AA:00:00:00:00:08", ip="192.168.88.78", name="phone")
    db.add(dev2)
    db.commit()
    log_event(db, "device_new", f"Новое устройство: phone ({dev2.mac}, {dev2.ip})")
    log_event(db, "register_request", f"Заявка: phone ({dev2.mac}) — владелец: Бабушка")
    log_event(db, "block", "не относится к делу")

    found = collect_notifications(db)
    assert [(n.kind, n.device.id) for n in found] == [
        ("device_new", dev2.id), ("register_request", dev2.id),
    ]
    assert "Бабушка" in found[1].message
    assert collect_notifications(db) == []  # повторно не отдаёт


def test_quota_block_notification_with_bonus_button(db):
    """Блокировка по квоте не молчит: событие с MAC доходит до бота,
    кнопка «+30 мин» ведёт в тот же callback, что и /bonus."""
    from app.bot.handlers import bonus_keyboard

    dev = Device(mac="AA:00:00:00:00:09", ip="192.168.88.79", name="Планшет")
    db.add(dev)
    db.commit()
    assert collect_notifications(db) == []  # инициализация курсора

    log_event(db, "quota_block",
              f"Квота на интернет исчерпана: Планшет ({dev.mac}, {dev.ip}) — "
              "доступ выключен до полуночи.")
    found = collect_notifications(db)
    assert [(n.kind, n.device.id) for n in found] == [("quota_block", dev.id)]

    kb = bonus_keyboard(dev.id)
    assert kb.inline_keyboard[0][0].callback_data == (
        f"pick:add_bonus_time:{dev.id}:30:internet"
    )


def test_new_device_keyboard():
    from app.bot.handlers import new_device_keyboard

    kb = new_device_keyboard(5, [(1, "Миша", "kid"), (2, "Папа", "adult")])
    flat = [b for row in kb.inline_keyboard for b in row]
    data = {b.callback_data for b in flat}
    assert {"nd:5:assign:1", "nd:5:assign:2", "nd:5:block", "nd:5:skip"} <= data
    assert any("Дети" in b.text for b in flat)


def test_device_pick_keyboard():
    from app.bot.handlers import MAX_PICK_BUTTONS, device_pick_keyboard

    kb = device_pick_keyboard([(1, "Планшет"), (2, "Телефон")], "pause_internet", ":60")
    flat = [b for row in kb.inline_keyboard for b in row]
    data = [b.callback_data for b in flat]
    assert "pick:pause_internet:1:60" in data and "pick:pause_internet:2:60" in data
    assert data[-1] == "pick:cancel"

    # длинный список обрезается, кнопка отмены остаётся
    kb = device_pick_keyboard([(i, f"dev{i}") for i in range(20)], "block_device")
    assert len(kb.inline_keyboard) == MAX_PICK_BUTTONS + 1
    assert kb.inline_keyboard[0][0].callback_data == "pick:block_device:0"


def test_telegram_user_id_allowlist_parsing(monkeypatch):
    """user_id-allowlist разбирается так же, как chat_id; пустой = отключён."""
    from app.config import settings

    monkeypatch.setattr(settings, "telegram_user_ids", "545165237, 42")
    assert settings.telegram_allowed_user_ids == {545165237, 42}
    monkeypatch.setattr(settings, "telegram_user_ids", "")
    assert settings.telegram_allowed_user_ids == set()


def test_bonus_request_notification_carries_id(db):
    from app.models import BonusRequest

    dev = Device(mac="AA:00:00:00:00:22", ip="192.168.88.92", name="Планшет")
    db.add(dev)
    db.commit()
    req = BonusRequest(device_id=dev.id, category="games", status="pending")
    db.add(req)
    db.commit()
    assert collect_notifications(db) == []  # инициализация курсора
    log_event(db, "bonus_request",
              f"Запрос времени: Планшет ({dev.mac}, {dev.ip}) просит «Игры» [req#{req.id}]")
    found = collect_notifications(db)
    assert len(found) == 1 and found[0].kind == "bonus_request"
    assert found[0].device.id == dev.id and found[0].request_id == req.id


def test_bonus_request_ignores_req_number_from_child_text(db):
    """Ребёнок пишет «req#1» в причине и переименовывает устройство в чужой MAC:
    кнопки родителя всё равно должны вести к ЕГО заявке и ЕГО устройству.

    Раньше id брался первым вхождением из свободного текста, а устройство —
    первым MAC: подделывались оба.
    """
    from app.models import BonusRequest
    from app.services import bonus

    other = Device(mac="AA:00:00:00:00:31", ip="192.168.88.95", name="Телевизор")
    kid_dev = Device(mac="AA:00:00:00:00:32", ip="192.168.88.96", name="Планшет")
    db.add_all([other, kid_dev])
    db.commit()
    stale = BonusRequest(device_id=other.id, category="games", status="pending")
    db.add(stale)
    db.commit()
    assert collect_notifications(db) == []  # инициализация курсора

    # Имя устройства ребёнок задаёт сам (DHCP-hostname), причину — тоже.
    kid_dev.name = f"req#{stale.id} {other.mac}"
    db.commit()
    req = bonus.create_request(db, kid_dev, "games",
                               reason=f"ну пожалуйста req#{stale.id} {other.mac}")
    assert req is not None and str(stale.id) not in req.reason

    found = collect_notifications(db)
    assert len(found) == 1
    assert found[0].request_id == req.id          # не чужая заявка
    assert found[0].device.id == kid_dev.id       # и не чужое устройство


def test_bonus_request_approve_deny_idempotent(db):
    from app.bot.handlers import _resolve_bonus_request, bonus_request_keyboard
    from app.models import BonusRequest, Person, Quota, QuotaBonus

    for m in (BonusRequest, QuotaBonus, Quota, Person):
        db.query(m).delete()
    db.commit()
    kid = Person(name="Дима", role="kid")
    db.add(kid)
    db.commit()
    d = Device(mac="AA:00:00:00:00:21", ip="192.168.88.91", name="Димин-пк",
               person_id=kid.id)
    db.add(d)
    db.add(Quota(target_type="group", target="kid", category="games", minutes_per_day=120))
    db.commit()  # чтобы у d появился id до создания заявки
    req = BonusRequest(device_id=d.id, category="games", reason="доиграть", status="pending")
    db.add(req)
    db.commit()
    req_id = req.id

    kb = bonus_request_keyboard(req_id)
    data = {b.callback_data for row in kb.inline_keyboard for b in row}
    assert {f"br:{req_id}:15", f"br:{req_id}:30", f"br:{req_id}:day",
            f"br:{req_id}:no"} == data

    # одобрение +30 -> заявка approved, создан QuotaBonus
    assert "Одобрено" in _resolve_bonus_request(req_id, "30")
    db.expire_all()
    assert db.get(BonusRequest, req_id).status == "approved"
    assert db.get(BonusRequest, req_id).minutes == 30
    bonus_row = db.query(QuotaBonus).filter_by(target=str(d.id), category="games").one()
    assert bonus_row.minutes == 30

    # повторное нажатие по обработанной — идемпотентно
    assert "обработана" in _resolve_bonus_request(req_id, "15")

    # отказ по новой заявке
    req2 = BonusRequest(device_id=d.id, category="games", status="pending")
    db.add(req2)
    db.commit()
    assert "Отказано" in _resolve_bonus_request(req2.id, "no")
    db.expire_all()
    assert db.get(BonusRequest, req2.id).status == "denied"

    for m in (BonusRequest, QuotaBonus, Quota):
        db.query(m).delete()
    db.delete(d)
    db.query(Person).delete()
    db.commit()


def test_describe_pending_resolves_device(db):
    from app.ai.orchestrator import _describe_pending

    dev = Device(mac="AA:BB:CC:11:22:33", ip="192.168.88.45", name="Планшет Миши")
    db.add(dev)
    db.commit()

    desc = _describe_pending(db, "block_device", {"device_id": dev.id})
    assert "Планшет Миши" in desc
    assert "192.168.88.45" in desc


def test_handle_voice_missing_stt_url(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from app.bot.handlers import handle_voice
    from app.config import settings

    monkeypatch.setattr(settings, "stt_url", "")
    msg = MagicMock()
    msg.voice = MagicMock()
    msg.answer = AsyncMock()
    asyncio.run(handle_voice(msg))
    msg.answer.assert_called_once()
    assert "HS_STT_URL" in msg.answer.call_args[0][0]


def test_handle_voice_transcription_success(monkeypatch):
    import asyncio
    import io
    from unittest.mock import AsyncMock, MagicMock

    from app.bot import handlers
    from app.config import settings

    monkeypatch.setattr(settings, "stt_url", "http://lunagpt:8010/v1/audio/transcriptions")

    mock_bot = MagicMock()
    mock_file = MagicMock()
    mock_file.file_path = "voice/voice.oga"
    mock_bot.get_file = AsyncMock(return_value=mock_file)
    mock_bot.download_file = AsyncMock(return_value=io.BytesIO(b"fake_voice_bytes"))
    mock_bot.send_chat_action = AsyncMock()

    msg = MagicMock()
    msg.bot = mock_bot
    msg.chat.id = 12345
    msg.voice.file_id = "file_123"
    msg.voice.duration = 5
    msg.from_user.id = 111
    msg.voice.mime_type = "audio/ogg"
    msg.answer = AsyncMock()

    # Mock httpx post
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"text": "выключи интернет детям"}

    async def mock_post(*args, **kwargs):
        return mock_resp

    monkeypatch.setattr("httpx.AsyncClient.post", mock_post)

    processed_prompts = []

    async def mock_process(message, text):
        processed_prompts.append(text)

    monkeypatch.setattr(handlers, "_process_user_nl_prompt", mock_process)

    asyncio.run(handlers.handle_voice(msg))

    assert "выключи интернет детям" in processed_prompts
    assert msg.answer.called


def test_pause_presets_keyboard():
    from app.bot.handlers import pause_presets_keyboard

    kb = pause_presets_keyboard()
    callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert "pz:kid:30" in callbacks
    assert "pz:kid:60" in callbacks
    assert "pz:kid:30:dinner" in callbacks
    assert "pz:kid:480" in callbacks
    assert "pz:guest:60" in callbacks
    assert "pz:select" in callbacks


def test_cb_pause_preset(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from aiogram.types import Message

    from app.bot import handlers

    executed = []

    def mock_run_tool(name, args, source="bot"):
        executed.append((name, args))
        return "Пауза включена"

    monkeypatch.setattr(handlers, "run_tool_sync", mock_run_tool)

    cb = MagicMock()
    cb.data = "pz:kid:30:dinner"
    cb.answer = AsyncMock()
    msg = MagicMock(spec=Message)
    msg.edit_text = AsyncMock()
    cb.message = msg

    asyncio.run(handlers.cb_pause_preset(cb))

    assert len(executed) == 1
    assert executed[0] == ("pause_internet", {"target": "kid", "minutes": 30, "reason": "ужин"})
    msg.edit_text.assert_called_once()
    assert "Пауза включена" in msg.edit_text.call_args[0][0]


def test_format_quota_and_keyboard():
    from app.bot import texts
    from app.bot.handlers import quota_buttons_keyboard

    items = [{
        "device": "Планшет",
        "device_id": 42,
        "owner": "Миша",
        "rows": [
            {"category": "games", "label": "Игры", "used": 60, "limit": 120},
            {"category": "internet", "label": "Интернет", "used": 150, "limit": 150},
        ],
    }]
    text = texts.format_quota(items)
    assert "Миша (Планшет)" in text
    assert "Игры:" in text
    assert "осталось 60 мин" in text
    assert "лимит исчерпан ⛔" in text
    assert "█" in text and "░" in text

    kb = quota_buttons_keyboard(items)
    assert kb is not None
    callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert "pick:add_bonus_time:42:15:internet" in callbacks
    assert "pick:add_bonus_time:42:30:internet" in callbacks


def test_format_usage():
    from app.bot import texts

    today = {"input": 1000, "output": 500, "requests": 5}
    text = texts.format_usage(today, week_tokens=15000, budget_left=285000, gateway="LunaGPT")
    assert "Запросов: 5" in text
    assert "Входных токенов: 1,000" in text
    assert "Выходных токенов: 500" in text
    assert "285,000" in text
    assert "15,000" in text
    assert "LunaGPT" in text


def test_keyboards_for_roles():
    from app.bot.handlers import keyboard_for_role

    kb_owner = keyboard_for_role("owner")
    assert kb_owner is not None
    owner_buttons = [btn.text for row in kb_owner.keyboard for btn in row]
    assert "🏠 Кто дома" in owner_buttons
    assert "📺 Общие устройства" in owner_buttons
    assert "📝 Заметки" in owner_buttons

    kb_kid = keyboard_for_role("kid")
    assert kb_kid is not None
    kid_buttons = [btn.text for row in kb_kid.keyboard for btn in row]
    assert "⏳ Моё время" in kid_buttons
    assert "🙏 Попросить время" in kid_buttons
    assert "🧠 Заработать +15м" in kid_buttons
    assert "🚀 Чем заняться?" in kid_buttons

    kb_guest = keyboard_for_role("guest")
    assert kb_guest is not None
    guest_buttons = [btn.text for row in kb_guest.keyboard for btn in row]
    assert "📶 Wi-Fi" in guest_buttons

    assert keyboard_for_role("unauthorized") is None


def test_access_request_flow(db):
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from aiogram.types import Message
    from sqlalchemy import select

    from app.bot.handlers import access_request_keyboard, cb_access_request
    from app.models import Person

    kb = access_request_keyboard(123456)
    callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert "acc:123456:admin" in callbacks
    assert "acc:123456:kid" in callbacks
    assert "acc:123456:guest" in callbacks
    assert "acc:123456:deny" in callbacks

    cb = MagicMock()
    cb.data = "acc:123456:kid"
    cb.answer = AsyncMock()
    msg = MagicMock(spec=Message)
    msg.text = "Пользователь: Ваня (@vanya, ID: 123456)"
    msg.edit_text = AsyncMock()
    cb.message = msg
    cb.bot = MagicMock()
    cb.bot.send_message = AsyncMock()

    asyncio.run(cb_access_request(cb))

    person = db.scalar(select(Person).where(Person.telegram_id == 123456))
    assert person is not None
    assert person.name == "Ваня"
    assert person.role == "kid"
    assert person.bot_role == "kid"

    msg.edit_text.assert_called_once()
    assert "Ребёнок" in msg.edit_text.call_args[0][0]
    cb.bot.send_message.assert_called_once()


def test_new_device_kind_assignment(db):
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from aiogram.types import Message

    from app.bot.handlers import cb_new_device, new_device_keyboard
    from app.models import Device

    dev = Device(mac="AA:33:00:00:00:01", ip="192.168.88.80", name="Samsung TV")
    db.add(dev)
    db.commit()

    kb = new_device_keyboard(dev.id, [])
    callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert f"nd:{dev.id}:kind:shared" in callbacks
    assert f"nd:{dev.id}:kind:iot" in callbacks

    cb = MagicMock()
    cb.data = f"nd:{dev.id}:kind:shared"
    cb.answer = AsyncMock()
    msg = MagicMock(spec=Message)
    msg.text = "Новое устройство: Samsung TV"
    msg.edit_text = AsyncMock()
    cb.message = msg

    asyncio.run(cb_new_device(cb))

    db.refresh(dev)
    assert dev.kind == "shared"
    assert dev.group == "shared"
    msg.edit_text.assert_called_once()
    assert "Общее (ТВ/Колонка)" in msg.edit_text.call_args[0][0]


def test_kid_guards_parent_commands(db):
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from aiogram.filters import CommandObject

    from app.bot.handlers import cmd_block, cmd_movie, cmd_pause
    from app.models import Person

    kid = Person(name="Лена", role="kid", telegram_id=8888, bot_role="kid")
    db.add(kid)
    db.commit()

    msg = MagicMock()
    msg.from_user.id = 8888
    msg.answer = AsyncMock()

    cmd_obj = CommandObject(prefix="/", command="pause", args="30")
    asyncio.run(cmd_pause(msg, cmd_obj))
    msg.answer.assert_called_once()
    assert "доступна только родителям" in msg.answer.call_args[0][0]

    msg.answer.reset_mock()
    cmd_block_obj = CommandObject(prefix="/", command="block", args="1")
    asyncio.run(cmd_block(msg, cmd_block_obj))
    msg.answer.assert_called_once()
    assert "доступна только родителям" in msg.answer.call_args[0][0]

    msg.answer.reset_mock()
    asyncio.run(cmd_movie(msg))
    msg.answer.assert_called_once()
    assert "доступна только родителям" in msg.answer.call_args[0][0]


def test_bonus_direct_approval(db):
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from aiogram.types import Message
    from sqlalchemy import select

    from app.bot.handlers import bonus_request_direct_keyboard, cb_bonus_direct
    from app.models import Person, QuotaBonus

    kid = Person(name="Коля", role="kid", telegram_id=9999, bot_role="kid")
    db.add(kid)
    db.commit()

    kb = bonus_request_direct_keyboard(kid.id)
    callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert f"bdir:{kid.id}:15" in callbacks
    assert f"bdir:{kid.id}:30" in callbacks
    assert f"bdir:{kid.id}:day" in callbacks
    assert f"bdir:{kid.id}:deny" in callbacks

    cb = MagicMock()
    cb.data = f"bdir:{kid.id}:30"
    cb.answer = AsyncMock()
    msg = MagicMock(spec=Message)
    msg.text = "Запрос дополнительного времени: Коля"
    msg.edit_text = AsyncMock()
    msg.photo = None
    cb.message = msg
    cb.bot = MagicMock()
    cb.bot.send_message = AsyncMock()

    asyncio.run(cb_bonus_direct(cb))

    qb = db.scalar(select(QuotaBonus).where(QuotaBonus.target == str(kid.id)))
    assert qb is not None
    assert qb.minutes == 30

    cb.bot.send_message.assert_called_once()
    assert "30 минут" in cb.bot.send_message.call_args[0][1]


def test_kid_explore_keyboard_and_callback():
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch

    from aiogram.types import Message

    from app.bot.handlers import cb_kid_explore, kid_explore_keyboard

    kb = kid_explore_keyboard()
    callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert "kexp:quest" in callbacks
    assert "kexp:game" in callbacks
    assert "kexp:english" in callbacks
    assert "kexp:fact" in callbacks

    cb = MagicMock()
    cb.data = "kexp:quest"
    cb.answer = AsyncMock()
    msg = MagicMock(spec=Message)
    msg.answer = AsyncMock()
    cb.message = msg

    with patch("app.bot.handlers._handle_kid_tutor", new=AsyncMock()) as mock_tutor:
        asyncio.run(cb_kid_explore(cb))
        mock_tutor.assert_called_once()
        assert "мини-квест" in mock_tutor.call_args[0][1]


def test_quiz_flow_and_parent_notification(db, monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from aiogram.types import Message
    from sqlalchemy import select

    from app.bot.handlers import cb_quiz, quiz_keyboard
    from app.config import settings
    from app.models import Person, QuotaBonus
    from app.services import family as family_svc

    monkeypatch.setattr(settings, "telegram_chat_ids", "777001")

    kid = Person(name="Артём", role="kid", telegram_id=5555, bot_role="kid")
    db.add(kid)
    db.commit()

    q_data = {
        "id": -1,
        "question": "Сколько спутников у Марса?",
        "options": ["1", "2", "3", "4"],
        "correct": 1,
        "topic": "Космос",
    }
    family_svc.store_active_quiz(db, kid.id, q_data)

    kb = quiz_keyboard(kid.id, q_data["options"])
    callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert f"qz:{kid.id}:0" in callbacks
    assert f"qz:{kid.id}:1" in callbacks

    # Проверяем верный ответ
    cb = MagicMock()
    cb.data = f"qz:{kid.id}:1"
    cb.from_user.id = 5555
    cb.answer = AsyncMock()
    msg = MagicMock(spec=Message)
    msg.text = "Вопрос викторины"
    msg.edit_text = AsyncMock()
    cb.message = msg
    cb.bot = MagicMock()
    cb.bot.send_message = AsyncMock()

    asyncio.run(cb_quiz(cb))

    qb = db.scalar(select(QuotaBonus).where(QuotaBonus.target == str(kid.id)))
    assert qb is not None
    assert qb.minutes == 15
    assert "Космос" in qb.comment

    msg.edit_text.assert_called_once()
    assert "Абсолютно верно" in msg.edit_text.call_args[0][0]

    cb.bot.send_message.assert_called_once()
    parent_alert = cb.bot.send_message.call_args[0][1]
    assert "Артём выиграл в викторине" in parent_alert
    assert "Космос" in parent_alert
    assert "Сколько спутников у Марса?" in parent_alert
