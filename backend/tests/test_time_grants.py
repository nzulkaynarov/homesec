"""A parent's "yes" changes what really holds the child back."""

from datetime import datetime, timedelta

import pytest

from app.db import Base, engine, session
from app.models import (
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
from app.services import quota, time_grants, unlock

NOON = datetime(2026, 11, 13, 12, 0)
EVERY_DAY = "0,1,2,3,4,5,6"


@pytest.fixture
def db():
    Base.metadata.create_all(engine)
    s = session()
    models = (EventLog, KVState, Pause, Quota, QuotaBonus, QuotaUsage, Rule, Device, Person)
    for model in models:
        s.query(model).delete()
    s.commit()
    yield s
    for model in models:
        s.query(model).delete()
    s.commit()
    s.close()


def _kid(db) -> Person:
    kid = Person(name="Миша", role="kid", bot_role="kid")
    db.add(kid)
    db.commit()
    return kid


def _phone(db, kid: Person) -> Device:
    device = Device(mac="00:11:22:33:44:51", ip="192.168.88.51", name="Телефон", person_id=kid.id)
    db.add(device)
    db.commit()
    return device


def _used(db, device: Device, category: str, minutes: int, day: datetime) -> None:
    db.add(QuotaUsage(device_id=device.id, date=f"{day:%Y-%m-%d}", category=category,
                      minutes=minutes))
    db.commit()


def test_limits_grow_by_the_minutes(db):
    kid = _kid(db)
    for category in ("games", "video"):
        db.add(Quota(name=category, target_type="person", target=str(kid.id), category=category,
                     minutes_per_day=60, days="0,1,2,3,4,5,6", enabled=True))
    db.commit()
    said = time_grants.grant(db, kid.id, 30, "тест", NOON)
    assert said.text.startswith("+30 мин к лимитам: игры, ") and said.minutes == 30
    added = sorted((b.category, b.minutes) for b in db.query(QuotaBonus))
    assert added == [("games", 30), ("video", 30)]


def test_a_pause_opens_for_the_minutes(db):
    kid = _kid(db)
    now = datetime.now()
    db.add(Pause(target_type="person", target=str(kid.id), until=now + timedelta(hours=2)))
    db.commit()
    said = time_grants.grant(db, kid.id, 30, "тест")
    assert said.text.startswith("интернет открыт до") and "пауза продолжится" in said.text
    assert unlock.open_until(db, kid.id) is not None
    assert db.query(QuotaBonus).count() == 0


def test_nothing_is_added_at_night(db):
    kid = _kid(db)
    now = datetime.now()
    start = (now - timedelta(minutes=30)).strftime("%H:%M")
    end = (now + timedelta(hours=2)).strftime("%H:%M")
    db.add(Rule(name="Отбой", target_type="person", target=str(kid.id), days="0,1,2,3,4,5,6",
                start_time=start, end_time=end, enabled=True))
    db.commit()
    with pytest.raises(time_grants.GrantError, match="ночной режим"):
        time_grants.grant(db, kid.id, 30, "тест")


def test_without_restrictions_it_says_so_and_adds_nothing(db):
    kid = _kid(db)
    said = time_grants.grant(db, kid.id, 30, "тест", NOON)
    assert said == time_grants.Granted("ограничений сейчас нет, интернет и так открыт", 0)
    assert db.query(QuotaBonus).count() == 0


def test_a_limit_set_in_the_panel_grows_too(db):
    """The panel sets only group and device limits, and the internet one switches the child
    off: a grant has to reach it, not only the limits set for the child in the bot."""
    kid = _kid(db)
    phone = _phone(db, kid)
    db.add(Quota(target_type="group", target="kid", category="internet", minutes_per_day=120,
                 days=EVERY_DAY))
    db.add(Quota(name="Игры", target_type="person", target=str(kid.id), category="games",
                 minutes_per_day=60, days=EVERY_DAY))
    db.commit()
    _used(db, phone, "internet", 120, NOON)
    assert quota.exhausted(db, [phone], NOON) == {phone.id: {"internet"}}

    said = time_grants.grant(db, kid.id, 15, "тест", NOON)
    assert said == time_grants.Granted("+15 мин к лимитам: интернет целиком, игры", 15)
    assert quota.exhausted(db, [phone], NOON) == {}


def test_a_device_limit_grows_too(db):
    kid = _kid(db)
    phone = _phone(db, kid)
    db.add(Quota(target_type="device", target=str(phone.id), category="video",
                 minutes_per_day=30, days=EVERY_DAY))
    db.commit()
    _used(db, phone, "video", 30, NOON)
    said = time_grants.grant_limits(db, kid.id, 30, "тест", NOON)
    assert said.text == "+30 мин к лимитам: youtube и видео"
    assert quota.exhausted(db, [phone], NOON) == {}


def test_more_time_never_shortens_an_open_window(db):
    """A parent opened the pause until in two hours; «+15» later adds to that, not takes away."""
    kid = _kid(db)
    now = datetime.now()
    db.add(Pause(target_type="person", target=str(kid.id), until=now + timedelta(hours=3)))
    db.commit()
    unlock.open_window(db, kid.id, now + timedelta(hours=2))
    said = time_grants.grant(db, kid.id, 15, "тест")
    opened = unlock.open_until(db, kid.id)
    assert opened is not None and opened >= now + timedelta(hours=2, minutes=14)
    assert "потом пауза продолжится" in said.text


def test_until_the_end_of_the_pause_nothing_continues_after_it(db):
    kid = _kid(db)
    until = datetime.now() + timedelta(hours=2)
    db.add(Pause(target_type="person", target=str(kid.id), until=until))
    db.commit()
    said = time_grants.grant(db, kid.id, time_grants.WHOLE_DAY, "тест")
    assert said.text == f"интернет открыт до {until:%H:%M}, до конца паузы"
    opened = unlock.open_until(db, kid.id)
    assert opened is not None and abs(opened - until) < timedelta(seconds=1)


def test_the_buttons_under_a_limit_alert_add_to_the_limits_and_leave_the_pause(db):
    kid = _kid(db)
    _phone(db, kid)
    db.add(Quota(name="Игры", target_type="person", target=str(kid.id), category="games",
                 minutes_per_day=60, days=EVERY_DAY))
    until = datetime.now() + timedelta(hours=2)
    db.add(Pause(target_type="person", target=str(kid.id), until=until))
    db.commit()
    said = time_grants.grant_limits(db, kid.id, 15, "тест")
    assert said.text == f"+15 мин к лимитам: игры; пауза до {until:%H:%M} остаётся"
    assert unlock.open_until(db, kid.id) is None
    assert [(b.category, b.minutes) for b in db.query(QuotaBonus)] == [("games", 15)]


def test_the_buttons_under_a_limit_alert_add_nothing_at_night(db):
    kid = _kid(db)
    db.add(Quota(name="Игры", target_type="person", target=str(kid.id), category="games",
                 minutes_per_day=60, days=EVERY_DAY))
    db.add(Rule(name="Отбой", target_type="person", target=str(kid.id), days=EVERY_DAY,
                start_time="11:00", end_time="13:00", enabled=True))
    db.commit()
    with pytest.raises(time_grants.GrantError, match="ночной режим до 13:00"):
        time_grants.grant_limits(db, kid.id, 15, "тест", NOON)
    assert db.query(QuotaBonus).count() == 0


def test_a_device_switched_off_by_hand_is_not_called_open(db):
    kid = _kid(db)
    phone = _phone(db, kid)
    phone.blocked_manual = True
    db.commit()
    said = time_grants.grant(db, kid.id, 30, "тест", NOON)
    assert said.minutes == 0
    assert "выключено вручную: «Телефон»" in said.text and "открыт" not in said.text
