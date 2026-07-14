"""Логика бота без Telegram: форматирование, антидребезг health-мониторинга,
курсор уведомлений о новых устройствах."""

import pytest

from app.bot import texts
from app.bot.health import HealthMonitor
from app.bot.notify import CURSOR_KEY, collect_notifications
from app.db import Base, engine, session
from app.models import Device, EventLog, KVState, kv_get, log_event


@pytest.fixture
def db():
    Base.metadata.create_all(engine)
    s = session()
    for model in (EventLog, Device, KVState):
        s.query(model).delete()
    s.commit()
    yield s
    s.close()


def test_format_status_and_devices():
    text = texts.format_status({
        "router_ok": True, "adguard_ok": False,
        "devices_total": 5, "devices_online": 3,
        "devices_blocked": 1, "devices_unknown": 2,
        "dns_queries_today": 1000, "dns_blocked_today": 50,
        "active_pauses": [{"target_type": "group", "target": "kid",
                           "until": "2026-07-14T21:30", "reason": ""}],
    })
    assert "✅ роутер" in text and "❌ AdGuard" in text
    assert "kid до 21:30" in text

    rows = [{"id": 1, "name": "Планшет", "group_label": "Дети", "owner": "Миша",
             "blocked_manual": True, "paused_until": None, "speed_limit": "5M/20M"}]
    text = texts.format_devices(rows)
    assert "Планшет" in text and "⛔" in text and "🐢 5M/20M" in text
    assert texts.format_devices([]) == "Устройств пока нет."


def test_health_debounce():
    up = {"ok": False}
    monitor = HealthMonitor(checks={"svc": lambda: up["ok"]}, fail_after=3, ok_after=2)
    assert monitor.tick() == [] and monitor.tick() == []  # 2 фейла — рано
    assert any("не отвечает" in m for m in monitor.tick())  # 3-й — алерт
    assert monitor.tick() == []  # алерт не повторяется
    up["ok"] = True
    assert monitor.tick() == []  # 1 успех — рано
    assert any("снова работает" in m for m in monitor.tick())
    assert monitor.tick() == []


def test_health_probe_exception_counts_as_fail():
    def boom():
        raise RuntimeError("нет сети")

    monitor = HealthMonitor(checks={"svc": boom}, fail_after=1)
    assert any("не отвечает" in m for m in monitor.tick())


def test_notification_cursor(db):
    dev = Device(mac="AA:00:00:00:00:07", ip="192.168.88.77", name="tv")
    db.add(dev)
    db.commit()
    log_event(db, "device_new", f"Новое устройство: tv ({dev.mac}, {dev.ip})")

    # первый запуск: история пропускается, курсор встаёт на текущий максимум
    assert collect_notifications(db) == []
    assert kv_get(db, CURSOR_KEY) != ""

    dev2 = Device(mac="AA:00:00:00:00:08", ip="192.168.88.78", name="phone")
    db.add(dev2)
    db.commit()
    log_event(db, "device_new", f"Новое устройство: phone ({dev2.mac}, {dev2.ip})")
    log_event(db, "register_request", f"Заявка: phone ({dev2.mac}) — владелец: Бабушка")
    log_event(db, "block", "не относится к делу")

    found = collect_notifications(db)
    assert [(n.kind, n.device.id) for n in found] == [
        ("device_new", dev2.id), ("register_request", dev2.id),
    ]
    assert "Бабушка" in found[1].message
    assert collect_notifications(db) == []  # повторно не отдаёт


def test_new_device_keyboard():
    from app.bot.handlers import new_device_keyboard

    kb = new_device_keyboard(5, [(1, "Миша", "kid"), (2, "Папа", "adult")])
    flat = [b for row in kb.inline_keyboard for b in row]
    data = {b.callback_data for b in flat}
    assert {"nd:5:assign:1", "nd:5:assign:2", "nd:5:block", "nd:5:skip"} <= data
    assert any("Дети" in b.text for b in flat)
