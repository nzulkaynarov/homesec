"""Grouped sorting of unclaimed devices through the Luna API."""

from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient

from app.ai import tools
from app.config import settings
from app.db import Base, engine, session
from app.main import app
from app.models import Device, DeviceMac, EventLog, KVState, Person, QuotaUsage

AUTH = {"Authorization": "Bearer test-token"}
URL = "/api/luna/v1/device-triage"


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr(settings, "luna_api_token", "test-token")
    monkeypatch.setattr(tools.enforcement, "get_self_ips", lambda: {"192.168.88.2"})
    Base.metadata.create_all(engine)
    s = session()
    for model in (QuotaUsage, DeviceMac, EventLog, KVState, Device, Person):
        s.query(model).delete()
    s.commit()
    yield s
    s.close()


@pytest.fixture
def client():
    return TestClient(app)


def _device(db, last_octet: int, label: str = "Watch", prefix: str = "AA", **kwargs) -> Device:
    fields = {
        "name": label,
        "hostname": label,
        "mac": f"{prefix}:00:00:00:00:{last_octet:02X}",
        **kwargs,
    }
    device = Device(**fields)
    db.add(device)
    db.commit()
    return device


def _person(db, name: str = "Марат", role: str = "adult") -> Person:
    person = Person(name=name, role=role)
    db.add(person)
    db.commit()
    return person


def test_private_addresses_with_one_hostname_form_one_group(db, client):
    first = _device(db, 1)
    _device(db, 2)
    _device(db, 3, "Mac")
    groups = client.get(URL, headers=AUTH).json()
    assert [(g["id"], g["name"], g["count"], g["mac_random"]) for g in groups][0] == (
        first.id,
        "Watch",
        2,
        True,
    )
    assert groups[1]["count"] == 1


def test_real_addresses_with_one_name_stay_separate(db, client):
    _device(db, 1, "Printer", prefix="00")
    _device(db, 2, "Printer", prefix="00")
    assert [g["count"] for g in client.get(URL, headers=AUTH).json()] == [1, 1]


def test_records_without_a_name_never_group(db, client):
    _device(db, 1, "AA:00:00:00:00:01", hostname="")
    _device(db, 2, "AA:00:00:00:00:02", hostname="")
    assert len(client.get(URL, headers=AUTH).json()) == 2


def test_claimed_and_typed_devices_are_not_in_the_queue(db, client):
    _device(db, 1, person_id=_person(db).id)
    _device(db, 2, kind="iot")
    _device(db, 3, kind="shared")
    assert client.get(URL, headers=AUTH).json() == []


def test_owner_choice_folds_the_group_into_their_device(db, client):
    person = _person(db)
    own = _device(db, 1, person_id=person.id)
    first = _device(db, 2)
    _device(db, 3)
    assert client.get(URL, headers=AUTH).json()[0]["owners"] == [{"id": person.id, "name": "Марат"}]

    answer = client.post(f"{URL}/{first.id}", json={"person_id": person.id}, headers=AUTH)

    assert answer.status_code == 200
    assert answer.json() == {"name": "Watch", "count": 2, "person": "Марат", "kind": "personal"}
    db.expire_all()
    assert db.query(Device).count() == 1
    macs = {m.mac for m in db.query(DeviceMac).filter_by(device_id=own.id)}
    assert macs == {"AA:00:00:00:00:02", "AA:00:00:00:00:03"}
    assert db.get(KVState, "reconcile_asap") is not None


def test_owner_without_a_matching_device_receives_the_first_record(db, client):
    person = _person(db)
    first = _device(db, 1)
    _device(db, 2)
    client.post(f"{URL}/{first.id}", json={"person_id": person.id}, headers=AUTH)
    db.expire_all()
    kept = db.query(Device).one()
    assert kept.id == first.id and kept.person_id == person.id


def test_kind_choice_folds_the_group(db, client):
    first = _device(db, 1)
    _device(db, 2)
    answer = client.post(f"{URL}/{first.id}", json={"kind": "iot"}, headers=AUTH)
    assert answer.json()["kind"] == "iot"
    db.expire_all()
    assert db.query(Device).one().group == "iot"


def test_usage_of_several_records_for_one_day_is_summed(db, client):
    first = _device(db, 1)
    second = _device(db, 2)
    third = _device(db, 3)
    today = date.today().isoformat()
    for device, minutes in ((second, 10), (third, 5)):
        db.add(QuotaUsage(device_id=device.id, date=today, category="games", minutes=minutes))
    db.commit()
    client.post(f"{URL}/{first.id}", json={"kind": "shared"}, headers=AUTH)
    db.expire_all()
    usage = db.query(QuotaUsage).filter_by(device_id=first.id).all()
    assert [u.minutes for u in usage] == [15]


def test_decision_needs_exactly_one_destination(db, client):
    first = _device(db, 1)
    assert client.post(f"{URL}/{first.id}", json={}, headers=AUTH).status_code == 422
    both = {"person_id": _person(db).id, "kind": "iot"}
    assert client.post(f"{URL}/{first.id}", json=both, headers=AUTH).status_code == 422
    assert (
        client.post(f"{URL}/{first.id}", json={"kind": "personal"}, headers=AUTH).status_code == 422
    )


def test_unknown_device_or_person_is_404(db, client):
    first = _device(db, 1)
    assert client.post(f"{URL}/999999", json={"kind": "iot"}, headers=AUTH).status_code == 404
    assert (
        client.post(f"{URL}/{first.id}", json={"person_id": 999999}, headers=AUTH).status_code
        == 404
    )


def test_the_pi_itself_cannot_be_folded(db, client):
    first = _device(db, 1, ip="192.168.88.2")
    assert client.post(f"{URL}/{first.id}", json={"kind": "iot"}, headers=AUTH).status_code == 409
    db.expire_all()
    assert db.get(Device, first.id).kind == "personal"


def test_triage_requires_the_token(db, client):
    assert client.get(URL).status_code == 401


def test_last_seen_is_the_newest_record(db, client):
    _device(db, 1, first_seen=datetime(2026, 1, 1, 8, 0))
    _device(db, 2, first_seen=datetime(2026, 3, 1, 9, 30))
    assert client.get(URL, headers=AUTH).json()[0]["last_seen"] == "2026-03-01T09:30"
