"""Time requests are reminded and closed; parents get a weekly summary."""

from datetime import datetime, timedelta

import pytest

from app.db import Base, engine, session
from app.models import BonusRequest, Device, EventLog, KVState, Person, log_event
from app.services import followups

NOW = datetime(2026, 11, 15, 20, 10)  # a Sunday evening


@pytest.fixture
def db():
    Base.metadata.create_all(engine)
    s = session()
    for model in (EventLog, KVState, BonusRequest, Device, Person):
        s.query(model).delete()
    s.commit()
    yield s
    s.close()


def _request(db, minutes_ago: int, status: str = "pending", category: str = "games"):
    person = db.query(Person).first()
    if person is None:
        person = Person(name="Миша", role="kid", bot_role="kid")
        db.add(person)
        db.commit()
    device = db.query(Device).first()
    if device is None:
        device = Device(mac="00:11:22:33:44:01", ip="192.168.88.51", name="Телефон",
                        person_id=person.id, kind="personal")
        db.add(device)
        db.commit()
    req = BonusRequest(device_id=device.id, category=category, status=status,
                       created=NOW - timedelta(minutes=minutes_ago))
    db.add(req)
    db.commit()
    return req


def test_parents_are_reminded_once_after_an_hour(db):
    fresh = _request(db, 20)
    waiting = _request(db, 75)
    due = followups.due_reminders(db, NOW)
    assert [req.id for req, _ in due] == [waiting.id]
    assert "Миша (Телефон) ждёт ответа 75 мин" in due[0][1]
    assert followups.due_reminders(db, NOW) == []  # never twice
    assert fresh.status == "pending"


def test_a_request_nobody_answered_closes_after_a_day(db):
    old = _request(db, 25 * 60)
    recent = _request(db, 90)
    assert followups.expire_stale(db, NOW) == 1
    db.refresh(old)
    db.refresh(recent)
    assert old.status == "expired" and recent.status == "pending"


def test_weekly_summary_counts_what_happened(db):
    _request(db, 60 * 24 * 2, status="approved")
    _request(db, 60 * 24 * 3, status="denied")
    _request(db, 60 * 24 * 4, status="expired")
    _request(db, 60 * 24 * 10, status="approved")  # older than a week
    for kind, message in [("block", "Заблокирован доступ"), ("block", "Заблокирован доступ"),
                          ("bot_action", "Пауза: Телевизор до 15.11 21:00"),
                          ("device_new", "Новое устройство"), ("ip_manual", "Ручной IP")]:
        log_event(db, kind, message)
    for row in db.query(EventLog):
        row.ts = NOW - timedelta(days=1)
    db.commit()
    text = followups.weekly_summary(db, NOW)
    assert "08.11–15.11" in text
    assert "Отключений по правилам, паузам и лимитам: 2" in text
    assert "Пауз из бота: 1" in text
    assert "Просьб о времени: 3 — одобрено 1, отказано 1, без ответа 1" in text
    assert "Новых устройств: 1" in text and "адресом, заданным вручную: 1" in text


def test_weekly_summary_goes_out_once_on_sunday_evening(db):
    assert followups.weekly_due(db, NOW)
    assert not followups.weekly_due(db, NOW.replace(hour=19))
    assert not followups.weekly_due(db, NOW - timedelta(days=1))
    followups.mark_weekly_sent(db, NOW.date())
    assert not followups.weekly_due(db, NOW)
