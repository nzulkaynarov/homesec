"""Shared devices: kid mode by default, off on demand, open for adults until a set time."""

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.bot import access, handlers, shared_menu
from app.config import settings
from app.db import Base, engine, session
from app.main import app
from app.models import GROUP_ADDRESS_LISTS, Device, EventLog, KVState, Pause, Person, kv_get
from app.services import family

AUTH = {"Authorization": "Bearer test-token"}


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr(settings, "luna_api_token", "test-token")
    Base.metadata.create_all(engine)
    s = session()
    for model in (Pause, EventLog, KVState, Device, Person):
        s.query(model).delete()
    s.commit()
    yield s
    s.close()


@pytest.fixture
def tv(db):
    device = Device(mac="AA:22:00:00:00:09", ip="192.168.88.90", name="TV", kind="shared")
    db.add(device)
    db.commit()
    return device


def test_shared_device_is_kid_mode_by_default(tv):
    assert tv.group == "shared"
    assert GROUP_ADDRESS_LISTS[tv.group] == "hs-kids"
    assert family.shared_device_state(tv) == "kid"


def test_adult_mode_lifts_kid_rules_and_expires_alone(db, tv):
    family.set_shared_state(db, tv, "adult", 3)
    assert tv.group == "adult" and tv.group not in GROUP_ADDRESS_LISTS
    assert family.shared_device_state(tv) == "adult"
    assert kv_get(db, "reconcile_asap") == "1"
    tv.adult_until = datetime.now() - timedelta(minutes=1)
    assert tv.group == "shared" and family.shared_device_state(tv) == "kid"


def test_off_blocks_and_kid_mode_restores(db, tv):
    family.set_shared_state(db, tv, "off")
    assert tv.blocked_manual is True and family.shared_device_state(tv) == "off"
    family.set_shared_state(db, tv, "kid")
    assert tv.blocked_manual is False


def test_adult_mode_clears_device_pause(db, tv):
    db.add(
        Pause(target_type="device", target=str(tv.id), until=datetime.now() + timedelta(hours=1))
    )
    db.commit()
    family.set_shared_state(db, tv, "adult", 1)
    assert db.query(Pause).count() == 0


def test_personal_device_cannot_use_shared_modes(db):
    phone = Device(mac="AA:22:00:00:00:08", ip="192.168.88.91", name="Phone", kind="personal")
    db.add(phone)
    db.commit()
    with pytest.raises(ValueError):
        family.set_shared_state(db, phone, "off")


def test_movie_mode_has_a_real_end_time(db, tv):
    family.set_movie_mode(db, 2)
    assert (
        datetime.now() + timedelta(hours=1) < tv.adult_until < datetime.now() + timedelta(hours=3)
    )


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (datetime(2026, 11, 9, 22, 0), 9.0),
        (datetime(2026, 11, 9, 6, 0), 1.0),
        (datetime(2026, 11, 9, 7, 0), 24.0),
    ],
)
def test_hours_until_morning(now, expected):
    assert family.hours_until_morning(now) == expected


def test_luna_cannot_switch_shared_devices(db, tv):
    """Shared devices are parents' network control and live in the HomeSec bot only."""
    client = TestClient(app)
    assert client.get("/api/luna/v1/shared-devices", headers=AUTH).status_code == 404
    url = f"/api/luna/v1/shared-devices/{tv.id}/state"
    assert client.post(url, json={"state": "off"}, headers=AUTH).status_code == 404


def test_shared_devices_do_not_count_as_personal(db, client=None):
    person = Person(name="Миша", role="kid")
    db.add(person)
    db.commit()
    db.add_all(
        [
            Device(mac="AA:22:00:00:00:01", ip="192.168.88.71", name="Tablet", person_id=person.id),
            Device(
                mac="AA:22:00:00:00:02",
                ip="192.168.88.72",
                name="TV",
                kind="shared",
                person_id=person.id,
            ),
        ]
    )
    db.commit()
    people = TestClient(app).get("/api/luna/v1/people", headers=AUTH).json()
    assert people[0]["devices"] == 1


@pytest.mark.parametrize("role", ["kid", "guest", "unauthorized"])
def test_only_adults_control_shared_devices(role):
    assert not access.check_message(role, "📺 Общие устройства").allowed
    assert not access.check_callback(role, "shs:1:off").allowed


def test_buttons_set_state_and_edit_the_same_message(db, tv):
    callback = MagicMock(data=f"shs:{tv.id}:off", answer=AsyncMock())
    callback.message = MagicMock(spec=handlers.Message, edit_text=AsyncMock())
    asyncio.run(shared_menu.cb_shared_state(callback))
    db.refresh(tv)
    assert tv.blocked_manual is True
    callback.answer.assert_awaited_once()
    assert "Выключено" in callback.message.edit_text.await_args.args[0]


def test_callback_data_fits_telegram_limit():
    markup = shared_menu._shared_actions_markup(10**9)
    for row in markup.inline_keyboard:
        for button in row:
            assert len(button.callback_data.encode()) <= 64
