"""House rules from Telegram buttons: bedtime, limits, pauses, people and parents."""

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import Message

from app.ai import tools
from app.bot import access, device_menu, handlers, rules_menu
from app.db import Base, engine, session
from app.models import Device, EventLog, KVState, Pause, Person, Quota, Rule
from app.services import enforcement, family, house_rules

CHAT, USER = 200, 9


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr(tools.enforcement, "get_self_ips", lambda: {"192.168.88.2"})
    monkeypatch.setattr(device_menu, "vendor_of", AsyncMock(return_value=None))
    Base.metadata.create_all(engine)
    s = session()
    for model in (EventLog, KVState, Pause, Quota, Rule, Device, Person):
        s.query(model).delete()
    s.commit()
    rules_menu._typing.clear()
    yield s
    s.close()


def _kid(db, name="Алимбек") -> Person:
    person = Person(name=name, role="kid", bot_role="kid")
    db.add(person)
    db.commit()
    return person


def _press(data: str) -> MagicMock:
    cb = MagicMock(data=data, answer=AsyncMock())
    cb.from_user.id = USER
    cb.message = MagicMock(spec=Message, edit_text=AsyncMock())
    cb.message.chat = MagicMock(id=CHAT)
    return cb


def _run(handler, data: str) -> tuple[str, list[str]]:
    cb = _press(data)
    asyncio.run(handler(cb))
    call = cb.message.edit_text.await_args
    return call.args[0], [
        b.callback_data for row in call.kwargs["reply_markup"].inline_keyboard for b in row
    ]


def _typed(text: str) -> MagicMock:
    message = MagicMock(spec=Message, text=text, answer=AsyncMock())
    message.chat = MagicMock(id=CHAT)
    message.from_user = MagicMock(id=USER)
    return message


def test_bedtime_buttons_block_the_childs_devices_at_night(db):
    kid = _kid(db)
    phone = Device(mac="00:11:22:33:44:01", ip="192.168.88.50", name="Redmi", person_id=kid.id)
    db.add(phone)
    db.commit()
    _, actions = _run(rules_menu.cb_bedtime, f"hrb:{kid.id}:school")
    assert f"hrbs:{kid.id}:school:2130" in actions
    _, actions = _run(rules_menu.cb_bedtime_start, f"hrbs:{kid.id}:school:2130")
    assert f"hrbe:{kid.id}:school:2130:0700" in actions
    text, _ = _run(rules_menu.cb_bedtime_save, f"hrbe:{kid.id}:school:2130:0700")
    assert "✅ Отбой вс–чт: 21:30–07:00" in text

    rule = db.query(Rule).one()
    assert (rule.target_type, rule.target, rule.days) == ("person", str(kid.id), "6,0,1,2,3")
    sunday_night = datetime(2026, 11, 8, 22, 0)  # a Sunday
    monday_morning = datetime(2026, 11, 9, 6, 30)
    friday_night = datetime(2026, 11, 13, 22, 0)
    assert enforcement.rule_is_active(rule, sunday_night)
    assert enforcement.rule_is_active(rule, monday_morning)
    assert not enforcement.rule_is_active(rule, friday_night)

    db.expire_all()
    blocked = enforcement._desired_state(db, [db.get(Device, phone.id)])["lists"]["hs-blocked"]
    assert ("192.168.88.50" in blocked) == enforcement.rule_is_active(rule, datetime.now())


def test_bedtime_can_be_changed_and_switched_off(db):
    kid = _kid(db)
    house_rules.set_bedtime(db, kid.id, "weekend", "22:30", "09:00")
    house_rules.set_bedtime(db, kid.id, "weekend", "23:00", "10:00")
    rule = db.query(Rule).one()
    assert (rule.start_time, rule.end_time) == ("23:00", "10:00")
    _run(rules_menu.cb_bedtime_off, f"hrbx:{kid.id}:weekend")
    assert db.query(Rule).count() == 0


def test_limit_is_one_per_person_and_category(db):
    kid = _kid(db)
    text, _ = _run(rules_menu.cb_limit_save, f"hrqs:{kid.id}:games:60")
    assert "✅ Игры: 60 мин в день" in text
    _run(rules_menu.cb_limit_save, f"hrqs:{kid.id}:games:90")
    quota = db.query(Quota).one()
    assert (quota.target_type, quota.target, quota.minutes_per_day) == ("person", str(kid.id), 90)
    _run(rules_menu.cb_limit_save, f"hrqs:{kid.id}:games:0")
    assert db.query(Quota).count() == 0
    with pytest.raises(house_rules.RuleError):
        house_rules.set_limit(db, kid.id, "internet", 60)


def test_pause_until_morning_follows_the_bedtime(db):
    kid = _kid(db)
    house_rules.set_bedtime(db, kid.id, "school", "21:30", "06:30")
    sunday_evening = datetime(2026, 11, 8, 20, 0)
    assert house_rules.wake_time(db, kid.id, sunday_evening) == datetime(2026, 11, 9, 6, 30)
    friday_evening = datetime(2026, 11, 13, 20, 0)
    assert house_rules.wake_time(db, kid.id, friday_evening) == datetime(2026, 11, 14, 7, 0)
    text, actions = _run(rules_menu.cb_pause, f"hrz:{kid.id}:m")
    assert "⏸ Пауза до" in text and f"hrr:{kid.id}" in actions
    assert house_rules.paused_until(db, kid.id) is not None
    _run(rules_menu.cb_resume, f"hrr:{kid.id}")
    assert house_rules.paused_until(db, kid.id) is None


def test_pause_all_kids_skips_adults(db):
    kid = _kid(db)
    db.add(Person(name="Камила", role="adult"))
    db.commit()
    text, _ = _run(rules_menu.cb_pause_all, "hrza")
    assert "детей 1" in text
    assert [p.target for p in db.query(Pause)] == [str(kid.id)]


def test_people_are_added_renamed_and_removed_from_the_bot(db):
    _run(rules_menu.cb_add, "hrla:kid")
    message = _typed("Алимбек")
    assert rules_menu.waiting_for_text(message)
    asyncio.run(rules_menu.on_text(message))
    kid = db.query(Person).one()
    assert (kid.name, kid.role) == ("Алимбек", "kid")

    _run(rules_menu.cb_rename, f"hrln:{kid.id}")
    asyncio.run(rules_menu.on_text(_typed("Алим")))
    db.expire_all()
    assert db.get(Person, kid.id).name == "Алим"

    tablet = Device(mac="00:11:22:33:44:02", ip="", name="iPad", person_id=kid.id)
    db.add(tablet)
    db.commit()
    house_rules.set_bedtime(db, kid.id, "school", "21:00", "07:00")
    text, actions = _run(rules_menu.cb_delete_confirm, f"hrld:{kid.id}")
    assert "без владельца" in text and f"hrldy:{kid.id}" in actions
    _run(rules_menu.cb_delete, f"hrldy:{kid.id}")
    db.expire_all()
    assert db.query(Person).count() == 0 and db.query(Rule).count() == 0
    assert db.get(Device, tablet.id).person_id is None


def test_duplicate_name_is_refused(db):
    _kid(db)
    with pytest.raises(house_rules.RuleError):
        house_rules.add_person(db, "Алимбек", "kid")


def test_owner_cannot_be_deleted_or_made_a_kid(db):
    owner = Person(name="Нурбек", role="adult", bot_role="owner", telegram_id=1)
    db.add(owner)
    db.commit()
    with pytest.raises(house_rules.RuleError):
        house_rules.delete_person(db, owner.id)
    with pytest.raises(house_rules.RuleError):
        house_rules.set_role(db, owner.id, "kid")


def test_second_parent_gets_notifications_once_linked(db, monkeypatch):
    monkeypatch.setattr(family.settings, "telegram_chat_ids", "111")
    mom = Person(name="Камила", role="adult")
    db.add(mom)
    db.commit()
    assert family.parent_chat_ids(db) == {111}
    _, role = handlers._link_telegram_sync(222, mom.id)
    assert role == "admin"
    db.expire_all()
    assert family.parent_chat_ids(db) == {111, 222}
    assert family.get_user_role(db, 222)[0] == "admin"


def test_access_request_offers_people_without_telegram():
    kb = handlers.access_request_keyboard(555, [(3, "Камила")])
    callbacks = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert callbacks[0] == "acc:555:link:3" and "acc:555:admin" in callbacks


def test_speed_preset_from_the_device_card(db):
    device = Device(mac="00:11:22:33:44:03", ip="192.168.88.60", name="PlayStation")
    db.add(device)
    db.commit()
    _, actions = _run(device_menu.cb_speed_pick, f"dmv:{device.id}:none:0")
    assert f"dmvs:{device.id}:1:none:0" in actions
    text, _ = _run(device_menu.cb_speed, f"dmvs:{device.id}:1:none:0")
    db.expire_all()
    assert db.get(Device, device.id).speed_limit == "5M/5M" and "🐢 Скорость: 5M/5M" in text
    _run(device_menu.cb_speed, f"dmvs:{device.id}:-1:none:0")
    db.expire_all()
    assert db.get(Device, device.id).speed_limit == ""


@pytest.mark.parametrize("role", ["kid", "guest"])
def test_children_cannot_open_the_rules(role):
    assert not access.check_message(role, rules_menu.BTN_RULES).allowed
    assert not access.check_callback(role, "hrqs:1:games:0").allowed


def test_rules_menu_sits_behind_the_access_middleware():
    callbacks = {h.callback for h in handlers.router.callback_query.handlers}
    assert {rules_menu.cb_limit_save, rules_menu.cb_delete} <= callbacks


def test_callback_data_fits_telegram_limit():
    assert len(f"hrbe:{10**9}:weekend:2330:1000".encode()) <= 64
    assert len(f"dmvs:{10**9}:3:p{10**9}:999".encode()) <= 64


def test_group_pause_from_the_ai_shows_up_and_can_be_lifted(db):
    from datetime import timedelta

    kid = _kid(db)
    until = datetime.now() + timedelta(hours=8)
    db.add(Pause(target_type="group", target="kid", until=until, reason="Ночной сон"))
    db.commit()
    text, actions = _run(rules_menu.cb_overview, "hr")
    assert f"пауза всем детям до {until:%H:%M}" in text and "hrra" in actions
    assert f"⏸ Пауза до {until:%H:%M}" in text  # the child's own summary shows it too
    text, _ = _run(rules_menu.cb_resume, f"hrr:{kid.id}")
    assert "пауза всем детям" in text  # a group pause is not lifted for one child
    text, actions = _run(rules_menu.cb_resume_kids, "hrra")
    assert "Снято пауз: 1" in text and "hrra" not in actions
    assert house_rules.paused_until(db, kid.id) is None


def test_device_pause_counts_for_its_owner(db):
    from datetime import timedelta

    kid = _kid(db)
    phone = Device(mac="00:11:22:33:44:09", ip="192.168.88.90", name="iPhone", person_id=kid.id)
    db.add(phone)
    db.commit()
    db.add(
        Pause(target_type="device", target=str(phone.id), until=datetime.now() + timedelta(hours=1))
    )
    db.commit()
    assert house_rules.paused_until(db, kid.id) is not None
    house_rules.resume(db, kid.id)
    assert house_rules.paused_until(db, kid.id) is None
