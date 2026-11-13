"""«⏸ Пауза»: any child, any child's device or a shared device goes offline for a chosen time."""

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import Message

from app.ai import tools
from app.bot import access, device_menu, pause_menu
from app.db import Base, engine, session
from app.models import Device, EventLog, KVState, Pause, Person, Rule, kv_get
from app.services import enforcement, pauses, unlock
from app.services.pauses import PauseError, Target

CHAT, USER = 300, 7


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr(tools.enforcement, "get_self_ips", lambda: {"192.168.88.2"})
    monkeypatch.setattr(device_menu, "vendor_of", AsyncMock(return_value=None))
    Base.metadata.create_all(engine)
    s = session()
    for model in (EventLog, KVState, Pause, Rule, Device, Person):
        s.query(model).delete()
    s.commit()
    pause_menu._custom.clear()
    yield s
    s.close()


def _family(db):
    misha = Person(name="Миша", role="kid", bot_role="kid")
    anya = Person(name="Аня", role="kid", bot_role="kid")
    db.add_all([misha, anya])
    db.commit()
    phone = Device(mac="00:11:22:33:44:01", ip="192.168.88.51", name="Телефон Миши",
                   person_id=misha.id, kind="personal")
    tablet = Device(mac="00:11:22:33:44:02", ip="192.168.88.52", name="Планшет Миши",
                    person_id=misha.id, kind="personal")
    anya_phone = Device(mac="00:11:22:33:44:03", ip="192.168.88.53", name="Телефон Ани",
                        person_id=anya.id, kind="personal")
    tv = Device(mac="00:11:22:33:44:04", ip="192.168.88.60", name="Телевизор", kind="shared")
    router = Device(mac="00:11:22:33:44:05", ip="192.168.88.5", name="Точка доступа",
                    kind="infra")
    db.add_all([phone, tablet, anya_phone, tv, router])
    db.commit()
    return misha, anya, phone, tablet, anya_phone, tv, router


def _press(data: str) -> MagicMock:
    cb = MagicMock(data=data, answer=AsyncMock())
    cb.from_user.id = USER
    cb.message = MagicMock(spec=Message, edit_text=AsyncMock())
    cb.message.chat = MagicMock(id=CHAT)
    return cb


def _run(handler, data: str) -> tuple[str, list[str]]:
    cb = _press(data)
    asyncio.run(handler(cb))
    call = cb.message.edit_text.await_args
    return call.args[0], [
        b.callback_data for row in call.kwargs["reply_markup"].inline_keyboard for b in row
    ]


def _typed(text: str) -> MagicMock:
    message = MagicMock(spec=Message, text=text, answer=AsyncMock())
    message.chat = MagicMock(id=CHAT)
    message.from_user = MagicMock(id=USER)
    return message


def _blocked_ips(db) -> set[str]:
    devices = list(db.query(Device))
    return set(enforcement._desired_state(db, devices)["lists"]["hs-blocked"])


def test_overview_offers_every_child_and_shared_device_but_not_equipment(db):
    misha, anya, *_rest, tv, router = _family(db)
    text, actions = asyncio.run(pause_menu.overview())
    data = [b.callback_data for row in actions.inline_keyboard for b in row]
    assert "pzt:a" in data and f"pzk:{misha.id}" in data and f"pzk:{anya.id}" in data
    assert f"pzt:d{tv.id}" in data and f"pzt:d{router.id}" not in data
    assert "пауз нет" in text


def test_child_screen_lists_each_device(db):
    misha, _anya, phone, tablet, *_ = _family(db)
    _, actions = _run(pause_menu.cb_kid, f"pzk:{misha.id}")
    assert f"pzt:p{misha.id}" in actions
    assert f"pzt:d{phone.id}" in actions and f"pzt:d{tablet.id}" in actions


def test_shared_device_goes_offline_for_the_chosen_time(db):
    *_, tv, _router = _family(db)
    before = datetime.now()
    text, actions = _run(pause_menu.cb_set, f"pzs:d{tv.id}:30")
    row = db.query(Pause).one()
    assert (row.target_type, row.target) == ("device", str(tv.id))
    assert timedelta(minutes=29) < row.until - before < timedelta(minutes=31)
    assert "Телевизор: без интернета до" in text and f"pzr:d{tv.id}" in actions
    assert kv_get(db, "reconcile_asap") == "1"  # the router hears within seconds
    assert _blocked_ips(db) == {"192.168.88.60"}


def test_one_childs_device_only(db):
    _misha, _anya, phone, *_ = _family(db)
    _run(pause_menu.cb_set, f"pzs:d{phone.id}:60")
    assert _blocked_ips(db) == {"192.168.88.51"}


def test_one_child_all_devices_and_not_the_sibling(db):
    misha, *_ = _family(db)
    _run(pause_menu.cb_set, f"pzs:p{misha.id}:15")
    assert _blocked_ips(db) == {"192.168.88.51", "192.168.88.52"}


def test_all_children_until_morning(db):
    _family(db)
    text, _ = _run(pause_menu.cb_set, "pzs:a:m")
    assert _blocked_ips(db) == {"192.168.88.51", "192.168.88.52", "192.168.88.53"}
    assert all(p.until.hour == 7 for p in db.query(Pause))  # no bedtime set: 07:00
    assert "Все дети: без интернета до 07:00" in text


def test_a_new_time_replaces_the_old_one(db):
    *_, tv, _router = _family(db)
    _run(pause_menu.cb_set, f"pzs:d{tv.id}:180")
    _run(pause_menu.cb_set, f"pzs:d{tv.id}:15")
    row = db.query(Pause).one()
    assert row.until - datetime.now() < timedelta(minutes=16)


def test_switch_on_now(db):
    *_, tv, _router = _family(db)
    _run(pause_menu.cb_set, f"pzs:d{tv.id}:60")
    text, actions = _run(pause_menu.cb_resume, f"pzr:d{tv.id}")
    assert db.query(Pause).count() == 0 and _blocked_ips(db) == set()
    assert "интернет снова включён" in text and f"pzr:d{tv.id}" not in actions


def test_resume_all(db):
    misha, _anya, _phone, _tablet, _anya_phone, tv, _router = _family(db)
    _run(pause_menu.cb_set, f"pzs:d{tv.id}:60")
    _run(pause_menu.cb_set, f"pzs:p{misha.id}:60")
    text, _ = _run(pause_menu.cb_resume_all, "pzra")
    assert db.query(Pause).count() == 0 and "Сняты все паузы: 2" in text


def test_typed_time(db):
    *_, tv, _router = _family(db)
    text, _ = _run(pause_menu.cb_custom, f"pzc:d{tv.id}")
    assert "На сколько отключить «Телевизор»" in text
    message = _typed("45")
    assert pause_menu.waiting_for_time(message)
    asyncio.run(pause_menu.on_time(message))
    row = db.query(Pause).one()
    assert timedelta(minutes=44) < row.until - datetime.now() < timedelta(minutes=46)
    assert "без интернета до" in message.answer.await_args.args[0]


def test_unclear_typed_time_asks_again(db):
    *_, tv, _router = _family(db)
    _run(pause_menu.cb_custom, f"pzc:d{tv.id}")
    message = _typed("попозже")
    asyncio.run(pause_menu.on_time(message))
    assert db.query(Pause).count() == 0
    assert "Не понял время" in message.answer.await_args.args[0]
    assert pause_menu.waiting_for_time(_typed("20"))  # still waiting for a time


def test_another_button_cancels_typing(db):
    *_, tv, _router = _family(db)
    _run(pause_menu.cb_custom, f"pzc:d{tv.id}")
    assert not pause_menu.waiting_for_time(_typed("📱 Устройства"))


@pytest.mark.parametrize(
    ("text", "minutes"),
    [("45", 45), ("45 мин", 45), ("2ч", 120), ("1,5 часа", 90), ("2 часа", 120)],
)
def test_parse_durations(text, minutes):
    now = datetime(2026, 11, 13, 17, 0)
    assert pauses.parse_until(text, now) == now + timedelta(minutes=minutes)


def test_parse_clock_times():
    now = datetime(2026, 11, 13, 17, 0)
    assert pauses.parse_until("до 18:30", now) == datetime(2026, 11, 13, 18, 30)
    assert pauses.parse_until("8:00", now) == datetime(2026, 11, 14, 8, 0)  # tomorrow morning
    assert pauses.parse_until("25:00", now) is None
    assert pauses.parse_until("скоро", now) is None


def test_limits(db):
    *_, tv, router = _family(db)
    now = datetime.now()
    with pytest.raises(PauseError):
        pauses.pause(db, Target("device", router.id), now + timedelta(minutes=30))
    with pytest.raises(PauseError):
        pauses.pause(db, Target("device", tv.id), now + timedelta(hours=25))
    with pytest.raises(PauseError):
        pauses.pause(db, Target("device", tv.id), now - timedelta(minutes=1))


def test_a_new_pause_closes_an_open_window(db):
    misha, *_ = _family(db)
    unlock.open_window(db, misha.id, datetime.now() + timedelta(hours=1))
    _run(pause_menu.cb_set, f"pzs:p{misha.id}:30")
    assert unlock.open_until(db, misha.id) is None
    assert "192.168.88.51" in _blocked_ips(db)


def test_device_card_offers_pause_and_shows_it(db):
    *_, tv, router = _family(db)
    text, markup = asyncio.run(device_menu._device(tv.id, "shared", 0))
    data = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert f"pzt:d{tv.id}" in data
    pauses.pause(db, Target("device", tv.id), datetime.now() + timedelta(minutes=30))
    text, _ = asyncio.run(device_menu._device(tv.id, "shared", 0))
    assert "⏸ Без интернета до" in text
    _, markup = asyncio.run(device_menu._device(router.id, "infra", 0))
    assert f"pzt:d{router.id}" not in [b.callback_data for r in markup.inline_keyboard for b in r]


def test_children_cannot_pause(db):
    for data in ("pzm", "pzt:a", "pzs:a:30", "pzr:a", "pzra"):
        assert not access.check_callback("kid", data).allowed
    assert not access.check_message("kid", pause_menu.BTN_PAUSE).allowed
