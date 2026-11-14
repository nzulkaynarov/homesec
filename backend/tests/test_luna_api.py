"""Network API for Luna and the outage watch on the Pi."""

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.bot import alerts, luna_watch
from app.config import settings
from app.db import Base, engine, session
from app.main import app
from app.models import (
    BonusRequest,
    Device,
    EventLog,
    KVState,
    Pause,
    Person,
    QuotaBonus,
    Rule,
    kv_set,
)
from app.services import enforcement, house_rules

AUTH = {"Authorization": "Bearer test-token"}


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr(settings, "luna_api_token", "test-token")
    Base.metadata.create_all(engine)
    s = session()
    monkeypatch.setattr(enforcement, "get_self_ips", lambda: {"192.168.88.2"})
    for model in (BonusRequest, QuotaBonus, EventLog, KVState, Pause, Rule, Device, Person):
        s.query(model).delete()
    s.commit()
    yield s
    s.close()


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def kid(db):
    person = Person(name="Миша", role="kid")
    db.add(person)
    db.commit()
    db.add(
        Device(
            name="Планшет",
            mac="AA:BB:CC:DD:EE:01",
            ip="192.168.88.50",
            person_id=person.id,
        )
    )
    db.commit()
    return person


def test_health_is_public(db, client):
    assert client.get("/api/luna/v1/health").json() == {"status": "ok"}


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}])
def test_requests_without_valid_token_are_rejected(db, client, headers):
    response = client.post("/api/luna/v1/heartbeat", json={}, headers=headers)
    assert response.status_code == 401


def test_api_is_closed_when_token_not_configured(db, client, monkeypatch):
    monkeypatch.setattr(settings, "luna_api_token", "")
    assert client.post("/api/luna/v1/heartbeat", json={}, headers=AUTH).status_code == 503


def test_time_request_flow(db, client, kid):
    created = client.post(
        "/api/luna/v1/people/миша/time-requests", json={"reason": "доделать"}, headers=AUTH
    )
    assert created.status_code == 201
    request_id = created.json()["id"]
    assert any(e.kind == "bonus_request" for e in db.query(EventLog))
    assert (
        client.post("/api/luna/v1/people/Миша/time-requests", json={}, headers=AUTH).status_code
        == 409
    )
    assert (
        client.get(f"/api/luna/v1/time-requests/{request_id}", headers=AUTH).json()["status"]
        == "pending"
    )


def test_unknown_person_or_no_device(db, client):
    assert (
        client.post("/api/luna/v1/people/Никто/time-requests", json={}, headers=AUTH).status_code
        == 404
    )
    db.add(Person(name="Без устройства", role="kid"))
    db.commit()
    url = "/api/luna/v1/people/Без устройства/time-requests"
    assert client.post(url, json={}, headers=AUTH).status_code == 404


def test_correct_quiz_awards_once(db, client, kid):
    from app.models import Quota as _Quota

    db.add(_Quota(name="Игры", target_type="person", target=str(kid.id), category="games",
                  minutes_per_day=60, days="0,1,2,3,4,5,6", enabled=True))
    db.commit()
    assert client.get("/api/luna/v1/people/Миша/quiz", headers=AUTH).json() == {"available": True}
    won = client.post("/api/luna/v1/people/Миша/quiz-result", json={"correct": True}, headers=AUTH)
    assert won.json() == {"awarded_minutes": 15}
    assert db.query(QuotaBonus).count() == 1
    again = client.post(
        "/api/luna/v1/people/Миша/quiz-result", json={"correct": True}, headers=AUTH
    )
    assert again.status_code == 409


def test_wrong_quiz_spends_the_attempt(db, client, kid):
    lost = client.post(
        "/api/luna/v1/people/Миша/quiz-result", json={"correct": False}, headers=AUTH
    )
    assert lost.json() == {"awarded_minutes": 0}
    assert client.get("/api/luna/v1/people/Миша/quiz", headers=AUTH).json() == {"available": False}


def test_heartbeat_stores_members(db, client):
    assert (
        client.post(
            "/api/luna/v1/heartbeat", json={"member_chat_ids": [1, 2]}, headers=AUTH
        ).status_code
        == 200
    )
    db.expire_all()
    assert luna_watch.member_chat_ids(db) == [1, 2]


def test_watch_reports_outage_once_and_recovery(db):
    now = datetime(2026, 11, 9, 12, 0)
    assert luna_watch.evaluate(db, now) is None  # Luna has never connected
    kv_set(db, luna_watch.HEARTBEAT_KEY, (now - timedelta(minutes=1)).isoformat())
    assert luna_watch.evaluate(db, now) is None
    kv_set(db, luna_watch.HEARTBEAT_KEY, (now - timedelta(minutes=4)).isoformat())
    assert luna_watch.evaluate(db, now) == "down"
    assert luna_watch.evaluate(db, now) is None
    kv_set(db, luna_watch.HEARTBEAT_KEY, now.isoformat())
    assert luna_watch.evaluate(db, now) == "up"
    assert luna_watch.evaluate(db, now) is None


def test_people_lists_family_with_device_counts(db, client, kid):
    db.add(Person(name="Мама", role="adult"))
    db.commit()
    people = {p["name"]: p for p in client.get("/api/luna/v1/people", headers=AUTH).json()}
    assert people["Миша"]["devices"] == 1 and people["Миша"]["role"] == "kid"
    assert people["Мама"]["devices"] == 0 and people["Мама"]["telegram_id"] is None


def test_people_requires_token(db, client):
    assert client.get("/api/luna/v1/people").status_code == 401


@pytest.mark.parametrize(
    ("role", "bot_role"), [("kid", "kid"), ("adult", "admin"), ("guest", "guest")]
)
def test_link_sets_telegram_and_matching_bot_role(db, client, role, bot_role):
    person = Person(name="Человек", role=role)
    db.add(person)
    db.commit()
    url = f"/api/luna/v1/people/{person.id}/telegram"
    assert client.put(url, json={"telegram_id": 555}, headers=AUTH).status_code == 200
    db.expire_all()
    assert person.telegram_id == 555 and person.bot_role == bot_role


def test_one_telegram_account_cannot_be_two_people(db, client):
    first, second = Person(name="А", role="kid", telegram_id=555), Person(name="Б", role="kid")
    db.add_all([first, second])
    db.commit()
    url = f"/api/luna/v1/people/{second.id}/telegram"
    assert client.put(url, json={"telegram_id": 555}, headers=AUTH).status_code == 409


def test_link_unknown_person(db, client):
    assert (
        client.put(
            "/api/luna/v1/people/999999/telegram", json={"telegram_id": 1}, headers=AUTH
        ).status_code
        == 404
    )


def _bedtime_now(db, person):
    now = datetime.now()
    start, end = now - timedelta(hours=1), now + timedelta(hours=1)
    db.add(
        Rule(
            name="Отбой",
            target_type="person",
            target=str(person.id),
            days="0,1,2,3,4,5,6",
            start_time=f"{start:%H:%M}",
            end_time=f"{end:%H:%M}",
        )
    )
    db.commit()
    return end


def _blocked(db):
    db.expire_all()
    return enforcement._desired_state(db, list(db.query(Device)))["lists"]["hs-blocked"]


def test_no_restriction_by_default(db, client, kid):
    url = "/api/luna/v1/people/Миша/restriction"
    assert client.get(url, headers=AUTH).json() == {"restriction": None}


def test_bedtime_is_reported_and_refuses_requests(db, client, kid):
    end = _bedtime_now(db, kid)
    found = client.get("/api/luna/v1/people/Миша/restriction", headers=AUTH).json()["restriction"]
    assert found["kind"] == "bedtime" and found["until"] == f"{end:%Y-%m-%dT%H:%M}"
    url = "/api/luna/v1/people/Миша/time-requests"
    assert client.post(url, json={}, headers=AUTH).status_code == 409
    assert db.query(BonusRequest).count() == 0


def test_pause_request_opens_a_window_and_the_pause_returns(db, client, kid):
    until = datetime.now() + timedelta(hours=2)
    db.add(Pause(target_type="group", target="kid", until=until, reason="ИИ"))
    db.commit()
    assert "192.168.88.50" in _blocked(db)
    found = client.get("/api/luna/v1/people/Миша/restriction", headers=AUTH).json()["restriction"]
    assert (found["kind"], found["open_until"]) == ("pause", None)

    created = client.post("/api/luna/v1/people/Миша/time-requests", json={}, headers=AUTH)
    request_id = created.json()["id"]
    assert db.get(BonusRequest, request_id).category == "pause"
    buttons = alerts.bonus_request_keyboard(request_id, "pause").inline_keyboard
    assert buttons[1][0].text == "▶️ До конца паузы"

    result = alerts._resolve_bonus_request(request_id, "30")
    assert "Интернет открыт до" in result
    assert "192.168.88.50" not in _blocked(db)
    status = client.get(f"/api/luna/v1/time-requests/{request_id}", headers=AUTH).json()
    assert status["status"] == "approved" and status["category"] == "pause"
    assert 29 <= status["minutes"] <= 30
    found = client.get("/api/luna/v1/people/Миша/restriction", headers=AUTH).json()["restriction"]
    assert found["open_until"] is not None

    later = datetime.now() + timedelta(minutes=31)
    db.expire_all()
    state = enforcement._desired_state
    with patch.object(enforcement, "datetime", wraps=datetime) as clock:
        clock.now.return_value = later
        assert "192.168.88.50" in state(db, list(db.query(Device)))["lists"]["hs-blocked"]


def test_window_never_opens_bedtime(db, client, kid):
    _bedtime_now(db, kid)
    db.add(
        Pause(target_type="person", target=str(kid.id), until=datetime.now() + timedelta(hours=1))
    )
    db.commit()
    house_rules.open_through_pause(db, kid.id, 30)
    assert "192.168.88.50" in _blocked(db)


def test_luna_cannot_take_over_a_linked_parent(db, client):
    parent = Person(name="Папа", role="adult", telegram_id=111, bot_role="admin")
    db.add(parent)
    db.commit()
    taken = client.put(f"/api/luna/v1/people/{parent.id}/telegram", json={"telegram_id": 666},
                       headers=AUTH)
    assert taken.status_code == 409
    db.refresh(parent)
    assert parent.telegram_id == 111
    same = client.put(f"/api/luna/v1/people/{parent.id}/telegram", json={"telegram_id": 111},
                      headers=AUTH)
    assert same.status_code == 200  # repeating the existing link stays harmless


def test_a_person_is_found_by_id_after_a_rename(db, client, kid):
    kid.name = "Михаил"
    db.commit()
    response = client.get(f"/api/luna/v1/people/{kid.id}/restriction", headers=AUTH)
    assert response.status_code == 200
