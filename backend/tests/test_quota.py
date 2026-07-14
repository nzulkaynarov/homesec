"""Квоты времени: учёт активных минут по журналу DNS, применение в
_desired_state, бонусы, инструменты."""

from datetime import datetime

import pytest

from app.ai import tools
from app.db import Base, engine, session
from app.models import (
    Device,
    EventLog,
    KVState,
    Pause,
    Person,
    Quota,
    QuotaBonus,
    QuotaUsage,
)
from app.services import quota
from app.services.enforcement import _desired_state

NOW = datetime(2026, 7, 14, 15, 0)  # вторник (weekday=1)


@pytest.fixture
def db():
    Base.metadata.create_all(engine)
    s = session()
    for model in (Quota, QuotaUsage, QuotaBonus, Pause, EventLog, KVState, Device, Person):
        s.query(model).delete()
    s.commit()
    yield s
    s.close()


@pytest.fixture
def kid_device(db):
    kid = Person(name="Миша", role="kid")
    db.add(kid)
    db.commit()
    d = Device(mac="AA:00:00:00:00:01", ip="192.168.88.30", name="Планшет", person_id=kid.id)
    db.add(d)
    db.commit()
    return d


def _qlog(ip: str, domains: list[str], ts: str):
    return [{"client": ip, "question": {"name": d}, "time": ts} for d in domains]


def test_domain_category():
    assert quota.domain_category("roblox.com") == "games"
    assert quota.domain_category("cdn.ROBLOX.com.") == "games"
    assert quota.domain_category("youtube.com") == "video"
    assert quota.domain_category("discordapp.net") == "social"
    assert quota.domain_category("wikipedia.org") is None
    assert quota.domain_category("notroblox.com") is None  # только суффикс с точкой


def test_parse_ts_nanoseconds():
    ts = quota._parse_ts("2026-07-14T15:00:00.123456789+05:00")
    assert ts is not None and ts.year == 2026
    assert quota._parse_ts("мусор") is None


def test_record_activity_thresholds(db, kid_device, monkeypatch):
    recent = NOW.strftime("%Y-%m-%dT%H:%M:%S") + "+05:00"
    monkeypatch.setattr(quota, "_parse_ts", lambda raw: NOW)  # окно всегда попадает
    # 3 запроса (< порога 5): интернет-минута не считается, но категория — да
    monkeypatch.setattr(
        quota.adguard, "get_query_log",
        lambda limit=500: _qlog("192.168.88.30", ["roblox.com", "a.com", "b.com"], recent),
    )
    active = quota.record_activity(db, now=NOW)
    assert active == {kid_device.id: {"games"}}

    # 6 запросов через 2 минуты: и интернет, и категория
    later = NOW.replace(minute=2)
    monkeypatch.setattr(quota, "_parse_ts", lambda raw: later)  # записи снова «свежие»
    monkeypatch.setattr(
        quota.adguard, "get_query_log",
        lambda limit=500: _qlog("192.168.88.30",
                                ["roblox.com", "a.com", "b.com", "c.com", "d.com", "e.com"],
                                recent),
    )
    active = quota.record_activity(db, now=later)
    assert active == {kid_device.id: {"games", "internet"}}

    usage = {(u.category): u.minutes for u in db.query(QuotaUsage)}
    assert usage == {"games": 2, "internet": 1}


def test_record_activity_tick_guard(db, kid_device, monkeypatch):
    monkeypatch.setattr(quota.adguard, "get_query_log", lambda limit=500: [])
    assert quota.record_activity(db, now=NOW) == {}
    # повторный вызов через 5 секунд — пропускается (защита от двойного счёта)
    monkeypatch.setattr(
        quota.adguard, "get_query_log",
        lambda limit=500: (_ for _ in ()).throw(AssertionError("не должен вызываться")),
    )
    assert quota.record_activity(db, now=NOW.replace(second=5)) == {}


def _use(db, device_id, category, minutes, date="2026-07-14"):
    db.add(QuotaUsage(device_id=device_id, date=date, category=category, minutes=minutes))
    db.commit()


def test_internet_quota_blocks_in_desired_state(db, kid_device):
    db.add(Quota(target_type="group", target="kid", category="internet", minutes_per_day=60))
    db.commit()
    _use(db, kid_device.id, "internet", 59)
    st = _desired_state(db, [kid_device])
    assert kid_device.ip not in st["lists"]["hs-blocked"]

    _use(db, kid_device.id, "internet", 1)
    st = _desired_state(db, [kid_device])
    assert kid_device.ip in st["lists"]["hs-blocked"]


def test_category_quota_blocks_services(db, kid_device):
    db.add(Quota(target_type="group", target="kid", category="games", minutes_per_day=120))
    db.commit()
    _use(db, kid_device.id, "games", 120)
    st = _desired_state(db, [kid_device])
    assert kid_device.ip not in st["lists"]["hs-blocked"]  # интернет живёт
    client = st["ag_clients"]["hs-aa0000000001"]
    assert "steam" in client["blocked_services"] and "roblox" in client["blocked_services"]


def test_bonus_extends_limit(db, kid_device):
    db.add(Quota(target_type="group", target="kid", category="games", minutes_per_day=60))
    db.commit()
    _use(db, kid_device.id, "games", 80)
    assert quota.exhausted(db, [kid_device]) == {kid_device.id: {"games"}}

    db.add(QuotaBonus(target_type="device", target=str(kid_device.id),
                      date=datetime.now().strftime("%Y-%m-%d"), category="games", minutes=30))
    db.commit()
    assert quota.exhausted(db, [kid_device]) == {}  # 80 < 60+30


def test_quota_days_and_disabled(db, kid_device):
    db.add(Quota(target_type="group", target="kid", category="internet",
                 minutes_per_day=10, days="5,6"))  # только выходные
    db.commit()
    _use(db, kid_device.id, "internet", 999)
    assert quota.exhausted(db, [kid_device], now=NOW) == {}  # вторник — квоты нет

    weekend = datetime(2026, 7, 18, 15, 0)  # суббота
    _use(db, kid_device.id, "internet", 999, date="2026-07-18")
    assert quota.exhausted(db, [kid_device], now=weekend) == {kid_device.id: {"internet"}}


def test_device_quota_overrides_group(db, kid_device):
    db.add(Quota(target_type="group", target="kid", category="games", minutes_per_day=120))
    db.add(Quota(target_type="device", target=str(kid_device.id),
                 category="games", minutes_per_day=30))
    db.commit()
    _use(db, kid_device.id, "games", 40)  # больше личной (30), меньше групповой (120)
    assert quota.exhausted(db, [kid_device]) == {kid_device.id: {"games"}}


# ---------- инструменты ----------

@pytest.fixture
def no_reconcile(monkeypatch):
    monkeypatch.setattr(tools.enforcement, "reconcile", lambda s: None)


def test_set_quota_tool(db, kid_device, no_reconcile):
    msg = tools.run_tool(db, "set_quota",
                         {"target": "kid", "category": "games", "minutes_per_day": 90})
    assert "90" in msg
    q = db.query(Quota).one()
    assert q.target == "kid" and q.minutes_per_day == 90

    tools.run_tool(db, "set_quota",
                   {"target": "kid", "category": "games", "minutes_per_day": 45})
    assert db.query(Quota).one().minutes_per_day == 45  # обновление, не дубль

    tools.run_tool(db, "set_quota",
                   {"target": "kid", "category": "games", "minutes_per_day": 0})
    assert db.query(Quota).count() == 0  # 0 = удалить
    with pytest.raises(tools.ToolError):
        tools.run_tool(db, "set_quota",
                       {"target": "kid", "category": "чепуха", "minutes_per_day": 60})


def test_bonus_tool_requires_quota(db, kid_device, no_reconcile):
    with pytest.raises(tools.ToolError, match="нет активной квоты"):
        tools.run_tool(db, "add_bonus_time",
                       {"target": "Планшет", "minutes": 30, "category": "games"})

    tools.run_tool(db, "set_quota",
                   {"target": "kid", "category": "games", "minutes_per_day": 60})
    msg = tools.run_tool(db, "add_bonus_time",
                         {"target": "Планшет", "minutes": 30, "category": "games",
                          "comment": "за уборку"})
    assert "+30" in msg and "за уборку" in msg
    assert db.query(QuotaBonus).one().target == str(kid_device.id)


def test_quota_status_tool(db, kid_device, no_reconcile):
    tools.run_tool(db, "set_quota",
                   {"target": "kid", "category": "video", "minutes_per_day": 60})
    _use(db, kid_device.id, "video", 60, date=datetime.now().strftime("%Y-%m-%d"))
    rows = tools.run_tool(db, "get_quota_status", {})
    assert rows == [{
        "device_id": kid_device.id, "device": "Планшет",
        "category": "video", "category_label": "YouTube и видео",
        "used_minutes": 60, "limit_minutes": 60, "exhausted": True,
    }]
