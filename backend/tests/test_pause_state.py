"""One reader of pauses: the router, the AI, the panel and the blocked page agree on who is off."""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.ai import tools
from app.db import Base, engine, session
from app.main import app
from app.models import Device, EventLog, KVState, Pause, Person, Rule, kv_set
from app.routers import dashboard, devices
from app.services import enforcement, pause_state, pauses, restrictions, unlock
from app.services.pauses import Target


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr(enforcement, "get_self_ips", lambda: {"192.168.88.2"})
    monkeypatch.setattr(enforcement, "reconcile", lambda s: None)
    monkeypatch.setattr(devices, "_reconcile_bg", lambda: None)
    Base.metadata.create_all(engine)
    s = session()
    for model in (EventLog, KVState, Pause, Rule, Device, Person):
        s.query(model).delete()
    s.commit()
    yield s
    s.close()


@pytest.fixture
def family(db):
    misha = Person(name="Миша", role="kid")
    db.add(misha)
    db.commit()
    phone = Device(
        mac="00:11:22:33:55:01",
        ip="192.168.88.71",
        name="Телефон Миши",
        person_id=misha.id,
        kind="personal",
    )
    tablet = Device(
        mac="00:11:22:33:55:02",
        ip="192.168.88.72",
        name="Планшет Миши",
        person_id=misha.id,
        kind="personal",
    )
    db.add_all([phone, tablet])
    db.commit()
    return misha, phone, tablet


def _blocked(db, *devs):
    return enforcement._desired_state(db, list(devs))["lists"]["hs-blocked"]


def test_a_child_paused_from_the_bot_is_off_everywhere(db, family):
    misha, phone, tablet = family
    pauses.pause(db, Target("person", misha.id), datetime.now() + timedelta(hours=1))

    assert {phone.ip, tablet.ip} <= _blocked(db, phone, tablet)
    assert restrictions.restriction_for_ip(db, phone.ip)["kind"] == "pause"
    rows = {r["name"]: r for r in tools.run_tool(db, "list_devices", {})}
    assert rows["Телефон Миши"]["paused_until"] and rows["Телефон Миши"]["paused_by"] == ["person"]
    cards = dashboard._console(db, [phone, tablet], set())
    card = cards[0]["devices"][0]
    assert card["paused_until"] and card["person_pause"] and not card["group_pause_only"]


def test_a_window_lets_through_older_pauses_but_not_newer_ones(db, family):
    misha, phone, tablet = family
    pauses.pause(db, Target("person", misha.id), datetime.now() + timedelta(hours=2))
    unlock.open_window(db, misha.id, datetime.now() + timedelta(minutes=30))
    assert phone.ip not in _blocked(db, phone, tablet)

    pauses.pause(db, Target("device", tablet.id), datetime.now() + timedelta(minutes=45))
    blocked = _blocked(db, phone, tablet)
    assert tablet.ip in blocked and phone.ip not in blocked
    state = pause_state.device_pauses(db, [tablet])[tablet.id]
    assert state.holds and state.open_until is not None
    assert state.held_until is not None and state.held_until < datetime.now() + timedelta(hours=1)


def test_a_window_stored_before_the_opening_time_was_kept_still_opens(db, family):
    misha, phone, tablet = family
    pauses.pause(db, Target("person", misha.id), datetime.now() + timedelta(hours=1))
    until = datetime.now() + timedelta(minutes=20)
    kv_set(db, f"unlock:{misha.id}", until.isoformat(timespec="seconds"))
    assert phone.ip not in _blocked(db, phone, tablet)
    assert unlock.open_until(db, misha.id) is not None


def test_ai_names_a_child_and_explains_a_device_held_by_the_child(db, family):
    misha, phone, _ = family
    result = tools.run_tool(db, "pause_internet", {"target": "миша", "minutes": 30})
    assert "Миша" in result
    assert pauses.paused_until(db, Target("person", misha.id)) is not None

    with pytest.raises(tools.ToolError, match="Миша"):
        tools.run_tool(db, "resume_internet", {"target": "Телефон Миши"})
    assert tools.run_tool(db, "resume_internet", {"target": "Миша"}).startswith("▶️")
    assert phone.ip not in _blocked(db, phone)
    with pytest.raises(tools.ToolError):
        tools.run_tool(db, "resume_internet", {"target": "Миша"})


def test_ai_resume_kids_lifts_every_child_pause(db, family):
    misha, phone, tablet = family
    pauses.pause(db, Target("person", misha.id), datetime.now() + timedelta(hours=1))
    db.add(
        Pause(
            target_type="device", target=str(tablet.id), until=datetime.now() + timedelta(hours=1)
        )
    )
    db.commit()
    tools.run_tool(db, "resume_internet", {"target": "kid"})
    assert not _blocked(db, phone, tablet)


def test_panel_lifts_a_childs_pause(db, family):
    misha, phone, tablet = family
    pauses.pause(db, Target("person", misha.id), datetime.now() + timedelta(hours=1))
    with TestClient(app) as client:
        client.post("/login", data={"username": "admin", "password": "testpass"})
        page = client.get("/devices")
        assert page.status_code == 200 and "⏸ до" in page.text
        moved = client.post(f"/people/{misha.id}/unpause", follow_redirects=False)
    assert moved.status_code == 302
    db.expire_all()
    assert not _blocked(db, phone, tablet)
