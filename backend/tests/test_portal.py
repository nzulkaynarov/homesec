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


def test_intercepted_http_redirected_to_panel(db):
    # NAT-перехват enable-block-page.rsc: Host — чужой сайт, а настоящий IP
    # клиента скрыт hairpin-masquerade (устройства с таким IP в базе нет).
    # Ответ — абсолютный редирект на прямой адрес панели: прямое соединение
    # придёт с настоящим IP, и уже оно разведётся на /blocked или /register.
    with TestClient(app) as c:
        r = c.get("/", headers={"host": "neverssl.com"}, follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == settings.panel_lan_url.rstrip("/") + "/"
        # заход по адресу панели (Host свой) — обычный /login, не перехват
        r = c.get("/", headers={"host": "testserver:8000"}, follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"] == "/login"


# ---------- портал регистрации ----------

def test_register_flow_and_cooldown(db):
    from app.models import EventLog, RegistrationRequest

    db.query(RegistrationRequest).delete()
    db.query(EventLog).delete()
    dev = Device(mac="DD:00:00:00:00:04", ip="testclient", name="новичок")
    db.add(dev)
    db.commit()
    with TestClient(app) as c:
        assert "Представьтесь" not in c.get("/blocked").text  # разные страницы
        r = c.get("/register")
        assert r.status_code == 200 and "заявку" in r.text

        r = c.post("/register", data={"name": "Бабушка", "comment": "мой планшет"})
        assert "Заявка отправлена" in r.text
        req = db.query(RegistrationRequest).one()
        assert req.device_id == dev.id and req.name == "Бабушка"
        ev = [e for e in db.query(EventLog) if e.kind == "register_request"]
        assert len(ev) == 1 and dev.mac in ev[0].message and "Бабушка" in ev[0].message

        # антиспам: повторная заявка раньше чем через 10 минут не создаётся
        r = c.post("/register", data={"name": "Ещё раз"})
        assert "уже отправлена" in r.text
        assert db.query(RegistrationRequest).count() == 1
    db.delete(dev)
    db.query(RegistrationRequest).delete()
    db.commit()


def test_register_for_known_device(db, dev):
    # у устройства с владельцем портал сообщает «уже зарегистрировано»
    d2 = Device(mac="EE:00:00:00:00:05", ip="testclient", name="своё",
                person_id=dev.person_id)
    db.add(d2)
    db.commit()
    with TestClient(app) as c:
        r = c.get("/register")
        assert "уже зарегистрировано" in r.text
    db.delete(d2)
    db.commit()


def test_unknown_device_redirected_to_register(db, monkeypatch):
    monkeypatch.setattr(settings, "block_unknown", True)
    dev = Device(mac="FF:00:00:00:00:06", ip="testclient", name="незнакомец")
    db.add(dev)
    db.commit()
    with TestClient(app) as c:
        r = c.get("/", follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"] == "/register"
    db.delete(dev)
    db.commit()
