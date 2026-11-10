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


def test_merge_keeps_screen_time_bonuses_and_request(db, monkeypatch):
    """Объединение переносит на настоящее устройство всё, что накопил дубль:
    минуты за сегодня, выданный бонус, висящую заявку и паузу.

    Без этого телефон, сменивший «приватный» MAC днём, после нажатия
    «объединить» терял часть экранного времени, а кнопка родителя под заявкой
    падала с «Не нашёл устройство»."""
    from datetime import datetime, timedelta

    from app.models import BonusRequest, Pause, QuotaBonus, QuotaUsage

    monkeypatch.setattr(tools.enforcement, "reconcile", lambda s: None)
    for m in (BonusRequest, QuotaBonus, QuotaUsage, Pause):
        db.query(m).delete()
    db.commit()

    target = Device(mac="00:11:22:33:44:55", ip="192.168.88.40", name="Телефон")
    dup = Device(mac="D2:BB:BB:BB:BB:02", ip="192.168.88.42", name="дубль")
    db.add_all([target, dup])
    db.commit()
    today = datetime.now().strftime("%Y-%m-%d")
    db.add_all([
        QuotaUsage(device_id=target.id, date=today, category="games", minutes=10),
        QuotaUsage(device_id=dup.id, date=today, category="games", minutes=25),
        QuotaUsage(device_id=dup.id, date=today, category="video", minutes=7),
        QuotaBonus(target_type="device", target=str(dup.id), date=today,
                   category="games", minutes=30),
        BonusRequest(device_id=dup.id, category="games", status="pending"),
        Pause(target_type="device", target=str(dup.id),
              until=datetime.now() + timedelta(hours=1)),
    ])
    db.commit()

    tools.run_tool(db, "merge_devices", {"duplicate_id": dup.id, "target_id": target.id})

    usage = {u.category: u.minutes for u in db.query(QuotaUsage)}
    assert usage == {"games": 35, "video": 7}  # минуты сложились, ничего не потеряно
    assert db.query(QuotaBonus).one().target == str(target.id)
    assert db.query(BonusRequest).one().device_id == target.id
    assert db.query(Pause).one().target == str(target.id)


def test_deleted_device_does_not_break_next_tick(db, monkeypatch):
    """Удаление устройства чистит связанные строки; даже если осиротевшая
    строка device_macs осталась от старой версии панели, тот же MAC в DHCP не
    должен ронять тик (раньше IntegrityError улетал наружу и пропускал ВЕСЬ
    reconcile: в эту минуту не применялись ни блокировки, ни квоты)."""
    from app.services.device_links import purge_device_rows

    dev = Device(mac="00:11:22:33:44:55", ip="192.168.88.40", name="Телефон")
    db.add(dev)
    db.commit()
    db.add(DeviceMac(device_id=dev.id, mac=dev.mac))
    db.commit()

    purge_device_rows(db, dev.id)
    db.delete(dev)
    db.commit()
    assert db.query(DeviceMac).count() == 0  # сирот не осталось

    # Имитируем базу, испорченную старой версией: сирота на месте.
    db.add(DeviceMac(device_id=999, mac="00:11:22:33:44:55"))
    db.commit()
    monkeypatch.setattr(enforcement.mikrotik, "get_leases",
                        lambda api: _leases(("00:11:22:33:44:55", "192.168.88.40", "phone")))
    devices = enforcement.discover_devices(db, api=None)  # не должно бросать
    assert len(devices) == 1
    assert db.query(DeviceMac).one().device_id == devices[0].id  # сирота перевешена


def test_hostname_from_device_is_sanitized(db, monkeypatch):
    """Имя из DHCP задаёт само устройство: MAC и «req#N» в нём — попытка
    подделать якоря, по которым бот разбирает свои же уведомления."""
    monkeypatch.setattr(
        enforcement.mikrotik, "get_leases",
        lambda api: _leases(("00:11:22:33:44:66", "192.168.88.43",
                             "req#7 AA:BB:CC:DD:EE:FF [взлом]")))
    devices = enforcement.discover_devices(db, api=None)
    name = devices[0].name
    assert "AA:BB:CC:DD:EE:FF" not in name and "req#7" not in name
    assert "[" not in name and "]" not in name


def test_migration_backfills_device_macs(tmp_path):
    """Миграция №1 на «старой» базе: добавляет hostname и переносит MAC."""
    eng = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with eng.begin() as c:
        # схема старой версии: devices без hostname, device_macs уже создана
        # create_all'ом при деплое
        c.execute(text("CREATE TABLE devices (id INTEGER PRIMARY KEY, "
                       "mac VARCHAR(17), ip VARCHAR(15), person_id INTEGER)"))
        c.execute(text("CREATE TABLE quota_usage (id INTEGER PRIMARY KEY, device_id INTEGER, "
                       "date VARCHAR(10), category VARCHAR(16), minutes INTEGER)"))
        c.execute(text("INSERT INTO quota_usage (device_id, date, category, minutes) VALUES "
                       "(1, '2026-11-01', 'internet', 5), (1, '2026-11-08', 'internet', 3)"))
        c.execute(text("CREATE TABLE device_macs (id INTEGER PRIMARY KEY, "
                       "device_id INTEGER, mac VARCHAR(17))"))
        c.execute(text("INSERT INTO devices (mac, ip) VALUES ('AA:BB:CC:DD:EE:FF', '')"))
    assert run_migrations(eng, MIGRATIONS) == len(MIGRATIONS)
    with eng.connect() as c:
        rows = c.execute(text("SELECT device_id, mac FROM device_macs")).fetchall()
        assert rows == [(1, "AA:BB:CC:DD:EE:FF")]
        c.execute(text("SELECT hostname FROM devices"))  # колонка появилась
        c.execute(text("SELECT tool FROM pending_actions"))  # таблица из миграции №2
        seen = c.execute(text("SELECT last_seen FROM devices")).scalar()
        assert seen == "2026-11-08 12:00:00.000000"  # migration 6 backfill
    assert run_migrations(eng, MIGRATIONS) == 0  # повторно — ничего


def _private(db, mac, ip, hostname, person=None, **kwargs):
    dev = Device(
        mac=mac,
        ip=ip,
        name=hostname,
        hostname=hostname,
        person_id=person.id if person else None,
        **kwargs,
    )
    db.add(dev)
    db.commit()
    db.add(DeviceMac(device_id=dev.id, mac=mac))
    db.commit()
    return dev


def test_rotated_private_address_folds_into_its_owner(db, monkeypatch):
    mom = Person(name="Мама", role="adult")
    db.add(mom)
    db.commit()
    watch = _private(db, "16:89:32:95:B6:1E", "192.168.88.38", "Watch", mom)
    monkeypatch.setattr(
        enforcement.mikrotik,
        "get_leases",
        lambda api: _leases(("BA:B8:F9:D1:E1:82", "192.168.88.41", "Watch")),
    )
    devices = enforcement.discover_devices(db, api=None, answering={"BA:B8:F9:D1:E1:82"})
    assert len(devices) == 1 and db.query(Device).count() == 1
    assert watch.ip == "192.168.88.41"
    assert {m.mac for m in db.query(DeviceMac)} == {"16:89:32:95:B6:1E", "BA:B8:F9:D1:E1:82"}
    kinds = [e.kind for e in db.query(EventLog)]
    assert "device_merged" in kinds and "device_new" not in kinds


def test_shared_name_between_people_is_not_folded(db, monkeypatch):
    mom, son = Person(name="Мама", role="adult"), Person(name="Сын", role="kid")
    db.add_all([mom, son])
    db.commit()
    _private(db, "92:F2:05:D5:F1:84", "192.168.88.54", "iPhone", mom)
    _private(db, "2E:F6:F1:2B:07:93", "192.168.88.44", "iPhone", son)
    monkeypatch.setattr(
        enforcement.mikrotik,
        "get_leases",
        lambda api: _leases(("56:6F:23:87:B0:71", "192.168.88.48", "iPhone")),
    )
    devices = enforcement.discover_devices(db, api=None, answering=set())
    new = next(d for d in devices if d.mac == "56:6F:23:87:B0:71")
    assert new.person_id is None  # a guest's iPhone must not get anyone's rights


def test_answering_twin_is_a_second_device(db, monkeypatch):
    mom = Person(name="Мама", role="adult")
    db.add(mom)
    db.commit()
    _private(db, "16:89:32:95:B6:1E", "192.168.88.38", "Watch", mom)
    monkeypatch.setattr(
        enforcement.mikrotik,
        "get_leases",
        lambda api: _leases(("BA:B8:F9:D1:E1:82", "192.168.88.41", "Watch")),
    )
    devices = enforcement.discover_devices(db, api=None, answering={"16:89:32:95:B6:1E"})
    assert len(devices) == 2


def test_mark_seen_stamps_online_devices_sparingly(db):
    from datetime import datetime, timedelta

    on = Device(mac="00:11:22:33:44:01", ip="192.168.88.10", name="on")
    off = Device(mac="00:11:22:33:44:02", ip="192.168.88.11", name="off")
    db.add_all([on, off])
    db.commit()
    now = datetime(2026, 11, 10, 12, 0)
    enforcement.mark_seen(db, [on, off], {"192.168.88.10"}, now)
    assert on.last_seen == now and off.last_seen is None
    enforcement.mark_seen(db, [on], {"192.168.88.10"}, now + timedelta(minutes=2))
    assert on.last_seen == now  # within SEEN_STEP: no write
    later = now + enforcement.SEEN_STEP
    enforcement.mark_seen(db, [on], {"192.168.88.10"}, later)
    assert on.last_seen == later


def test_migration_drops_pseudo_people(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'people.db'}")
    Base.metadata.create_all(eng)
    with eng.begin() as c:
        c.execute(
            text(
                "INSERT INTO people (id, name, role, bot_role) VALUES "
                "(1, 'Нурбек', 'adult', 'owner'), (9, 'Известные', 'guest', 'guest'), "
                "(10, 'Неизвестные', 'guest', 'guest')"
            )
        )
        c.execute(
            text(
                "INSERT INTO devices (mac, ip, name, hostname, kind, person_id, "
                "blocked_manual, speed_limit, first_seen) VALUES "
                "('58:6C:25:F9:7E:FA', '', 'Nurbek', '', 'personal', 9, 0, '', "
                "'2026-08-08 00:00:00'), "
                "('92:F2:05:D5:F1:84', '', 'iPhone', '', 'personal', 1, 0, '', "
                "'2026-07-13 00:00:00')"
            )
        )
        c.execute(text("PRAGMA user_version = 6"))
    run_migrations(eng, MIGRATIONS)
    with eng.connect() as c:
        assert c.execute(text("SELECT name FROM people")).scalars().all() == ["Нурбек"]
        owners = dict(c.execute(text("SELECT name, person_id FROM devices")).fetchall())
        assert owners == {"Nurbek": None, "iPhone": 1}
