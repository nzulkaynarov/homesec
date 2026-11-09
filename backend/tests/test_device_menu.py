"""Moving any device to another owner or kind from the Telegram menu."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import Message

from app.ai import tools
from app.bot import access, device_menu, handlers
from app.db import Base, engine, session
from app.models import Device, EventLog, KVState, Person
from app.services import device_owners


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr(tools.enforcement, "get_self_ips", lambda: {"192.168.88.2"})
    monkeypatch.setattr(device_menu, "vendor_of", AsyncMock(return_value="Sony"))
    Base.metadata.create_all(engine)
    s = session()
    for model in (EventLog, KVState, Device, Person):
        s.query(model).delete()
    s.commit()
    yield s
    s.close()


def _person(db, name="Камила", role="adult") -> Person:
    person = Person(name=name, role=role)
    db.add(person)
    db.commit()
    return person


def _device(db, octet=10, name="iPhone", **kwargs) -> Device:
    device = Device(
        mac=f"00:11:22:33:44:{octet:02X}", ip=f"192.168.88.{octet}", name=name, **kwargs
    )
    db.add(device)
    db.commit()
    return device


def _press(data: str) -> MagicMock:
    cb = MagicMock(data=data, answer=AsyncMock())
    cb.message = MagicMock(spec=Message, edit_text=AsyncMock())
    return cb


def _run(handler, data: str) -> tuple[str, list[str]]:
    cb = _press(data)
    asyncio.run(handler(cb))
    call = cb.message.edit_text.await_args
    return call.args[0], [
        b.callback_data for row in call.kwargs["reply_markup"].inline_keyboard for b in row
    ]


def _fresh(db, device: Device) -> Device:
    db.expire_all()
    return db.get(Device, device.id)


def test_overview_counts_owners_and_kinds(db):
    person = _person(db)
    _device(db, 10, person_id=person.id)
    _device(db, 11, kind="shared")
    _device(db, 12)
    text, actions = _run(device_menu.cb_overview, "dm")
    assert "Устройства" in text
    assert (
        f"dms:p{person.id}:0" in actions and "dms:shared:0" in actions and "dms:none:0" in actions
    )
    assert "dms:iot:0" not in actions


def test_section_lists_only_its_devices_with_addresses(db):
    person = _person(db)
    mine = _device(db, 10, person_id=person.id)
    other = _device(db, 11)
    _, actions = _run(device_menu.cb_section, f"dms:p{person.id}:0")
    assert f"dmd:{mine.id}:p{person.id}:0" in actions
    assert not any(a.startswith(f"dmd:{other.id}:") for a in actions)


def test_long_section_pages(db):
    for octet in range(10, 23):
        _device(db, octet)
    _, first = _run(device_menu.cb_section, "dms:none:0")
    _, second = _run(device_menu.cb_section, "dms:none:1")
    assert sum(a.startswith("dmd:") for a in first) == 10 and "dms:none:1" in first
    assert sum(a.startswith("dmd:") for a in second) == 3 and "dms:none:0" in second


def test_owner_can_be_replaced(db):
    old, new = _person(db, "Алимбек", "kid"), _person(db, "Умарбек", "kid")
    device = _device(db, 10, person_id=old.id)
    text, _ = _run(device_menu.cb_choose, f"dmc:{device.id}:{new.id}:p{old.id}:0")
    assert "✅" in text and "Умарбек" in text
    assert _fresh(db, device).person_id == new.id
    assert db.get(KVState, "reconcile_asap").value == "1"


def test_person_choice_makes_a_shared_device_personal(db):
    person = _person(db)
    device = _device(db, 10, kind="shared")
    _run(device_menu.cb_choose, f"dmc:{device.id}:{person.id}:shared:0")
    fresh = _fresh(db, device)
    assert (fresh.kind, fresh.person_id) == ("personal", person.id)


@pytest.mark.parametrize("kind", ["shared", "iot", "infra"])
def test_kind_choice_frees_the_device_from_its_owner(db, kind):
    person = _person(db, "Умарбек", "kid")
    device = _device(db, 10, person_id=person.id)
    _run(device_menu.cb_choose, f"dmc:{device.id}:{kind}:p{person.id}:0")
    fresh = _fresh(db, device)
    assert (fresh.kind, fresh.person_id, fresh.group) == (kind, None, kind)


def test_owner_can_be_cleared(db):
    person = _person(db)
    device = _device(db, 10, person_id=person.id)
    _run(device_menu.cb_choose, f"dmc:{device.id}:none:p{person.id}:0")
    fresh = _fresh(db, device)
    assert (fresh.kind, fresh.person_id, fresh.group) == ("personal", None, "unknown")


def test_leaving_infrastructure_asks_first(db):
    person = _person(db, "Умарбек", "kid")
    device = _device(db, 10, kind="infra")
    text, actions = _run(device_menu.cb_choose, f"dmc:{device.id}:{person.id}:infra:0")
    assert "потерять интернет" in text and f"dmy:{device.id}:{person.id}:infra:0" in actions
    assert _fresh(db, device).kind == "infra"
    _run(device_menu.cb_apply, f"dmy:{device.id}:{person.id}:infra:0")
    fresh = _fresh(db, device)
    assert (fresh.kind, fresh.person_id) == ("personal", person.id)


def test_pi_cannot_be_moved_even_with_an_old_button(db):
    person = _person(db, "Умарбек", "kid")
    pi = _device(db, 2, kind="infra")
    text, actions = _run(device_menu.cb_device, f"dmd:{pi.id}:infra:0")
    assert "сама Pi" in text and not any(a.startswith(("dmc:", "dmp:")) for a in actions)
    text, _ = _run(device_menu.cb_apply, f"dmy:{pi.id}:{person.id}:infra:0")
    assert "сама Pi" in text
    assert _fresh(db, pi).kind == "infra"


def test_card_shows_details_and_only_useful_actions(db):
    device = _device(db, 10, kind="shared")
    text, actions = _run(device_menu.cb_device, f"dmd:{device.id}:shared:0")
    assert "Производитель: Sony" in text and "192.168.88.10" in text
    assert f"dmc:{device.id}:shared:shared:0" not in actions
    assert f"dmc:{device.id}:iot:shared:0" in actions and f"dmp:{device.id}:shared:0" in actions


def test_missing_device_is_reported_not_raised(db):
    text, _ = _run(device_menu.cb_device, "dmd:999:none:0")
    assert device_menu.GONE in text
    with pytest.raises(LookupError):
        device_owners.move(db, 999, "shared")


@pytest.mark.parametrize("role", ["kid", "guest"])
def test_children_and_guests_are_kept_out(role):
    assert not access.check_message(role, device_menu.BTN_DEVICES).allowed
    assert not access.check_callback(role, "dmc:1:2:none:0").allowed


def test_parents_are_let_in():
    assert access.check_message("admin", device_menu.BTN_DEVICES).allowed
    assert access.check_callback("admin", "dmc:1:2:none:0").allowed


def test_menu_sits_behind_the_access_middleware():
    callbacks = {h.callback for h in handlers.router.callback_query.handlers}
    assert {device_menu.cb_choose, device_menu.cb_apply, device_menu.cb_device} <= callbacks
    assert device_menu.btn_devices in {h.callback for h in handlers.router.message.handlers}


def test_callback_data_fits_telegram_limit():
    assert len(f"dmy:{10**9}:{10**9}:p{10**9}:999".encode()) <= 64
