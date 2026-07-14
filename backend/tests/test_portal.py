"""Страница «время вышло»: определение причины по IP и публичный доступ."""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.db import Base, engine, session
from app.main import app
from app.models import Device, Pause, Person, Quota, QuotaUsage, Rule
from app.services.restrictions import restriction_for_ip


@pytest.fixture
def db():
    Base.metadata.create_all(engine)
    s = session()
    for model in (Quota, QuotaUsage, Pause, Rule, Device, Person):
        s.query(model).delete()
    s.commit()
    yield s
    s.close()


@pytest.fixture
def dev(db):
    kid = Person(name="Миша", role="kid")
    db.add(kid)
    db.commit()
    d = Device(mac="AA:00:00:00:00:01", ip="192.168.88.30", name="Планшет", person_id=kid.id)
    db.add(d)
    db.commit()
    return d


def test_no_restriction(db, dev):
    assert restriction_for_ip(db, "192.168.88.30") is None
    assert restriction_for_ip(db, "10.0.0.99") is None  # не наше устройство
    assert restriction_for_ip(db, "") is None


def test_manual_block(db, dev):
    dev.blocked_manual = True
    db.commit()
    assert restriction_for_ip(db, dev.ip)["kind"] == "manual"


def test_pause_reason_with_until(db, dev):
    until = datetime.now() + timedelta(minutes=30)
    db.add(Pause(target_type="group", target="kid", until=until))
    db.commit()
    info = restriction_for_ip(db, dev.ip)
    assert info["kind"] == "pause" and info["until"] == until


def test_quota_reason(db, dev):
    db.add(Quota(target_type="group", target="kid", category="internet", minutes_per_day=60))
    db.add(QuotaUsage(device_id=dev.id, date=datetime.now().strftime("%Y-%m-%d"),
                      category="internet", minutes=60))
    db.commit()
    info = restriction_for_ip(db, dev.ip)
    assert info["kind"] == "quota"
    assert info["quota"][0]["used"] >= info["quota"][0]["limit"]


def test_rule_reason_until(db, dev):
    now = datetime.now()
    db.add(Rule(name="Ночь", target_type="group", target="kid",
                days="0,1,2,3,4,5,6", start_time="00:00", end_time="23:59"))
    db.commit()
    info = restriction_for_ip(db, dev.ip)
    assert info["kind"] == "rule" and info["rule_name"] == "Ночь"
    assert info["until"] is not None and info["until"] > now


def test_unknown_reason(db, monkeypatch):
    db.add(Device(mac="BB:00:00:00:00:02", ip="192.168.88.55", name="mystery"))
    db.commit()
    monkeypatch.setattr(settings, "block_unknown", True)
    assert restriction_for_ip(db, "192.168.88.55")["kind"] == "unknown"
    monkeypatch.setattr(settings, "block_unknown", False)
    assert restriction_for_ip(db, "192.168.88.55") is None


def test_blocked_page_public_and_redirect(db):
    # TestClient приходит с request.client.host == "testclient"
    d = Device(mac="CC:00:00:00:00:03", ip="testclient", name="тест", blocked_manual=True)
    db.add(d)
    db.commit()
    with TestClient(app) as c:
        r = c.get("/blocked")  # публичная, без логина
        assert r.status_code == 200 and "Доступ выключен" in r.text
        # неавторизованный запрос с ограниченного устройства -> /blocked, не /login
        r = c.get("/", follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"] == "/blocked"
    d = db.get(Device, d.id)
    db.delete(d)
    db.commit()


def test_unrestricted_still_goes_to_login(db):
    with TestClient(app) as c:
        r = c.get("/", follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"] == "/login"
