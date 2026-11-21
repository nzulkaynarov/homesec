"""A child's device in use during bedtime is told to the parents once a night, and an idle
phone on its charger is not."""

from datetime import datetime, timedelta

import pytest

from app.db import Base, engine, session
from app.models import Device, KVState, Person, Rule
from app.services import adguard, night_watch

NIGHT = datetime(2026, 11, 21, 23, 15)  # Saturday


@pytest.fixture
def db():
    Base.metadata.create_all(engine)
    s = session()
    for model in (Device, Person, Rule, KVState):
        s.query(model).delete()
    s.commit()
    yield s
    s.close()


def _child(db, seen=NIGHT - timedelta(minutes=1)):
    kid = Person(name="Заурбек", role="kid")
    db.add(kid)
    db.flush()
    pc = Device(
        mac="B4:2E:99:FB:8E:8F",
        ip="192.168.88.239",
        name="Zaurbek",
        person_id=kid.id,
        kind="personal",
        last_seen=seen,
    )
    db.add(pc)
    db.add(
        Rule(
            name="Отбой пт–сб",
            target_type="person",
            target=str(kid.id),
            days="4,5",
            start_time="22:00",
            end_time="08:00",
            enabled=True,
        )
    )
    db.commit()
    return kid, pc


def _dns(monkeypatch, client, count, at=NIGHT):
    log = [
        {"client": client, "time": (at - timedelta(seconds=i)).isoformat()} for i in range(count)
    ]
    monkeypatch.setattr(adguard, "get_query_log", lambda limit=0: log)


def test_a_computer_in_use_at_night_is_told_once(db, monkeypatch):
    _, pc = _child(db)
    _dns(monkeypatch, pc.ip, 400)
    found = night_watch.awake_devices(db, NIGHT)
    assert len(found) == 1
    assert found[0].text.startswith("🌙 Заурбек: «Zaurbek» работает в 23:15, хотя отбой с 22:00.")
    assert "без интернета работают" in found[0].text
    night_watch.remember(db, found[0])
    assert night_watch.awake_devices(db, NIGHT + timedelta(minutes=30)) == []


def test_a_phone_asleep_on_the_charger_raises_nothing(db, monkeypatch):
    _, pc = _child(db)
    _dns(monkeypatch, pc.ip, 5)
    assert night_watch.awake_devices(db, NIGHT) == []


def test_nothing_in_the_first_minutes_of_bedtime_or_by_day(db, monkeypatch):
    _, pc = _child(db, seen=datetime(2026, 11, 21, 22, 5))
    _dns(monkeypatch, pc.ip, 400, at=datetime(2026, 11, 21, 22, 5))
    assert night_watch.awake_devices(db, datetime(2026, 11, 21, 22, 5)) == []
    _dns(monkeypatch, pc.ip, 400, at=datetime(2026, 11, 21, 15, 0))
    assert night_watch.awake_devices(db, datetime(2026, 11, 21, 15, 0)) == []


def test_a_device_gone_from_the_network_or_a_silent_adguard_raises_nothing(db, monkeypatch):
    _, pc = _child(db, seen=NIGHT - timedelta(hours=1))
    _dns(monkeypatch, pc.ip, 400)
    assert night_watch.awake_devices(db, NIGHT) == []

    pc.last_seen = NIGHT
    db.commit()

    def down(limit=0):
        raise OSError("AdGuard is down")

    monkeypatch.setattr(adguard, "get_query_log", down)
    assert night_watch.awake_devices(db, NIGHT) == []


def test_the_next_night_is_told_again(db, monkeypatch):
    _, pc = _child(db)
    _dns(monkeypatch, pc.ip, 400)
    night_watch.remember(db, night_watch.awake_devices(db, NIGHT)[0])
    sunday_night = NIGHT + timedelta(days=1)
    pc.last_seen = sunday_night
    db.add(
        Rule(
            name="Отбой вс–чт",
            target_type="person",
            target=str(pc.person_id),
            days="6,0,1,2,3",
            start_time="22:00",
            end_time="08:00",
            enabled=True,
        )
    )
    db.commit()
    _dns(monkeypatch, pc.ip, 400, at=sunday_night)
    assert len(night_watch.awake_devices(db, sunday_night)) == 1
