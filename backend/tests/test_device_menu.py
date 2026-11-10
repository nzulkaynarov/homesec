"""Moving any device to another owner or kind from the Telegram menu."""

import asyncio
from datetime import datetime, timedelta
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


CHAT, USER = 100, 7


def _press(data: str) -> MagicMock:
    cb = MagicMock(data=data, answer=AsyncMock())
    cb.from_user.id = USER
    cb.message = MagicMock(spec=Message, edit_text=AsyncMock())
    cb.message.chat = MagicMock(id=CHAT)
    return cb


def _typed(text: str) -> MagicMock:
    message = MagicMock(spec=Message, text=text, answer=AsyncMock())
    message.chat = MagicMock(id=CHAT)
    message.from_user = MagicMock(id=USER)
    return message


def _run(handler, data: str) -> tuple[str, list[str]]:
    cb = _press(data)
    asyncio.run(handler(cb))
    call = cb.message.edit_text.await_args
    return call.args[0], [
        b.callback_data for row in call.kwargs["reply_markup"].inline_keyboard for b in row
    ]


def _gone(db, device: Device) -> bool:
    device_id = device.__dict__.get("id") or device.id  # read before expiry reloads it
    db.expire_all()
    return db.query(Device).filter_by(id=device_id).count() == 0


def _fresh(db, device: Device) -> Device:
    db.expire_all()
    return db.get(Device, device.id)


def test_overview_counts_owners_and_kinds(db):
    person = _person(db)
    _device(db, 10, person_id=person.id)
    _device(db, 11, kind="shared")
    _device(db, 12)
    text, actions = _run(device_menu.cb_overview, "dm")
    assert "Все устройства" in text
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


def test_overview_lists_every_device_with_what_tells_it_apart(db):
    person = _person(db)
    _device(db, 10, name="Igra", hostname="iPad-Kamila", person_id=person.id)
    _device(db, 11, name="Sony", kind="shared")
    text, _ = _run(device_menu.cb_overview, "dm")
    assert "• Igra (iPad-Kamila) · .10" in text and "• Sony · .11" in text
    assert "👤 Камила: 1" in text and "📺 Общие: 1" in text


def test_overview_stays_within_the_telegram_limit(db):
    for octet in range(1, 200):
        _device(db, octet, name="Очень длинное имя устройства " * 2)
    text, _ = _run(device_menu.cb_overview, "dm")
    assert len(text) <= device_menu.TEXT_LIMIT


def _ago(**kwargs) -> datetime:
    return datetime.now() - timedelta(**kwargs)


def test_card_shows_when_last_seen_and_new_actions(db):
    device = _device(db, 10, last_seen=_ago(days=3))
    text, actions = _run(device_menu.cb_device, f"dmd:{device.id}:none:0")
    assert "В сети: 3 дн назад" in text
    for prefix in ("dmr:", "dmm:", "dmx:", "dmb:"):
        assert any(a.startswith(prefix) for a in actions), prefix


def test_online_device_cannot_be_deleted(db):
    device = _device(db, 10, last_seen=_ago(minutes=1))
    text, actions = _run(device_menu.cb_device, f"dmd:{device.id}:none:0")
    assert "сейчас в сети" in text and not any(a.startswith("dmx:") for a in actions)
    text, _ = _run(device_menu.cb_delete, f"dmxy:{device.id}:none:0")
    assert device_menu.STILL_ONLINE in text and not _gone(db, device)


def test_delete_asks_then_removes(db):
    device = _device(db, 10, last_seen=_ago(days=40))
    text, actions = _run(device_menu.cb_delete_confirm, f"dmx:{device.id}:none:0")
    assert "Удалить" in text and f"dmxy:{device.id}:none:0" in actions
    text, _ = _run(device_menu.cb_delete, f"dmxy:{device.id}:none:0")
    assert "удалено" in text and _gone(db, device)
    assert db.get(KVState, "reconcile_asap").value == "1"


def test_rename_takes_the_next_typed_message(db):
    device = _device(db, 10, name="iPhone")
    text, _ = _run(device_menu.cb_rename, f"dmr:{device.id}:none:0")
    assert "Как назвать" in text
    message = _typed("Телефон бабушки")
    assert device_menu.waiting_for_name(message)
    asyncio.run(device_menu.on_name(message))
    assert _fresh(db, device).name == "Телефон бабушки"
    assert "✅" in message.answer.await_args.args[0]
    assert not device_menu.waiting_for_name(_typed("ещё текст"))


def test_menu_button_cancels_rename(db):
    device = _device(db, 10)
    _run(device_menu.cb_rename, f"dmr:{device.id}:none:0")
    assert not device_menu.waiting_for_name(_typed(device_menu.BTN_DEVICES))
    assert not device_menu.waiting_for_name(_typed("Новое имя"))


def test_merge_offers_same_name_first_and_folds(db):
    person = _person(db)
    keep = _device(db, 10, name="Watch", hostname="Watch", person_id=person.id)
    _device(db, 11, name="Sony", hostname="Bravia")
    fold = _device(db, 12, name="Watch", hostname="Watch", person_id=person.id)
    _, actions = _run(device_menu.cb_merge_pick, f"dmm:{keep.id}:none:0")
    picks = [a for a in actions if a.startswith("dmg:")]
    assert picks[0] == f"dmg:{keep.id}:{fold.id}:none:0"
    text, actions = _run(device_menu.cb_merge_confirm, f"dmg:{keep.id}:{fold.id}:none:0")
    assert "Отменить нельзя" in text
    text, _ = _run(device_menu.cb_merge, f"dmgy:{keep.id}:{fold.id}:none:0")
    assert "Объединено" in text
    assert _gone(db, fold) and not _gone(db, keep)


def test_block_and_unblock_from_the_card(db):
    device = _device(db, 10)
    text, actions = _run(device_menu.cb_block, f"dmb:{device.id}:1:none:0")
    assert _fresh(db, device).blocked_manual and f"dmb:{device.id}:0:none:0" in actions
    _run(device_menu.cb_block, f"dmb:{device.id}:0:none:0")
    assert not _fresh(db, device).blocked_manual


def test_stale_screen_purges_only_ownerless_unblocked(db):
    person = _person(db)
    gone = _device(db, 10, name="Старый", last_seen=_ago(days=40))
    mine = _device(db, 11, name="Мой", person_id=person.id, last_seen=_ago(days=40))
    held = _device(db, 12, name="Блок", blocked_manual=True, last_seen=_ago(days=40))
    fresh = _device(db, 13, name="Свежий", last_seen=_ago(days=2))
    text, actions = _run(device_menu.cb_overview, "dm")
    assert "dms:stale:0" in actions
    text, actions = _run(device_menu.cb_section, "dms:stale:0")
    assert "больше 30 дней: 3" in text and "dmz" in actions
    text, _ = _run(device_menu.cb_purge_confirm, "dmz")
    assert "Старый" in text and "Мой" not in text and "Блок" not in text
    _run(device_menu.cb_purge, "dmzy")
    assert _gone(db, gone)
    assert all(not _gone(db, d) for d in (mine, held, fresh))


def test_never_seen_old_record_counts_as_stale(db):
    device = _device(db, 10, first_seen=_ago(days=90))
    assert device_owners.is_stale(device) and device in device_owners.stale(db)


def test_new_callback_data_fits_telegram_limit():
    assert len(f"dmgy:{10**9}:{10**9}:p{10**9}:999".encode()) <= 64
