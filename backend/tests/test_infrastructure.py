"""The network's own equipment must keep working whatever anyone sets on it."""

import pytest

from app.ai import tools
from app.db import Base, engine, session
from app.models import (
    DEVICE_KINDS,
    GROUP_LABELS,
    Device,
    GroupPolicy,
    Person,
    Quota,
)
from app.services.enforcement import _desired_state


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr(tools.enforcement, "get_self_ips", lambda: {"192.168.88.2"})
    Base.metadata.create_all(engine)
    s = session()
    for model in (Quota, GroupPolicy, Device, Person):
        s.query(model).delete()
    s.commit()
    yield s
    s.close()


def _server(db, **kwargs) -> Device:
    device = Device(
        mac="00:E0:4C:00:00:01", ip="192.168.88.241", name="Сервер", kind="infra", **kwargs
    )
    db.add(device)
    db.commit()
    return device


def _state(db) -> dict:
    return _desired_state(db, list(db.query(Device)))


def test_infrastructure_is_a_known_kind_with_a_label():
    assert "infra" in DEVICE_KINDS and "infra" in GROUP_LABELS


def test_flags_cannot_cut_off_infrastructure(db):
    _server(db, blocked_manual=True, speed_limit="1M/1M")
    state = _state(db)
    lists = state["lists"]
    assert "192.168.88.241" not in lists["hs-blocked"]
    assert "192.168.88.241" not in lists["hs-managed"]
    assert "192.168.88.241" not in lists["hs-kids"]
    assert "192.168.88.241" not in state["queues"]


def test_kid_filter_does_not_reach_infrastructure(db):
    db.add(GroupPolicy(group="kid", blocked_services="", safe_search=True))
    db.commit()
    _server(db)
    assert "192.168.88.241" not in str(_state(db).get("ag_clients", {}))


def test_tools_refuse_to_block_infrastructure(db):
    with pytest.raises(tools.ToolError):
        tools._guard_self(_server(db))


def test_marking_as_infrastructure_frees_it_from_its_owner(db):
    kid = Person(name="Ребёнок", role="kid")
    device = Device(mac="00:E0:4C:00:00:02", ip="192.168.88.50", name="ПК", person=kid)
    db.add_all([kid, device])
    db.commit()
    assert device.group == "kid"
    tools.set_device_kind(db, device.id, "infra")
    db.refresh(device)
    assert device.person is None and device.group == "infra"
