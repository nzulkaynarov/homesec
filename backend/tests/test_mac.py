"""Анти-MAC-рандомизация: детект случайного MAC, матчинг lease по всем MAC
устройства, подозрение на двойника по hostname, объединение, миграция."""

import pytest
from sqlalchemy import create_engine, text

from app.ai import tools
from app.db import Base, engine, session
from app.migrations import MIGRATIONS, run_migrations
from app.models import Device, DeviceMac, EventLog, KVState, Person, is_random_mac
from app.services import enforcement


@pytest.fixture
def db():
    Base.metadata.create_all(engine)
    s = session()
    for model in (DeviceMac, EventLog, KVState, Device, Person):
        s.query(model).delete()
    s.commit()
    yield s
    s.close()


def test_is_random_mac():
    assert is_random_mac("D2:11:22:33:44:55")  # 2-й символ 2
    assert is_random_mac("AA:11:22:33:44:55")  # A
    assert is_random_mac("f6:11:22:33:44:55")  # 6, нижний регистр
    assert not is_random_mac("D0:11:22:33:44:55")
    assert not is_random_mac("00:1A:2B:3C:4D:5E")


def _leases(*rows):
    return [{"mac": m, "ip": ip, "hostname": h, "dynamic": True, "status": "bound",
             "server": "defconf", "id": "*1"} for m, ip, h in rows]


def test_discover_matches_alias_mac(db, monkeypatch):
    dev = Device(mac="00:11:22:33:44:55", ip="192.168.88.40",
                 name="Телефон", hostname="mishas-phone")
    db.add(dev)
    db.commit()
    db.add_all([DeviceMac(device_id=dev.id, mac="00:11:22:33:44:55"),
                DeviceMac(device_id=dev.id, mac="D2:AA:AA:AA:AA:01")])
    db.commit()

    # lease пришёл со вторым (случайным) MAC и новым IP: дубль НЕ создаётся
    monkeypatch.setattr(enforcement.mikrotik, "get_leases",
                        lambda api: _leases(("D2:AA:AA:AA:AA:01", "192.168.88.41",
                                             "mishas-phone")))
    devices = enforcement.discover_devices(db, api=None)
    assert len(devices) == 1
    assert dev.ip == "192.168.88.41"  # адрес переехал на устройство
    assert not any(e.kind == "device_new" for e in db.query(EventLog))


def test_discover_releases_stale_ip(db, monkeypatch):
    """Ротация «приватного» MAC: DHCP выдал старый IP новому MAC — протухшая
    запись должна отдать адрес, иначе AdGuard получает двух клиентов с одним
    ids и отвечает 400 на каждом тике (инцидент 2026-07-15)."""
    stale = Device(mac="FE:8B:73:34:03:35", ip="192.168.88.248", name="старый iPhone")
    db.add(stale)
    db.commit()
    monkeypatch.setattr(enforcement.mikrotik, "get_leases",
                        lambda api: _leases(("56:6F:23:87:B0:71", "192.168.88.248",
                                             "iphone")))
    devices = enforcement.discover_devices(db, api=None)
    fresh = next(d for d in devices if d.mac == "56:6F:23:87:B0:71")
    assert fresh.ip == "192.168.88.248"
    assert stale.ip == ""  # адрес отобран у протухшей записи
    assert {d.ip for d in devices if d.ip} == {"192.168.88.248"}  # дублей нет
    assert any(e.kind == "ip_conflict" for e in db.query(EventLog))


def test_discover_flags_twin_by_hostname(db, monkeypatch):
    dev = Device(mac="00:11:22:33:44:55", ip="192.168.88.40",
                 name="Телефон", hostname="mishas-phone")
    db.add(dev)
    db.commit()
    monkeypatch.setattr(enforcement.mikrotik, "get_leases",
                        lambda api: _leases(("D2:BB:BB:BB:BB:02", "192.168.88.42",
                                             "mishas-phone")))
    devices = enforcement.discover_devices(db, api=None)
    assert len(devices) == 2  # дубль создан, но помечен кандидатом
    events = {e.kind: e.message for e in db.query(EventLog)}
    assert "⚠️ случайный MAC" in events["device_new"]
    assert "device_maybe_same" in events
    assert "00:11:22:33:44:55" in events["device_maybe_same"]


def test_merge_devices_tool(db, monkeypatch):
    monkeypatch.setattr(tools.enforcement, "reconcile", lambda s: None)
    kid = Person(name="Миша", role="kid")
    db.add(kid)
    db.commit()
    target = Device(mac="00:11:22:33:44:55", ip="192.168.88.40",
                    name="Телефон", person_id=kid.id, hostname="mishas-phone")
    dup = Device(mac="D2:BB:BB:BB:BB:02", ip="192.168.88.42",
                 name="D2:BB:BB:BB:BB:02", hostname="mishas-phone")
    db.add_all([target, dup])
    db.commit()
    db.add_all([DeviceMac(device_id=target.id, mac=target.mac),
                DeviceMac(device_id=dup.id, mac=dup.mac)])
    db.commit()

    msg = tools.run_tool(db, "merge_devices",
                         {"duplicate_id": dup.id, "target_id": target.id})
    assert "Телефон" in msg
    assert db.query(Device).count() == 1
    macs = {dm.mac for dm in db.query(DeviceMac)}
    assert macs == {"00:11:22:33:44:55", "D2:BB:BB:BB:BB:02"}
    assert target.ip == "192.168.88.42"  # свежий адрес дубля
    assert target.person_id == kid.id  # владелец сохранён

    with pytest.raises(tools.ToolError):
        tools.run_tool(db, "merge_devices",
                       {"duplicate_id": target.id, "target_id": target.id})


def test_migration_backfills_device_macs(tmp_path):
    """Миграция №1 на «старой» базе: добавляет hostname и переносит MAC."""
    eng = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with eng.begin() as c:
        # схема старой версии: devices без hostname, device_macs уже создана
        # create_all'ом при деплое
        c.execute(text("CREATE TABLE devices (id INTEGER PRIMARY KEY, "
                       "mac VARCHAR(17), ip VARCHAR(15))"))
        c.execute(text("CREATE TABLE device_macs (id INTEGER PRIMARY KEY, "
                       "device_id INTEGER, mac VARCHAR(17))"))
        c.execute(text("INSERT INTO devices (mac, ip) VALUES ('AA:BB:CC:DD:EE:FF', '')"))
    assert run_migrations(eng, MIGRATIONS) == 1
    with eng.connect() as c:
        rows = c.execute(text("SELECT device_id, mac FROM device_macs")).fetchall()
        assert rows == [(1, "AA:BB:CC:DD:EE:FF")]
        c.execute(text("SELECT hostname FROM devices"))  # колонка появилась
    assert run_migrations(eng, MIGRATIONS) == 0  # повторно — ничего
