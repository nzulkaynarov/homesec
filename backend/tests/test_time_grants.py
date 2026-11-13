"""A parent's "yes" changes what really holds the child back."""

from datetime import datetime, timedelta

import pytest

from app.db import Base, engine, session
from app.models import EventLog, KVState, Pause, Person, Quota, QuotaBonus, Rule
from app.services import time_grants, unlock

NOON = datetime(2026, 11, 13, 12, 0)


@pytest.fixture
def db():
    Base.metadata.create_all(engine)
    s = session()
    for model in (EventLog, KVState, Pause, Quota, QuotaBonus, Rule, Person):
        s.query(model).delete()
    s.commit()
    yield s
    s.close()


def _kid(db) -> Person:
    kid = Person(name="Миша", role="kid", bot_role="kid")
    db.add(kid)
    db.commit()
    return kid


def test_limits_grow_by_the_minutes(db):
    kid = _kid(db)
    for category in ("games", "video"):
        db.add(Quota(name=category, target_type="person", target=str(kid.id), category=category,
                     minutes_per_day=60, days="0,1,2,3,4,5,6", enabled=True))
    db.commit()
    said = time_grants.grant(db, kid.id, 30, "тест", NOON)
    assert said.startswith("+30 мин к лимитам: игры, ")
    added = sorted((b.category, b.minutes) for b in db.query(QuotaBonus))
    assert added == [("games", 30), ("video", 30)]


def test_a_pause_opens_for_the_minutes(db):
    kid = _kid(db)
    now = datetime.now()
    db.add(Pause(target_type="person", target=str(kid.id), until=now + timedelta(hours=2)))
    db.commit()
    said = time_grants.grant(db, kid.id, 30, "тест")
    assert said.startswith("интернет открыт до") and "пауза продолжится" in said
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
    assert said == "ограничений сейчас нет, интернет и так открыт"
    assert db.query(QuotaBonus).count() == 0
