"""Network equipment and marked devices: one message when one drops off, one when it is back."""

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import Message

from app.bot import device_menu
from app.db import Base, engine, session
from app.models import Device, KVState, Person
from app.services import enforcement, uptime

NOW = datetime(2026, 11, 17, 12, 30)


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr(enforcement, "get_self_ips", lambda: {"192.168.88.2"})
    Base.metadata.create_all(engine)
    s = session()
    for model in (KVState, Device, Person):
        s.query(model).delete()
    s.commit()
    monkeypatch.setattr(uptime.enforcement, "enforcement_status", lambda db, now: (True, ""))
    yield s
    s.close()


def _device(db, octet, name, kind="personal", seen_ago=timedelta(minutes=1), **kwargs):
    device = Device(
        mac=f"00:11:22:33:44:{octet:02X}",
        ip=f"192.168.88.{octet}",
        name=name,
        kind=kind,
        last_seen=NOW - seen_ago,
        **kwargs,
    )
    db.add(device)
    db.commit()
    return device


def _deliver(db, now):
    found = uptime.changes(db, now)
    for change in found:
        uptime.record(db, change)
    return found


def test_lost_equipment_is_announced_once_and_its_return_too(db):
    router = _device(db, 250, "ArcherC6", kind="infra", seen_ago=timedelta(minutes=14))
    [lost] = _deliver(db, NOW)
    assert lost.down and "«ArcherC6»" in lost.text and "с 12:16" in lost.text
    assert _deliver(db, NOW + timedelta(minutes=5)) == []
    router.last_seen = NOW + timedelta(minutes=20)
    db.commit()
    [back] = _deliver(db, NOW + timedelta(minutes=21))
    assert not back.down and "снова в сети" in back.text and "не было 35 мин" in back.text
    assert _deliver(db, NOW + timedelta(minutes=22)) == []


def test_only_equipment_and_marked_devices_are_watched(db):
    _device(db, 30, "Телефон", seen_ago=timedelta(hours=3))
    _device(db, 40, "Колонка", kind="iot", seen_ago=timedelta(hours=3))
    _device(db, 121, "IP_camera", kind="iot", seen_ago=timedelta(hours=3), watched=True)
    assert [c.text.split("»")[0] for c in uptime.changes(db, NOW)] == ["🔌 «IP_camera"]


def test_short_silence_and_the_pi_itself_are_quiet(db):
    _device(db, 15, "Server Luna", kind="infra", seen_ago=timedelta(minutes=8))
    _device(db, 2, "raspberrypi", kind="infra", seen_ago=timedelta(hours=12))
    assert uptime.changes(db, NOW) == []


def test_nothing_is_judged_without_a_recent_reconcile(db, monkeypatch):
    _device(db, 250, "ArcherC6", kind="infra", seen_ago=timedelta(hours=12))
    monkeypatch.setattr(uptime.enforcement, "enforcement_status", lambda db, now: (False, "x"))
    assert uptime.changes(db, NOW) == []


def test_unwatching_forgets_an_announced_loss(db):
    camera = _device(db, 121, "IP_camera", kind="iot", seen_ago=timedelta(hours=1), watched=True)
    assert len(_deliver(db, NOW)) == 1
    assert "Больше не слежу" in uptime.set_watched(db, camera.id, False)
    assert uptime.changes(db, NOW) == []
    assert "Сообщу" in uptime.set_watched(db, camera.id, True)
    assert len(uptime.changes(db, NOW)) == 1  # watched again, still missing: told again


def test_card_toggles_watching(db, monkeypatch):
    monkeypatch.setattr(device_menu, "vendor_of", AsyncMock(return_value=""))
    camera = _device(db, 121, "IP_camera", kind="iot")
    text, keyboard = asyncio.run(device_menu._device(camera.id, "iot", 0))
    buttons = {b.text: b.callback_data for row in keyboard.inline_keyboard for b in row}
    assert "🔔 Сообщу" not in text and buttons["🔔 Следить за связью"] == f"dmw:{camera.id}:1:iot:0"
    cb = MagicMock(data=f"dmw:{camera.id}:1:iot:0")
    cb.answer = AsyncMock()
    cb.message = MagicMock(spec=Message)
    cb.message.edit_text = AsyncMock()
    asyncio.run(device_menu.cb_watch(cb))
    db.refresh(camera)
    assert camera.watched
    shown = cb.message.edit_text.await_args.args[0]
    assert "Сообщу, если «IP_camera» пропадёт" in shown and "🔔 Сообщу, если пропадёт" in shown


def test_equipment_card_has_no_toggle_but_says_it_is_watched(db, monkeypatch):
    monkeypatch.setattr(device_menu, "vendor_of", AsyncMock(return_value=""))
    router = _device(db, 250, "ArcherC6", kind="infra")
    text, keyboard = asyncio.run(device_menu._device(router.id, "infra", 0))
    buttons = [b.text for row in keyboard.inline_keyboard for b in row]
    assert "🔔 Сообщу, если пропадёт из сети" in text
    assert not any("за связью" in b for b in buttons)
