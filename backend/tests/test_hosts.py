"""The PC and the Pi: samples kept a week, the owner told once per problem, one button."""

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.bot import access, common
from app.config import settings
from app.db import Base, engine, session
from app.main import app
from app.models import HostSample, KVState
from app.services import hosts

NOW = datetime(2026, 11, 20, 12, 0)
AUTH = {"Authorization": "Bearer test-token"}
BOOT = {"id": "boot-b", "started": "2026-11-19T18:46:34", "previous_clean": True}


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr(settings, "luna_api_token", "test-token")
    Base.metadata.create_all(engine)
    s = session()
    for model in (HostSample, KVState):
        s.query(model).delete()
    s.commit()
    yield s
    s.close()


def _pc(db, minutes_ago, boot=None, **data):
    sample = {"boot": boot or BOOT, "cpu_max_c": 60, "mem_avail_mb": 20000, **data}
    hosts.record(db, hosts.PC, sample, NOW - timedelta(minutes=minutes_ago))


def _apply(db, now=NOW):
    found = hosts.changes(db, now)
    for change in found:
        hosts.remember(db, change)
    return [c.text for c in found if c.text]


def test_report_needs_the_token_and_is_stored(db):
    client = TestClient(app)
    body = {"boot": {"id": "x"}, "cpu_max_c": 55.5, "backup_age_h": {"luna": 8.2, "homesec": None}}
    assert client.post("/api/luna/v1/host", json=body).status_code == 401
    assert client.post("/api/luna/v1/host", json=body, headers=AUTH).status_code == 200
    _, data = hosts.latest(db, hosts.PC)
    assert data["cpu_max_c"] == 55.5
    assert data["backup_age_h"] == {"luna": 8.2, "homesec": None}


def test_samples_older_than_a_week_are_dropped(db):
    _pc(db, minutes_ago=8 * 24 * 60)
    _pc(db, minutes_ago=0)
    assert len(hosts.samples(db, hosts.PC)) == 1


def test_a_hot_cpu_is_told_after_five_minutes_and_once(db):
    for minute in range(4, 0, -1):
        _pc(db, minute, cpu_max_c=93)
    assert _apply(db) == []
    _pc(db, 0, cpu_max_c=94)
    told = _apply(db)
    assert len(told) == 1 and "94 °C" in told[0]
    _pc(db, 0, cpu_max_c=95)
    assert _apply(db) == []


def test_recovery_is_told_after_two_clean_minutes(db):
    for minute in range(5, 0, -1):
        _pc(db, minute + 2, cpu_max_c=95)
    _apply(db, NOW - timedelta(minutes=2))
    _pc(db, 1)
    assert _apply(db) == []
    _pc(db, 0)
    assert _apply(db) == ["✅ Снова в норме: температура процессора ПК."]


def test_a_stopped_service_and_an_old_backup(db):
    for minute in (2, 1, 0):
        _pc(db, minute, services_down=["lunagpt-gateway"], backup_age_h={"luna": 40, "homesec": 5})
    told = _apply(db)
    assert any("шлюз моделей" in t for t in told)
    assert any("Luna не обновлялась 40 ч" in t for t in told)
    assert not any("HomeSec" in t for t in told)


def test_a_silent_pc_is_told_once_with_its_last_readings(db):
    _pc(db, 6, cpu_max_c=71, top_process="node 99.0%")
    told = _apply(db)
    assert len(told) == 1
    assert "не отвечает с 11:54" in told[0] and "71 °C" in told[0] and "node" in told[0]
    assert _apply(db) == []
    _pc(db, 0)
    assert _apply(db) == ["✅ ПК снова на связи (молчал 6 мин)."]


def test_a_clean_shutdown_is_not_an_alarm(db):
    hosts.record(db, hosts.PC, {"boot": BOOT, "stopping": True}, NOW - timedelta(minutes=5))
    assert _apply(db) == ["🖥 ПК выключен штатно в 11:55."]


def test_a_sudden_power_off_names_the_last_readings(db):
    old = {"id": "boot-a"}
    _pc(db, 30, boot=old, cpu_max_c=73)
    _apply(db, NOW - timedelta(minutes=30))
    new = {
        "id": "boot-b",
        "started": "2026-11-20T11:58:00",
        "previous_clean": False,
        "previous_last": "2026-11-20T11:30:05",
    }
    _pc(db, 0, boot=new)
    told = _apply(db)
    sudden = next(t for t in told if t.startswith("⚡"))
    assert "около 11:30" in sudden and "11:58" in sudden and "73 °C" in sudden
    assert "скорее питание" in sudden
    _pc(db, 0, boot=new)
    assert not any(t.startswith("⚡") for t in _apply(db))


def test_the_first_boot_seen_is_silent(db):
    _pc(db, 0, boot={"id": "boot-a", "previous_clean": False})
    assert _apply(db) == []


def test_new_kernel_errors_are_told_once(db):
    _pc(db, 1, kernel_errors=0)
    assert _apply(db, NOW - timedelta(minutes=1)) == []
    _pc(db, 0, kernel_errors=2, kernel_error_last="nvme nvme0: I/O 12 QID 3 timeout")
    told = _apply(db)
    assert len(told) == 1 and "ошибки: 2" in told[0] and "timeout" in told[0]
    _pc(db, 0, kernel_errors=2)
    assert _apply(db) == []


def test_an_oom_kill_is_told_as_memory_not_hardware(db):
    _pc(db, 1, kernel_errors=1, oom_kills=0)
    _apply(db, NOW - timedelta(minutes=1))
    # the monitor stopped counting OOM as hardware: the kernel counter drops to 0, silently
    _pc(db, 0, kernel_errors=0, oom_kills=1, oom_last="pytest 4.9 GB")
    told = _apply(db)
    assert len(told) == 1 and "кончилась память" in told[0] and "pytest 4.9 GB" in told[0]
    assert "аппаратные" not in told[0]
    # a real hardware error after that is still told
    _pc(db, 0, kernel_errors=1, oom_kills=1, kernel_error_last="mce: [Hardware Error]")
    told = _apply(db)
    assert len(told) == 1 and "аппаратные ошибки: 1" in told[0]
    assert "нехватки памяти: ⚠️ 1" in hosts.status_text(db, NOW)


def test_low_memory_is_told_after_two_minutes(db):
    _pc(db, 1, mem_avail_mb=1500, top_process="pytest 99%")
    assert _apply(db, NOW - timedelta(minutes=1)) == []
    _pc(db, 0, mem_avail_mb=1100, top_process="pytest 99%")
    told = _apply(db)
    assert len(told) == 1 and "почти кончилась память" in told[0]


def test_pi_power_dip_and_full_card(db):
    for minute in (1, 0):
        sample = {"cpu_c": 60, "undervoltage_since_boot": True, "disks": {"/": 9.5}}
        hosts.record(db, hosts.PI, sample, NOW - timedelta(minutes=minute))
    told = _apply(db)
    assert any("просадка питания" in t for t in told)
    assert any("Raspberry Pi мало места: / свободно 10%" in t for t in told)


def test_status_shows_both_machines(db):
    _pc(db, 30, cpu_max_c=80, gpu_c=50)
    _pc(
        db,
        0,
        cpu_max_c=65,
        cpu_busy_pct=99,
        load1=19.27,
        mem_avail_mb=21000,
        swap_used_mb=1400,
        nvme_c=41,
        disks={"/": 44, "/data": 87},
        services_down=[],
        top_process="node 111%",
        backup_age_h={"luna": 8, "homesec": 9},
        kernel_errors=0,
        tailscale_days=165,
    )
    hosts.record(
        db,
        hosts.PI,
        {
            "cpu_c": 62,
            "undervoltage_since_boot": False,
            "disks": {"/": 44},
            "mem_avail_mb": 306,
            "uptime_s": 172000,
        },
        NOW,
    )
    text = hosts.status_text(db, NOW)
    for part in (
        "ПК — на связи",
        "65 °C (за сутки макс. 80)",
        "очередь 19,3",
        "спит (за сутки макс. 50)",
        "/data 87%",
        "все работают",
        "Luna 8 ч назад",
        "ещё 165 дн.",
        "ошибки ядра: нет",
        "Raspberry Pi — работает 1 дн. 23 ч",
        "Питание: в норме",
    ):
        assert part in text, part


def test_status_of_a_silent_pc(db):
    _pc(db, 20)
    assert "🔴 молчит с 11:40 (20 мин)" in hosts.status_text(db, NOW)


def test_only_the_owner_gets_the_button():
    def labels(role):
        return {b.text for row in common.keyboard_for_role(role).keyboard for b in row}

    assert common.BTN_SERVER in labels("owner")
    assert common.BTN_SERVER not in labels("admin")
    assert not access.check_message("kid", common.BTN_SERVER).allowed
