"""Grouped sorting of unclaimed devices and its Telegram menu."""

import asyncio
from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import Message

from app.ai import tools
from app.bot import access, handlers, triage
from app.db import Base, engine, session
from app.models import Device, DeviceMac, EventLog, KVState, Person, QuotaUsage
from app.services import device_triage


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr(tools.enforcement, "get_self_ips", lambda: {"192.168.88.2"})
    Base.metadata.create_all(engine)
    s = session()
    for model in (QuotaUsage, DeviceMac, EventLog, KVState, Device, Person):
        s.query(model).delete()
    s.commit()
    yield s
    s.close()


def _device(db, last_octet: int, label: str = "Watch", prefix: str = "AA", **kwargs) -> Device:
    fields = {"name": label, "hostname": label, "mac": f"{prefix}:00:00:00:00:{last_octet:02X}"}
    device = Device(**{**fields, **kwargs})
    db.add(device)
    db.commit()
    return device


def _person(db, name: str = "Марат", role: str = "adult") -> Person:
    person = Person(name=name, role=role)
    db.add(person)
    db.commit()
    return person


def _press(data: str) -> MagicMock:
    cb = MagicMock(data=data, answer=AsyncMock())
    cb.message = MagicMock(spec=Message, edit_text=AsyncMock())
    return cb


def _edited(cb: MagicMock) -> tuple[str, list[str]]:
    call = cb.message.edit_text.await_args
    markup = call.kwargs["reply_markup"]
    rows = markup.inline_keyboard if markup else []
    return call.args[0], [button.callback_data for row in rows for button in row]


def test_private_addresses_with_one_hostname_form_one_group(db):
    first = _device(db, 1)
    _device(db, 2)
    _device(db, 3, "Mac")
    groups = device_triage.list_groups(db)
    assert (groups[0]["id"], groups[0]["name"], groups[0]["count"]) == (first.id, "Watch", 2)
    assert groups[1]["count"] == 1


def test_real_addresses_with_one_name_stay_separate(db):
    _device(db, 1, "Printer", prefix="00")
    _device(db, 2, "Printer", prefix="00")
    assert [g["count"] for g in device_triage.list_groups(db)] == [1, 1]


def test_records_without_a_name_never_group(db):
    _device(db, 1, "AA:00:00:00:00:01", hostname="")
    _device(db, 2, "AA:00:00:00:00:02", hostname="")
    assert len(device_triage.list_groups(db)) == 2


def test_claimed_and_typed_devices_are_not_in_the_queue(db):
    _device(db, 1, person_id=_person(db).id)
    _device(db, 2, kind="iot")
    _device(db, 3, kind="shared")
    assert device_triage.list_groups(db) == []


def test_last_seen_is_the_newest_record(db):
    _device(db, 1, first_seen=datetime(2026, 1, 1, 8, 0))
    _device(db, 2, first_seen=datetime(2026, 3, 1, 9, 30))
    assert device_triage.list_groups(db)[0]["last_seen"] == "2026-03-01T09:30"


def test_owner_choice_folds_the_group_into_their_device(db):
    person = _person(db)
    own = _device(db, 1, person_id=person.id)
    first = _device(db, 2)
    _device(db, 3)
    assert device_triage.list_groups(db)[0]["owners"] == [{"id": person.id, "name": "Марат"}]

    result = device_triage.sort_group(db, first.id, person.id, None)

    assert result == {"name": "Watch", "count": 2, "person": "Марат", "kind": "personal"}
    db.expire_all()
    assert db.query(Device).count() == 1
    macs = {m.mac for m in db.query(DeviceMac).filter_by(device_id=own.id)}
    assert macs == {"AA:00:00:00:00:02", "AA:00:00:00:00:03"}
    assert db.get(KVState, "reconcile_asap") is not None


def test_owner_without_a_matching_device_receives_the_first_record(db):
    person = _person(db)
    first = _device(db, 1)
    _device(db, 2)
    device_triage.sort_group(db, first.id, person.id, None)
    db.expire_all()
    kept = db.query(Device).one()
    assert kept.id == first.id and kept.person_id == person.id


def test_kind_choice_folds_the_group(db):
    first = _device(db, 1)
    _device(db, 2)
    assert device_triage.sort_group(db, first.id, None, "iot")["kind"] == "iot"
    db.expire_all()
    assert db.query(Device).one().group == "iot"


def test_usage_of_several_records_for_one_day_is_summed(db):
    first = _device(db, 1)
    second = _device(db, 2)
    third = _device(db, 3)
    today = date.today().isoformat()
    for device, minutes in ((second, 10), (third, 5)):
        db.add(QuotaUsage(device_id=device.id, date=today, category="games", minutes=minutes))
    db.commit()
    device_triage.sort_group(db, first.id, None, "shared")
    db.expire_all()
    usage = db.query(QuotaUsage).filter_by(device_id=first.id).all()
    assert [u.minutes for u in usage] == [15]


def test_unknown_device_or_person_is_refused(db):
    first = _device(db, 1)
    with pytest.raises(LookupError):
        device_triage.sort_group(db, 999999, None, "iot")
    with pytest.raises(LookupError):
        device_triage.sort_group(db, first.id, 999999, None)


def test_the_pi_itself_cannot_be_folded(db):
    first = _device(db, 1, ip="192.168.88.2")
    with pytest.raises(tools.ToolError):
        device_triage.sort_group(db, first.id, None, "iot")
    db.expire_all()
    assert db.get(Device, first.id).kind == "personal"


@pytest.mark.parametrize("role", ["kid", "guest", "unauthorized"])
@pytest.mark.parametrize("data", ["trg:0", "trgp:1:0", "trgc:1:iot:0", "trgy:1:iot:0"])
def test_only_adults_can_press_triage_buttons(role, data):
    assert not access.check_callback(role, data).allowed
    assert access.check_callback("admin", data).allowed


@pytest.mark.parametrize("role", ["kid", "guest"])
def test_children_and_guests_cannot_open_the_menu(role):
    assert not access.check_message(role, triage.BTN_TRIAGE).allowed


def test_triage_handlers_sit_behind_the_access_middleware():
    callbacks = {h.callback for h in handlers.router.callback_query.handlers}
    assert {triage.cb_card, triage.cb_people, triage.cb_choose, triage.cb_apply} <= callbacks
    assert triage.btn_triage in {h.callback for h in handlers.router.message.handlers}
    assert handlers.router.callback_query.outer_middleware._middlewares


def test_triage_button_is_reached_before_free_text_handlers():
    names = [h.callback.__name__ for h in handlers.router.message.handlers]
    assert names.index("btn_triage") < names.index("free_text")


def test_callback_data_fits_telegram_limit():
    markup = triage.card_keyboard(10**12, 10**4, 10**5)
    data = [button.callback_data for row in markup.inline_keyboard for button in row]
    assert all(len(item.encode()) <= 64 for item in data)


def test_lone_card_has_no_skip_button():
    markup = triage.card_keyboard(1, 0, 1)
    assert not any(b.callback_data.startswith("trg:") for r in markup.inline_keyboard for b in r)


def test_card_explains_rotating_addresses():
    group = {
        "name": "Watch",
        "count": 34,
        "mac_random": True,
        "ip": "",
        "last_seen": "2026-11-05T10:00",
        "owners": [{"id": 3, "name": "Марат"}],
    }
    text = triage.card_text(group, 0, 12, None)
    assert "1 из 12" in text and "Записей: 34" in text and "Марат" in text


def test_skip_wraps_around_and_edits_same_message(db):
    _device(db, 1)
    _device(db, 2, "Mac")
    cb = _press("trg:2")
    asyncio.run(triage.cb_card(cb))
    assert "1 из 2" in _edited(cb)[0]


def test_private_address_is_never_looked_up(db, monkeypatch):
    _device(db, 1)
    lookup = AsyncMock(return_value="Apple")
    monkeypatch.setattr(triage, "vendor_of", lookup)
    asyncio.run(triage._card(0))
    lookup.assert_not_awaited()


def test_people_with_matching_device_come_first(db):
    _person(db, "Аня")
    marat = _person(db)
    _device(db, 1, person_id=marat.id)
    first = _device(db, 2)
    cb = _press(f"trgp:{first.id}:0")
    asyncio.run(triage.cb_people(cb))
    assert _edited(cb)[1][0] == f"trgc:{first.id}:{marat.id}:0"


def test_big_group_asks_before_folding(db):
    first = _device(db, 1)
    _device(db, 2)
    cb = _press(f"trgc:{first.id}:iot:0")
    asyncio.run(triage.cb_choose(cb))
    text, data = _edited(cb)
    assert "2 записей" in text and f"trgy:{first.id}:iot:0" in data
    db.expire_all()
    assert db.query(Device).count() == 2


def test_single_record_applies_at_once_and_reports_done(db):
    first = _device(db, 1)
    cb = _press(f"trgc:{first.id}:iot:0")
    asyncio.run(triage.cb_choose(cb))
    text, _ = _edited(cb)
    assert "умный дом" in text and "разобраны" in text


def test_confirmed_folding_is_applied(db):
    first = _device(db, 1)
    _device(db, 2)
    cb = _press(f"trgy:{first.id}:shared:0")
    asyncio.run(triage.cb_apply(cb))
    db.expire_all()
    assert db.query(Device).one().kind == "shared"


def test_already_sorted_group_moves_on(db):
    _device(db, 1)
    cb = _press("trgy:999999:iot:0")
    asyncio.run(triage.cb_apply(cb))
    assert triage.ALREADY_SORTED in _edited(cb)[0]


def test_vendor_comes_from_the_bundled_registry():
    assert asyncio.run(triage.vendor_of("00:BB:C1:00:00:01")) == "CANON INC."
    assert asyncio.run(triage.vendor_of("not a mac")) is None
