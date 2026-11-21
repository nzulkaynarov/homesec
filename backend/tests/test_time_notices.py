"""What a parent reads about a child's time: the child's name, the reason, what holds them back
now and what the buttons will do, without MAC, IP, request numbers or English words."""

import re
from datetime import datetime, timedelta

import pytest

from app.bot import alerts
from app.db import Base, engine, session
from app.models import (
    BonusRequest,
    Device,
    EventLog,
    KVState,
    Pause,
    Person,
    Quota,
    QuotaBonus,
    QuotaUsage,
    Rule,
)
from app.services import time_notices

NOW = datetime(2026, 11, 13, 12, 0)
TODAY = NOW.strftime("%Y-%m-%d")
EVERY_DAY = "0,1,2,3,4,5,6"
MAC = re.compile(r"[0-9A-F]{2}(?::[0-9A-F]{2}){5}", re.IGNORECASE)
TECHNICAL = ("req#", "internet", "pause", "pending", "approved", "games", "video", "quota")


@pytest.fixture
def db():
    Base.metadata.create_all(engine)
    s = session()
    models = (
        EventLog,
        KVState,
        BonusRequest,
        Pause,
        Quota,
        QuotaBonus,
        QuotaUsage,
        Rule,
        Device,
        Person,
    )
    for model in models:
        s.query(model).delete()
    s.commit()
    yield s
    for model in models:
        s.query(model).delete()
    s.commit()
    s.close()


@pytest.fixture
def kid(db):
    person = Person(name="Миша", role="kid", bot_role="kid")
    db.add(person)
    db.commit()
    db.add(
        Device(
            mac="00:11:22:33:44:01",
            ip="192.168.88.51",
            name="Телефон",
            person_id=person.id,
            kind="personal",
        )
    )
    db.commit()
    return person


def _device(db, person=None) -> Device:
    if person is None:
        return db.query(Device).filter(Device.person_id.is_(None)).one()
    return db.query(Device).filter(Device.person_id == person.id).one()


def _request(db, device: Device, reason: str = "") -> BonusRequest:
    req = BonusRequest(device_id=device.id, category="internet", reason=reason, status="pending")
    db.add(req)
    db.commit()
    return req


def _limit(db, kid, category: str, minutes: int, used: int) -> None:
    db.add(
        Quota(
            name=category,
            target_type="person",
            target=str(kid.id),
            category=category,
            minutes_per_day=minutes,
            days=EVERY_DAY,
            enabled=True,
        )
    )
    db.add(QuotaUsage(device_id=_device(db, kid).id, date=TODAY, category=category, minutes=used))
    db.commit()


def _readable(text: str, device: Device) -> None:
    assert not MAC.search(text)
    assert device.ip not in text
    for word in TECHNICAL:
        assert word not in text


def _buttons(markup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


def test_a_request_during_a_pause_names_the_child_and_the_reason(db, kid):
    db.add(Pause(target_type="person", target=str(kid.id), until=NOW + timedelta(hours=2)))
    db.commit()
    device = _device(db, kid)
    notice = time_notices.request_notice(db, _request(db, device, "📚 Доделать уроки"), NOW)

    assert notice.hold == time_notices.PAUSE
    assert notice.text.startswith("🙏 Миша просит ещё времени\nПричина: 📚 Доделать уроки")
    assert "⏸ Сейчас пауза до 14:00." in notice.text
    assert (
        "«➕ 15 мин» и «➕ 30 мин» откроют интернет поверх паузы, потом пауза продолжится; "
        "«▶️ До конца паузы» откроет до 14:00, когда пауза кончится." in notice.text
    )
    assert "Телефон" not in notice.text  # the child is named, not the device
    _readable(notice.text, device)
    buttons = _buttons(alerts.bonus_request_keyboard(1, notice.hold))
    assert buttons == ["➕ 15 мин", "➕ 30 мин", "▶️ До конца паузы", "🚫 Отказать"]


def test_a_request_shows_which_limit_is_used_up(db, kid):
    _limit(db, kid, "games", 60, 60)
    _limit(db, kid, "video", 90, 20)
    device = _device(db, kid)
    notice = time_notices.request_notice(db, _request(db, device, "Сделал дело: помыл посуду"), NOW)

    assert notice.hold == time_notices.LIMITS
    assert "Причина: Сделал дело: помыл посуду" in notice.text
    assert "• Игры: 60 из 60 мин — исчерпан" in notice.text
    assert "• YouTube и видео: 20 из 90 мин\n" in notice.text
    assert "«🌞 До конца дня» снимет лимиты до полуночи" in notice.text
    _readable(notice.text, device)
    assert "🌞 До конца дня" in _buttons(alerts.bonus_request_keyboard(1, notice.hold))


def test_at_night_the_parent_is_told_the_buttons_wait_for_the_morning(db, kid):
    db.add(
        Rule(
            name="Отбой",
            target_type="person",
            target=str(kid.id),
            days=EVERY_DAY,
            start_time="11:00",
            end_time="13:00",
        )
    )
    db.commit()
    notice = time_notices.request_notice(db, _request(db, _device(db, kid)), NOW)
    assert notice.hold == time_notices.BEDTIME
    assert "🌙 Сейчас ночной режим до 13:00." in notice.text
    assert "Отказать можно сразу, а после 13:00 нажмите «➕»" in notice.text
    assert "Причина" not in notice.text  # no reason, no empty line for it


def test_with_nothing_to_add_to_the_parent_only_allows_or_refuses(db, kid):
    notice = time_notices.request_notice(db, _request(db, _device(db, kid), "Просто прошу"), NOW)
    assert notice.hold == time_notices.OPEN
    assert "Сейчас ни паузы, ни лимитов: интернет открыт." in notice.text
    assert _buttons(alerts.bonus_request_keyboard(1, notice.hold)) == [
        "👍 Разрешить",
        "🚫 Отказать",
    ]


def test_a_device_without_an_owner_asks_whose_it_is(db, kid):
    device = Device(mac="00:11:22:33:44:09", ip="192.168.88.59", name="Планшет")
    db.add(device)
    db.commit()
    notice = time_notices.request_notice(db, _request(db, device), NOW)
    assert notice.hold == time_notices.NO_OWNER
    assert notice.text.startswith("🙏 Устройство «Планшет» просит ещё времени")
    assert "Чьё оно? Выберите ниже" in notice.text and "/devices" not in notice.text
    _readable(notice.text, device)
    assert notice.people == ((kid.id, "Миша"),)
    keyboard = alerts.request_keyboard(1, notice)
    assert _buttons(keyboard) == ["→ Миша", "🚫 Отказать"]
    assert keyboard.inline_keyboard[0][0].callback_data == f"bro:1:{kid.id}"


def test_with_nobody_in_the_family_a_device_without_an_owner_can_only_be_refused(db):
    device = Device(mac="00:11:22:33:44:09", ip="192.168.88.59", name="Планшет")
    db.add(device)
    db.commit()
    notice = time_notices.request_notice(db, _request(db, device), NOW)
    assert "можно только отказать" in notice.text
    assert _buttons(alerts.request_keyboard(1, notice)) == ["🚫 Отказать"]


def test_a_limit_almost_used_up(db, kid):
    _limit(db, kid, "games", 60, 50)
    device = _device(db, kid)
    notice = time_notices.quota_notice(db, device, "quota_warn", "games", NOW)
    assert notice.text.startswith(
        "⚠️ Миша: лимит «Игры» почти исчерпан (50 из 60 мин), осталось 10 мин."
    )
    assert "добавят минуты к каждому лимиту на сегодня" in notice.text
    _readable(notice.text, device)
    keyboard = alerts.bonus_keyboard(kid.id, notice.hold)
    assert keyboard is not None and _buttons(keyboard) == ["➕ 15 мин", "➕ 30 мин"]


def test_the_internet_limit_used_up(db, kid):
    _limit(db, kid, "internet", 120, 120)
    device = _device(db, kid)
    notice = time_notices.quota_notice(db, device, "quota_block", "internet", NOW)
    assert notice.text.startswith(
        "⛔ Миша: лимит «Интернет целиком» исчерпан (120 из 120 мин), "
        "интернет выключен до полуночи."
    )
    _readable(notice.text, device)


def _group_limit(db, kid, category: str, minutes: int, used: int) -> None:
    """A limit the panel sets: on the children's group, not on the child."""
    db.add(
        Quota(
            target_type="group",
            target="kid",
            category=category,
            minutes_per_day=minutes,
            days=EVERY_DAY,
            enabled=True,
        )
    )
    db.add(QuotaUsage(device_id=_device(db, kid).id, date=TODAY, category=category, minutes=used))
    db.commit()


def test_a_group_limit_from_the_panel_gets_buttons_that_reach_it(db, kid):
    _group_limit(db, kid, "games", 60, 50)
    notice = time_notices.quota_notice(db, _device(db, kid), "quota_warn", "games", NOW)
    assert notice.hold == time_notices.LIMITS
    assert "не личный лимит" not in notice.text
    assert "добавят минуты к каждому лимиту на сегодня" in notice.text
    keyboard = alerts.bonus_keyboard(kid.id, notice.hold)
    assert keyboard is not None
    assert [b.callback_data for b in keyboard.inline_keyboard[0]] == [
        f"gt:{kid.id}:15:l",
        f"gt:{kid.id}:30:l",
    ]


def test_a_used_up_group_limit_is_not_called_nothing_to_add_to(db, kid):
    """The panel's «Игры детям» is used up and the child has no limit of their own: the parent
    reads the limit and gets buttons that add to it, not «Разрешить» over an open internet."""
    _group_limit(db, kid, "games", 120, 120)
    device = _device(db, kid)
    notice = time_notices.request_notice(db, _request(db, device, "🎮 Доиграть"), NOW)
    assert notice.hold == time_notices.LIMITS
    assert "• Игры: 120 из 120 мин — исчерпан" in notice.text
    assert "добавлять время не к чему" not in notice.text
    assert "интернет открыт" not in notice.text
    assert _buttons(alerts.request_keyboard(1, notice)) == [
        "➕ 15 мин",
        "➕ 30 мин",
        "🌞 До конца дня",
        "🚫 Отказать",
    ]


def test_a_device_switched_off_by_hand_is_named_with_a_way_to_switch_it_on(db, kid):
    device = _device(db, kid)
    device.blocked_manual = True
    db.commit()
    notice = time_notices.request_notice(db, _request(db, device), NOW)
    assert notice.hold == time_notices.BLOCKED
    assert "⛔ Выключено вручную: «Телефон»." in notice.text
    assert "интернет открыт" not in notice.text
    keyboard = alerts.request_keyboard(7, notice)
    assert _buttons(keyboard) == ["✅ Включить «Телефон»", "🚫 Отказать"]
    assert keyboard.inline_keyboard[0][0].callback_data == f"bru:7:{device.id}"
    _readable(notice.text, device)


def test_a_device_switched_off_by_hand_next_to_a_limit_keeps_both_ways(db, kid):
    _limit(db, kid, "games", 60, 60)
    device = _device(db, kid)
    device.blocked_manual = True
    db.commit()
    notice = time_notices.request_notice(db, _request(db, device), NOW)
    assert notice.hold == time_notices.LIMITS
    assert "его можно включить кнопкой ниже" in notice.text
    assert _buttons(alerts.request_keyboard(1, notice)) == [
        "➕ 15 мин",
        "➕ 30 мин",
        "✅ Включить «Телефон»",
        "🌞 До конца дня",
        "🚫 Отказать",
    ]


def test_a_limit_alert_at_night_has_no_buttons_and_says_when_time_can_be_added(db, kid):
    _limit(db, kid, "games", 60, 50)
    db.add(
        Rule(
            name="Отбой",
            target_type="person",
            target=str(kid.id),
            days=EVERY_DAY,
            start_time="11:00",
            end_time="13:00",
        )
    )
    db.commit()
    notice = time_notices.quota_notice(db, _device(db, kid), "quota_warn", "games", NOW)
    assert notice.hold == time_notices.BEDTIME
    assert "Ночью время не добавляется. После 13:00 его можно добавить в «⏳ Квоты»." in notice.text
    assert "кнопки сработают" not in notice.text
    assert alerts.bonus_keyboard(kid.id, notice.hold) is None


def test_a_limit_alert_before_midnight_says_the_limits_start_over(db, kid):
    _limit(db, kid, "games", 60, 50)
    db.add(
        Rule(
            name="Отбой",
            target_type="person",
            target=str(kid.id),
            days=EVERY_DAY,
            start_time="21:00",
            end_time="07:00",
        )
    )
    db.commit()
    late = NOW.replace(hour=22)
    notice = time_notices.quota_notice(db, _device(db, kid), "quota_warn", "games", late)
    assert "в полночь лимиты начнутся заново" in notice.text
    assert alerts.bonus_keyboard(kid.id, notice.hold) is None


def test_a_limit_alert_during_a_pause_adds_to_the_limit_and_leaves_the_pause(db, kid):
    _limit(db, kid, "games", 60, 50)
    db.add(Pause(target_type="person", target=str(kid.id), until=NOW + timedelta(hours=2)))
    db.commit()
    notice = time_notices.quota_notice(db, _device(db, kid), "quota_warn", "games", NOW)
    assert notice.hold == time_notices.PAUSE
    assert "добавят минуты к каждому лимиту на сегодня, а пауза до 14:00 останется" in notice.text
    assert "поверх паузы" not in notice.text
    keyboard = alerts.bonus_keyboard(kid.id, notice.hold)
    assert keyboard is not None and keyboard.inline_keyboard[0][0].callback_data.endswith(":l")


def test_a_limit_lifted_before_the_alert_went_out_gets_no_buttons(db, kid):
    _limit(db, kid, "video", 60, 10)
    notice = time_notices.quota_notice(db, _device(db, kid), "quota_warn", "games", NOW)
    assert notice.hold == time_notices.OPEN
    assert "Этого лимита сегодня уже нет" in notice.text
    assert alerts.bonus_keyboard(kid.id, notice.hold) is None


def test_with_a_window_open_the_buttons_extend_it(db, kid):
    db.add(Pause(target_type="person", target=str(kid.id), until=NOW + timedelta(hours=3)))
    db.commit()
    db.add(KVState(key=f"unlock:{kid.id}", value=f"{NOW + timedelta(hours=2):%Y-%m-%dT%H:%M:%S}"))
    db.commit()
    notice = time_notices.request_notice(db, _request(db, _device(db, kid)), NOW)
    assert "Интернет уже открыт родителем до 14:00." in notice.text
    assert "«➕ 15 мин» и «➕ 30 мин» продлят открытое время" in notice.text
