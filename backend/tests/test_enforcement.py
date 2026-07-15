"""Проверяет расчёт желаемого состояния сети из БД — чистая логика без сети."""


from datetime import datetime

import pytest

from app.config import settings
from app.db import Base, engine, session
from app.models import Device, GroupPolicy, Person, Quota, QuotaUsage, Rule
from app.services.enforcement import DOH_SERVER_IPS, _desired_state


@pytest.fixture
def db():
    Base.metadata.create_all(engine)
    s = session()
    # чистим таблицы, чтобы тесты не влияли друг на друга
    for model in (Rule, GroupPolicy, Quota, QuotaUsage, Device, Person):
        s.query(model).delete()
    s.commit()
    yield s
    s.close()


def test_groups_blocks_queues_and_adguard(db):
    kid = Person(name="Kid", role="kid")
    adult = Person(name="Adult", role="adult")
    db.add_all([kid, adult])
    db.commit()

    devices = [
        Device(mac="AA:00:00:00:00:01", ip="192.168.88.10", name="kid-pc", person_id=kid.id),
        Device(mac="AA:00:00:00:00:02", ip="192.168.88.11", name="adult-phone", person_id=adult.id),
        Device(mac="AA:00:00:00:00:03", ip="192.168.88.12", name="mystery"),
        Device(mac="AA:00:00:00:00:04", ip="192.168.88.13", name="tv",
               blocked_manual=True, speed_limit="5M/20M"),
        Device(mac="AA:00:00:00:00:05", ip="", name="offline"),
    ]
    db.add_all(devices)
    db.add(GroupPolicy(group="kid", blocked_services="games,video", safe_search=True))
    # правило, активное прямо сейчас
    db.add(Rule(name="now", target_type="group", target="kid",
                days="0,1,2,3,4,5,6", start_time="00:00", end_time="23:59", enabled=True))
    db.commit()

    st = _desired_state(db, list(db.query(Device).all()))
    lists = st["lists"]

    assert lists["hs-kids"] == {"192.168.88.10"}
    assert "192.168.88.10" in lists["hs-blocked"]   # ребёнок под активным правилом
    assert "192.168.88.13" in lists["hs-blocked"]   # ручная блокировка
    assert "192.168.88.11" not in lists["hs-blocked"]  # взрослый свободен
    assert "192.168.88.11" not in lists["hs-managed"]  # взрослый без лимитов — fasttrack
    assert "192.168.88.13" in lists["hs-managed"]
    assert st["queues"] == {"192.168.88.13": "5M/20M"}
    assert lists["hs-doh"] == set(DOH_SERVER_IPS)

    ag = st["ag_clients"]
    assert ag["hs-aa0000000001"]["safe_search"] is True
    assert "youtube" in ag["hs-aa0000000001"]["blocked_services"]
    assert "steam" in ag["hs-aa0000000001"]["blocked_services"]
    # устройство без IP не попадает ни в один список
    assert all(d["ip"] for d in ag.values())


def test_duplicate_ip_yields_single_adguard_client(db):
    """Страховка: даже если в базе два устройства с одним IP (окно между
    тиками), в AdGuard уходит ОДИН клиент — иначе 400 на clients/add."""
    kid = Person(name="Kid", role="kid")
    db.add(kid)
    db.commit()
    db.add_all([
        Device(mac="CC:00:00:00:00:01", ip="192.168.88.248", name="старый", person_id=kid.id),
        Device(mac="CC:00:00:00:00:02", ip="192.168.88.248", name="новый", person_id=kid.id),
    ])
    db.add(GroupPolicy(group="kid", blocked_services="games", safe_search=False))
    db.commit()

    ag = _desired_state(db, list(db.query(Device).all()))["ag_clients"]
    assert len(ag) == 1
    assert next(iter(ag.values()))["ip"] == "192.168.88.248"


def test_quota_block_reason_marked_for_notification(db):
    """Исчерпанная интернет-квота видна в quota_blocked — reconcile по этой
    карте пишет событие quota_block (с MAC для notify.py) вместо безликого block."""
    kid = Person(name="Kid", role="kid")
    db.add(kid)
    db.commit()
    dev = Device(mac="AA:00:00:00:00:31", ip="192.168.88.31", name="Планшет",
                 person_id=kid.id)
    manual = Device(mac="AA:00:00:00:00:32", ip="192.168.88.32", name="tv",
                    blocked_manual=True)
    db.add_all([dev, manual])
    db.commit()
    db.add(Quota(target_type="device", target=str(dev.id), category="internet",
                 minutes_per_day=30))
    db.add(QuotaUsage(device_id=dev.id, date=datetime.now().strftime("%Y-%m-%d"),
                      category="internet", minutes=30))
    db.commit()

    st = _desired_state(db, list(db.query(Device).all()))
    assert dev.ip in st["lists"]["hs-blocked"]
    assert manual.ip in st["lists"]["hs-blocked"]
    # причина «квота» — только у квотного устройства, не у ручной блокировки
    assert set(st["quota_blocked"]) == {dev.ip}
    assert st["quota_blocked"][dev.ip] is dev


def test_block_unknown_toggle(db, monkeypatch):
    db.add(Device(mac="BB:00:00:00:00:01", ip="192.168.88.50", name="guest-laptop"))
    db.commit()
    devices = list(db.query(Device).all())

    monkeypatch.setattr(settings, "block_unknown", False)
    assert "192.168.88.50" not in _desired_state(db, devices)["lists"]["hs-blocked"]

    monkeypatch.setattr(settings, "block_unknown", True)
    assert "192.168.88.50" in _desired_state(db, devices)["lists"]["hs-blocked"]
