"""Логика бота без Telegram: форматирование, антидребезг health-мониторинга,
курсор уведомлений о новых устройствах."""

import re

import pytest

from app.bot import screens, texts
from app.bot.health import HealthMonitor
from app.bot.notify import CURSOR_KEY, collect_notifications
from app.bot.rich import to_plain
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
    """The "/" menu only brings the buttons back and explains them; nothing else lives there."""
    commands = [c for c, _ in texts.BOT_COMMANDS]
    assert commands == ["start", "help"]
    for cmd, desc in texts.BOT_COMMANDS:
        assert re.fullmatch(r"[a-z0-9_]{1,32}", cmd)  # формат Telegram
        assert 1 <= len(desc) <= 256 and desc[0].isupper()  # описания по-русски


def _keyboard_buttons(role: str) -> list[str]:
    from app.bot.common import keyboard_for_role

    keyboard = keyboard_for_role(role)
    assert keyboard is not None
    return [button.text for row in keyboard.keyboard for button in row]


@pytest.mark.parametrize("role", ["owner", "admin"])
def test_start_and_help_explain_every_button_without_commands(role):
    """Button-first: each button of this parent's keyboard in one line, then words and voice;
    a slash command appears only on the help's last line, for those used to them."""
    start, help_ = texts.start_text(role), texts.help_text(role)
    assert len(start) < len(help_)
    for text in (start, help_):
        lines = text.splitlines()
        for button in _keyboard_buttons(role):
            assert sum(line.startswith(f"{button} — ") for line in lines) == 1, button
        assert "голосов" in text and "«запомни:" in text
    assert "/" not in start
    *main, last = help_.splitlines()
    assert "/" not in "\n".join(main) and last == texts.COMMANDS
    assert ("🖥 Сервер" in start) == (role == "owner")


def test_power_user_commands_in_the_help_still_work():
    from app.bot import handlers

    registered = {
        command
        for handler in handlers.router.message.handlers
        for flt in handler.filters or []
        for command in getattr(flt.callback, "commands", ())
    }
    named = set(re.findall(r"/([a-z]+)", texts.COMMANDS))
    assert named and named <= registered
    # The old commands keep working even though the help no longer lists them.
    assert {"status", "usage", "digest", "notes", "who", "quota", "movie"} <= registered


def test_format_status_and_devices():
    from datetime import datetime

    html = screens.status_html(
        {
            "router_ok": True, "adguard_ok": False,
            "devices_total": 5, "devices_online": 3,
            "devices_blocked": 1, "devices_unknown": 2,
            "dns_queries_today": 1000, "dns_blocked_today": 50,
            "active_pauses": [{"target_type": "group", "target": "kid",
                               "until": "2026-07-14T21:30", "reason": ""}],
            "screen_time": [{"device": "Планшет", "category": "games", "label": "Игры",
                             "used_minutes": 80, "limit_minutes": 120}],
        },
        [("Все дети", datetime(2026, 7, 14, 21, 30))],
    )
    text = to_plain(html)
    assert "Роутер · ✅ работает" in text and "Фильтр сайтов · ❌ не отвечает" in text
    assert "Обращений к сайтам сегодня · 1000, заблокировано 50" in text
    assert "Все дети · 21:30" in text and "kid" not in text  # who by name, not a code
    assert "Планшет · игры · 1:20/2:00" in text  # прогресс квоты экранного времени
    assert not re.search(r"MikroTik|AdGuard|DNS", text)
    assert "<table" in html
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
    """A quota block reaches the bot, and its buttons give time to the device's owner by the
    same rule as every other "give time" button; an ownerless device gets none."""
    from app.bot.alerts import bonus_keyboard

    dev = Device(mac="AA:00:00:00:00:09", ip="192.168.88.79", name="Планшет")
    db.add(dev)
    db.commit()
    assert collect_notifications(db) == []  # инициализация курсора

    log_event(db, "quota_block",
              f"Квота на интернет исчерпана: Планшет ({dev.mac}, {dev.ip}) — "
              "доступ выключен до полуночи.")
    found = collect_notifications(db)
    assert [(n.kind, n.device.id) for n in found] == [("quota_block", dev.id)]
    assert found[0].category == "internet"

    assert bonus_keyboard(None) is None
    kb = bonus_keyboard(7)
    assert [b.callback_data for b in kb.inline_keyboard[0]] == ["gt:7:15:l", "gt:7:30:l"]


def test_new_device_keyboard():
    from app.bot.alerts import new_device_keyboard

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
    from app.bot.alerts import _resolve_bonus_request, bonus_request_keyboard
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
    db.add(Quota(target_type="person", target=str(kid.id), category="games", minutes_per_day=120))
    db.commit()  # чтобы у d появился id до создания заявки
    req = BonusRequest(device_id=d.id, category="games", reason="доиграть", status="pending")
    db.add(req)
    db.commit()
    req_id = req.id

    kb = bonus_request_keyboard(req_id)
    data = {b.callback_data for row in kb.inline_keyboard for b in row}
    assert {f"br:{req_id}:15:l", f"br:{req_id}:30:l", f"br:{req_id}:day:l",
            f"br:{req_id}:no:l"} == data

    # одобрение +30 -> заявка approved, создан QuotaBonus
    answer = _resolve_bonus_request(req_id, "30", "l")
    assert answer.final and answer.text == "✅ Дима: +30 мин к лимитам: игры."
    db.expire_all()
    assert db.get(BonusRequest, req_id).status == "approved"
    assert db.get(BonusRequest, req_id).minutes == 30
    bonus_row = db.query(QuotaBonus).filter_by(target=str(kid.id), category="games").one()
    assert (bonus_row.target_type, bonus_row.minutes) == ("person", 30)

    # a second press, by the same or the other parent, adds nothing
    assert "уже ответили: одобрено" in _resolve_bonus_request(req_id, "15", "l").text
    assert db.query(QuotaBonus).count() == 1

    # отказ по новой заявке
    req2 = BonusRequest(device_id=d.id, category="games", status="pending")
    db.add(req2)
    db.commit()
    assert _resolve_bonus_request(req2.id, "no").text == "🚫 Дима: отказано, Luna скажет об этом."
    db.expire_all()
    assert db.get(BonusRequest, req2.id).status == "denied"

    for m in (BonusRequest, QuotaBonus, Quota):
        db.query(m).delete()
    db.delete(d)
    db.query(Person).delete()
    db.commit()


def _clear_family(db):
    from app.models import BonusRequest, Pause, Person, Quota, QuotaBonus, Rule

    for m in (BonusRequest, QuotaBonus, Quota, Rule, Pause, Device, Person):
        db.query(m).delete()
    db.commit()


def _kid_with_phone(db):
    from app.models import Person

    _clear_family(db)
    kid = Person(name="Миша", role="kid")
    db.add(kid)
    db.commit()
    phone = Device(mac="AA:00:00:00:00:51", ip="192.168.88.151", name="Телефон", person_id=kid.id)
    db.add(phone)
    db.commit()
    return kid, phone


def _waiting(db, device, status="pending"):
    from app.models import BonusRequest

    req = BonusRequest(device_id=device.id, category="internet", status=status)
    db.add(req)
    db.commit()
    return req


def _callback(data, text="🙏 Миша просит ещё времени"):
    from unittest.mock import AsyncMock, MagicMock

    from aiogram.types import Message

    cb = MagicMock(data=data, answer=AsyncMock())
    cb.message = MagicMock(spec=Message, text=text, edit_text=AsyncMock())
    return cb


def _edited(cb):
    """The text and button labels a callback redrew its message with."""
    call = cb.message.edit_text.call_args
    markup = call.kwargs["reply_markup"]
    return call.args[0], [b.text for row in markup.inline_keyboard for b in row]


def test_parents_read_the_request_without_addresses_or_numbers(db):
    """The journal line keeps the MAC and [req#N] for the linkage; the parent's text is rendered
    from the request: the child's name, the reason and what holds them back now."""
    from app.bot import main as bot_main
    from app.models import Person
    from app.services import bonus

    _clear_family(db)
    kid = Person(name="Миша", role="kid")
    db.add(kid)
    db.commit()
    dev = Device(mac="AA:00:00:00:00:41", ip="192.168.88.141", name="Телефон Миши",
                 person_id=kid.id)
    db.add(dev)
    db.commit()
    assert collect_notifications(db) == []  # инициализация курсора
    req = bonus.create_request(db, dev, "internet", "🎮 Доиграть или досмотреть")
    assert req is not None
    event = db.query(EventLog).filter(EventLog.kind == "bonus_request").one()
    assert dev.mac in event.message and f"[req#{req.id}]" in event.message  # linkage kept

    _latest, items = bot_main._collect_notifications()
    assert [(item[0], item[5]) for item in items] == [("bonus_request", req.id)]
    notice = items[0][7]
    assert notice is not None
    assert notice.text.startswith("🙏 Миша просит ещё времени\nПричина: 🎮 Доиграть или досмотреть")
    assert dev.mac not in notice.text and dev.ip not in notice.text and "req#" not in notice.text
    assert "Телефон" not in notice.text and "internet" not in notice.text
    _clear_family(db)


def test_one_child_switched_off_is_told_once(db):
    from app.bot import main as bot_main
    from app.models import Person

    _clear_family(db)
    kid = Person(name="Миша", role="kid")
    db.add(kid)
    db.commit()
    phone = Device(mac="AA:00:00:00:00:42", ip="192.168.88.142", name="Телефон", person_id=kid.id)
    tablet = Device(mac="AA:00:00:00:00:43", ip="192.168.88.143", name="Планшет", person_id=kid.id)
    db.add_all([phone, tablet])
    db.commit()
    assert collect_notifications(db) == []  # инициализация курсора
    for dev in (phone, tablet):
        log_event(db, "quota_block",
                  f"Квота на интернет исчерпана: {dev.name} ({dev.mac}, {dev.ip}) — "
                  "доступ выключен до полуночи.")
    _latest, items = bot_main._collect_notifications()
    assert len(items) == 1
    text = items[0][7].text
    assert text.startswith("⛔ Миша: лимит «Интернет целиком» исчерпан")
    assert phone.mac not in text and phone.ip not in text
    _clear_family(db)


def test_time_refused_at_night_keeps_the_request_and_its_buttons(db):
    import asyncio
    from datetime import datetime, timedelta
    from unittest.mock import AsyncMock, MagicMock

    from aiogram.types import Message

    from app.bot import alerts
    from app.models import BonusRequest, Person, Rule

    _clear_family(db)
    kid = Person(name="Миша", role="kid")
    db.add(kid)
    db.commit()
    dev = Device(mac="AA:00:00:00:00:44", ip="192.168.88.144", name="Телефон", person_id=kid.id)
    db.add(dev)
    now = datetime.now()
    db.add(Rule(name="Отбой", target_type="person", target=str(kid.id), days="0,1,2,3,4,5,6",
                start_time=f"{now - timedelta(hours=1):%H:%M}",
                end_time=f"{now + timedelta(hours=1):%H:%M}"))
    db.commit()
    req = BonusRequest(device_id=dev.id, category="internet", status="pending")
    db.add(req)
    db.commit()

    cb = MagicMock(data=f"br:{req.id}:30:t", answer=AsyncMock())
    cb.message = MagicMock(spec=Message, text="🙏 Миша просит ещё времени", edit_text=AsyncMock())
    asyncio.run(alerts.cb_bonus_request(cb))

    said = cb.answer.call_args
    assert "ночной режим" in said.args[0] and "Просьба ждёт ответа" in said.args[0]
    assert said.kwargs["show_alert"] is True
    cb.message.edit_text.assert_not_called()  # the buttons stay: the parent can still refuse
    db.expire_all()
    assert db.get(BonusRequest, req.id).status == "pending"
    _clear_family(db)


def test_allowing_with_nothing_to_add_records_no_minutes(db):
    """«👍 Разрешить» adds nothing, so Luna must not tell the child the internet was opened."""
    from app.bot import alerts
    from app.models import BonusRequest

    _kid, phone = _kid_with_phone(db)
    req = _waiting(db, phone)
    answer = alerts._resolve_bonus_request(req.id, "ok", "o")
    assert answer.final
    assert answer.text == "👍 Миша: ограничений сейчас нет, Luna передаст, что вы разрешили."
    db.expire_all()
    stored = db.get(BonusRequest, req.id)
    assert (stored.status, stored.minutes) == ("approved", 0)
    _clear_family(db)


def test_a_button_pressed_after_the_pause_ended_gives_nothing_and_asks_again(db):
    """«▶️ До конца паузы» pressed once the pause is over must not lift every limit for the day."""
    from app.bot import alerts
    from app.models import BonusRequest, Quota, QuotaBonus
    from app.services import time_notices

    kid, phone = _kid_with_phone(db)
    db.add(Quota(target_type="person", target=str(kid.id), category="games", minutes_per_day=60))
    db.commit()
    req = _waiting(db, phone)
    answer = alerts._resolve_bonus_request(req.id, "day", "p")
    assert not answer.final and answer.text == alerts.CHANGED
    assert answer.redraw is not None and answer.redraw.hold == time_notices.LIMITS
    assert db.query(QuotaBonus).count() == 0
    db.expire_all()
    assert db.get(BonusRequest, req.id).status == "pending"
    # buttons sent before the hold travelled with them are drawn again too
    assert alerts._resolve_bonus_request(req.id, "15").redraw is not None
    assert db.query(QuotaBonus).count() == 0
    _clear_family(db)


def test_allow_pressed_after_a_pause_started_does_not_open_it(db):
    import asyncio
    from datetime import datetime, timedelta

    from app.bot import alerts
    from app.models import Pause
    from app.services import unlock

    kid, phone = _kid_with_phone(db)
    req = _waiting(db, phone)
    db.add(Pause(target_type="person", target=str(kid.id),
                 until=datetime.now() + timedelta(hours=1)))
    db.commit()
    cb = _callback(f"br:{req.id}:ok:o")
    asyncio.run(alerts.cb_bonus_request(cb))
    assert unlock.open_until(db, kid.id) is None
    assert cb.answer.call_args.args[0] == alerts.CHANGED
    text, buttons = _edited(cb)
    assert "⏸ Сейчас пауза до" in text
    assert buttons == ["➕ 15 мин", "➕ 30 мин", "▶️ До конца паузы", "🚫 Отказать"]
    _clear_family(db)


def test_an_expired_request_does_not_say_somebody_answered(db):
    from app.bot import alerts

    _kid, phone = _kid_with_phone(db)
    req = _waiting(db, phone, status="expired")
    answer = alerts._resolve_bonus_request(req.id, "15", "l")
    assert answer.final
    assert answer.text == "Просьба закрылась сама: за сутки на неё никто не ответил."
    _clear_family(db)


def test_a_device_switched_on_from_under_the_request_shows_it_again(db):
    import asyncio

    from app.bot import alerts

    _kid, phone = _kid_with_phone(db)
    phone.blocked_manual = True
    db.commit()
    req = _waiting(db, phone)
    cb = _callback(f"bru:{req.id}:{phone.id}")
    asyncio.run(alerts.cb_switch_on_for_request(cb))
    db.expire_all()
    assert db.get(Device, phone.id).blocked_manual is False
    text, buttons = _edited(cb)
    assert text.startswith("🙏 Миша просит ещё времени") and "интернет открыт" in text
    assert buttons == ["👍 Разрешить", "🚫 Отказать"]
    _clear_family(db)


def test_saying_whose_device_asks_brings_the_time_buttons(db):
    import asyncio

    from app.bot import alerts
    from app.models import Person, Quota
    from app.services import time_notices

    _clear_family(db)
    kid = Person(name="Миша", role="kid")
    db.add(kid)
    db.commit()
    db.add(Quota(target_type="person", target=str(kid.id), category="games", minutes_per_day=60))
    tablet = Device(mac="AA:00:00:00:00:52", ip="192.168.88.152", name="Планшет")
    db.add(tablet)
    db.commit()
    req = _waiting(db, tablet)
    notice = time_notices.request_notice(db, req)
    assert notice.hold == time_notices.NO_OWNER
    owner_button = alerts.request_keyboard(req.id, notice).inline_keyboard[0][0]
    assert owner_button.text == "→ Миша"

    cb = _callback(owner_button.callback_data, notice.text)
    asyncio.run(alerts.cb_owner_for_request(cb))
    db.expire_all()
    assert db.get(Device, tablet.id).person_id == kid.id
    text, buttons = _edited(cb)
    assert text.startswith("🙏 Миша просит ещё времени")
    assert buttons == ["➕ 15 мин", "➕ 30 мин", "🌞 До конца дня", "🚫 Отказать"]
    _clear_family(db)


def test_a_grant_refused_at_night_keeps_the_alert_buttons(db):
    import asyncio
    from datetime import datetime, timedelta

    from app.bot import reports
    from app.models import Person, Quota, QuotaBonus, Rule

    _clear_family(db)
    kid = Person(name="Миша", role="kid")
    db.add(kid)
    db.commit()
    now = datetime.now()
    db.add(Quota(target_type="person", target=str(kid.id), category="games", minutes_per_day=60))
    db.add(Rule(name="Отбой", target_type="person", target=str(kid.id), days="0,1,2,3,4,5,6",
                start_time=f"{now - timedelta(hours=1):%H:%M}",
                end_time=f"{now + timedelta(hours=1):%H:%M}"))
    db.commit()
    cb = _callback(f"gt:{kid.id}:15:l", "⚠️ Миша: лимит «Игры» почти исчерпан (50 из 60 мин).")
    asyncio.run(reports.cb_grant(cb))
    assert "ночной режим" in cb.answer.call_args.args[0]
    assert cb.answer.call_args.kwargs["show_alert"] is True
    cb.message.edit_text.assert_not_called()  # the buttons stay
    assert db.query(QuotaBonus).count() == 0
    _clear_family(db)


def test_describe_pending_resolves_device(db):
    from app.ai.orchestrator import _describe_pending

    dev = Device(mac="AA:BB:CC:11:22:33", ip="192.168.88.45", name="Планшет Миши")
    db.add(dev)
    db.commit()

    desc = _describe_pending(db, "block_device", {"device_id": dev.id})
    assert "Планшет Миши" in desc
    assert "192.168.88.45" in desc


def test_handle_voice_without_gateway(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from app.bot.handlers import handle_voice
    from app.config import settings

    monkeypatch.setattr(settings, "ai_base_url", "")
    msg = MagicMock()
    msg.voice = MagicMock()
    msg.answer = AsyncMock()
    asyncio.run(handle_voice(msg))
    msg.answer.assert_called_once()
    assert "HS_AI_BASE_URL" in msg.answer.call_args[0][0]


def test_handle_voice_transcription_success(monkeypatch):
    import asyncio
    import io
    from unittest.mock import AsyncMock, MagicMock

    from app.bot import handlers
    from app.config import settings

    monkeypatch.setattr(settings, "ai_base_url", "http://lunagpt:4000")
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")

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
    mock_resp.json.return_value = {
        "choices": [{"message": {"content": "выключи интернет детям"}}]
    }
    sent = {}

    async def mock_post(self, url, **kwargs):
        sent.update(url=url, **kwargs)
        return mock_resp

    monkeypatch.setattr("httpx.AsyncClient.post", mock_post)

    processed_prompts = []

    async def mock_process(message, text):
        processed_prompts.append(text)

    monkeypatch.setattr(handlers, "_process_user_nl_prompt", mock_process)

    asyncio.run(handlers.handle_voice(msg))

    assert "выключи интернет детям" in processed_prompts
    assert msg.answer.called
    # Straight to the gateway with HomeSec's own key, never to LunaGPT core.
    assert sent["url"] == "http://lunagpt:4000/v1/chat/completions"
    assert sent["json"]["disable_fallbacks"] is True
    assert sent["headers"]["Authorization"] == "Bearer test-key"


def test_format_quota_and_keyboard():
    from app.bot.reports import quota_buttons_keyboard

    items = [{
        "device": "Планшет",
        "device_id": 42,
        "owner": "Миша",
        "owner_id": 7,
        "rows": [
            {"category": "games", "label": "Игры", "used": 60, "limit": 120},
            {"category": "internet", "label": "Интернет", "used": 150, "limit": 150},
        ],
    }]
    text = to_plain(screens.quota_html(items))
    assert "📱 Миша (Планшет)" in text
    assert "Игры · [████░░░░] 1:00/2:00 · 60 мин" in text
    assert "Интернет · [████████] 2:30/2:30 · ⛔ всё" in text

    kb = quota_buttons_keyboard(items)
    assert kb is not None
    buttons = {btn.callback_data: btn.text for row in kb.inline_keyboard for btn in row}
    assert buttons["gt:7:15"] == "➕ 15 мин · Миша" and "gt:7:30" in buttons  # per child


def test_format_usage():
    """The AI spend in money and plain words: no tokens, no gateway, no addresses."""
    spend = [("Миша", 0.25, 12, {"tutor": 0.2, "quiz": 0.05}), ("Папа", 0.5, 3, {})]
    text = to_plain(screens.usage_html(spend, 1.5, 40))
    assert "Миша · $0.25 · 12 · репетитор $0.20, викторина $0.05" in text
    assert "Итого · $0.75 · 15" in text
    assert "Дневной лимит ИИ: израсходовано 40%" in text
    assert "За 7 дней: $1.50" in text
    assert not re.search(r"[Тт]окен|[Шш]люз|\d+\.\d+\.\d+\.\d+", text)
    assert "исчерпан" in to_plain(screens.usage_html([], 0.0, 100))
    empty = to_plain(screens.usage_html([], 0.0))
    assert "ещё ничего не потратил" in empty and "лимит" not in empty


def test_keyboards_for_roles():
    from app.bot.common import keyboard_for_role

    kb_owner = keyboard_for_role("owner")
    assert kb_owner is not None
    owner_buttons = [btn.text for row in kb_owner.keyboard for btn in row]
    assert "🏠 Кто дома" in owner_buttons
    assert "📺 Общие устройства" in owner_buttons
    assert "📝 Заметки" in owner_buttons

    # children and guests have no menu here: their place is Luna
    assert keyboard_for_role("kid") is None and keyboard_for_role("guest") is None

    assert keyboard_for_role("unauthorized") is None


def test_access_request_flow(db):
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from aiogram.types import Message
    from sqlalchemy import select

    from app.bot.onboarding import access_request_keyboard, cb_access_request
    from app.models import Person

    kb = access_request_keyboard(123456)
    callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
    assert callbacks == ["acc:123456:admin", "acc:123456:deny"]  # parents only

    cb = MagicMock()
    cb.data = "acc:123456:admin"
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
    assert person.role == "adult"
    assert person.bot_role == "admin"

    msg.edit_text.assert_called_once()
    assert "Родитель" in msg.edit_text.call_args[0][0]
    cb.bot.send_message.assert_called_once()

    # a button from an old message offering "child" opens nothing, and the person is told
    cb.data = "acc:777:kid"
    cb.bot.send_message.reset_mock()
    asyncio.run(cb_access_request(cb))
    assert db.scalar(select(Person).where(Person.telegram_id == 777)) is None
    assert "только для родителей" in cb.bot.send_message.call_args[0][1]


def test_new_device_kind_assignment(db):
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from aiogram.types import Message

    from app.bot.alerts import cb_new_device, new_device_keyboard
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


def test_a_parent_gives_time_from_the_quota_report(db):
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from aiogram.types import Message
    from sqlalchemy import select

    from app.bot import reports
    from app.models import Person, Quota, QuotaBonus

    kid = Person(name="Коля", role="kid")
    db.add(kid)
    db.commit()
    db.add(Quota(name="Игры", target_type="person", target=str(kid.id), category="games",
                 minutes_per_day=60, days="0,1,2,3,4,5,6", enabled=True))
    db.commit()

    cb = MagicMock()
    cb.data = f"gt:{kid.id}:30"
    cb.answer = AsyncMock()
    msg = MagicMock(spec=Message)
    msg.text = "⚠️ Коля: лимит «Игры» почти исчерпан (50 из 60 мин), осталось 10 мин."
    msg.edit_text = AsyncMock()
    cb.message = msg

    asyncio.run(reports.cb_grant(cb))

    qb = db.scalar(select(QuotaBonus).where(QuotaBonus.target == str(kid.id)))
    assert (qb.minutes, qb.category) == (30, "games")  # the limit that exists, not "internet"
    assert "✅ Коля: +30 мин к лимитам: игры." in msg.edit_text.call_args[0][0]


def test_old_direct_request_buttons_say_they_are_outdated():
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from app.bot.alerts import cb_old_direct_request

    cb = MagicMock(data="bdir:1:30", answer=AsyncMock(), message=None)
    asyncio.run(cb_old_direct_request(cb))
    assert "Luna" in cb.answer.call_args[0][0]


def test_a_notice_nobody_received_is_sent_again(db, monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from app.bot import main as bot_main
    from app.bot import notify
    from app.models import Device, log_event

    assert collect_notifications(db) == []  # cursor starts at the end of the journal
    dev = Device(mac="AA:BB:CC:DD:EE:01", ip="192.168.88.77", name="Новый телефон")
    db.add(dev)
    db.commit()
    log_event(db, "device_new", "Новое устройство AA:BB:CC:DD:EE:01")
    monkeypatch.setattr(bot_main, "_parent_ids", lambda: set())
    monkeypatch.setattr(bot_main, "NOTIFY_INTERVAL", 0)

    async def one_round(send):
        bot = MagicMock(send_message=send)

        async def stop(_seconds):
            raise asyncio.CancelledError

        monkeypatch.setattr(bot_main.asyncio, "sleep", stop)
        try:
            await bot_main.notify_loop(bot, {1})
        except asyncio.CancelledError:
            pass

    asyncio.run(one_round(AsyncMock(side_effect=RuntimeError("no internet"))))
    assert len(notify.collect_notifications(db, advance=False)) == 1  # still waiting
    delivered = AsyncMock()
    asyncio.run(one_round(delivered))
    delivered.assert_awaited()
    assert notify.collect_notifications(db, advance=False) == []


def test_rich_html_escapes_what_people_type():
    from app.bot.rich import table

    assert "<p>" in screens.notes_html([])
    cell = table([("<b>Ваня & Co</b>", "*x*")])
    assert "&lt;b&gt;Ваня &amp; Co&lt;/b&gt;" in cell
    assert to_plain(cell) == "<b>Ваня & Co</b> · *x*"


def test_button_colours_mean_the_same_everywhere():
    from app.bot import alerts, reports

    def styles(markup):
        return {b.text: b.style for row in markup.inline_keyboard for b in row}

    request = styles(alerts.bonus_request_keyboard(1))
    assert request["🚫 Отказать"] == texts.DANGER and request["➕ 15 мин"] == texts.SUCCESS
    assert reports.grant_button(1, 15).style == texts.SUCCESS
    assert styles(alerts.new_device_keyboard(1, []))["⛔ Блокировать"] == texts.DANGER
