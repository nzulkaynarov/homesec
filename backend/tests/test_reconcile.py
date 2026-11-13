"""Аварийные режимы reconcile() — те самые предохранители, что появились после
реальных инцидентов, но не были покрыты ни одним тестом: раньше сьют оставался
зелёным, даже если fail-closed сломать.

Проверяем три свойства:
* пустой набор собственных IP → роутер не трогаем вовсе (иначе малинка может
  попасть в списки блокировки, и весь дом останется без DNS);
* роутер недоступен → до AdGuard дело не доходит (состояние считать рано);
* повторные ошибки не заливают журнал (троттлинг).
"""

from datetime import datetime, timedelta

import pytest

from app.db import Base, engine, session
from app.models import Device, EventLog, KVState, Person
from app.services import enforcement


@pytest.fixture
def db():
    Base.metadata.create_all(engine)
    s = session()
    for model in (EventLog, KVState, Device, Person):
        s.query(model).delete()
    s.commit()
    enforcement._last_error_log.clear()
    yield s
    enforcement._last_error_log.clear()
    s.close()


def _explode(*a, **kw):
    raise AssertionError("роутер не должен трогаться в этом режиме")


def test_empty_self_ips_is_fail_closed(db, monkeypatch):
    monkeypatch.setattr(enforcement, "get_self_ips", set)
    monkeypatch.setattr(enforcement.mikrotik, "api_session", _explode)
    monkeypatch.setattr(enforcement.adguard, "sync_clients", _explode)

    summary = enforcement.reconcile(db)

    assert summary["ok"] is False and summary["errors"]
    assert "HS_SELF_IPS" in summary["errors"][0]
    errors = [e for e in db.query(EventLog) if e.kind == "error"]
    assert len(errors) == 1


def test_router_down_stops_before_adguard(db, monkeypatch):
    monkeypatch.setattr(enforcement, "get_self_ips", lambda: {"192.168.88.2"})

    def dead_router():
        raise enforcement.mikrotik.MikrotikError("роутер недоступен: timeout")

    monkeypatch.setattr(enforcement.mikrotik, "api_session", dead_router)
    monkeypatch.setattr(enforcement.adguard, "sync_clients", _explode)

    summary = enforcement.reconcile(db)

    assert summary["ok"] is False
    assert "timeout" in summary["errors"][0]
    assert summary["newly_blocked"] == []
    assert [e.kind for e in db.query(EventLog)] == ["error"]


def test_repeated_errors_are_throttled(db, monkeypatch):
    """Роутер лежит час — в журнале не должно быть 60 одинаковых записей:
    иначе полезные события тонут, а страница журнала бесполезна."""
    monkeypatch.setattr(enforcement, "get_self_ips", set)
    for _ in range(3):
        enforcement.reconcile(db)
    assert len([e for e in db.query(EventLog) if e.kind == "error"]) == 1

    # прошло больше окна тишины — сообщаем снова
    enforcement._last_error_log["self_ip"] = (
        datetime.now() - enforcement._ERROR_LOG_COOLDOWN - timedelta(seconds=1))
    enforcement.reconcile(db)
    assert len([e for e in db.query(EventLog) if e.kind == "error"]) == 2


def test_self_ips_always_have_static_anchor(monkeypatch):
    """Автоопределение может отдать пусто (нет маршрута, hostname без A-записи),
    и тогда единственная защита малинки — статический якорь из конфига."""
    monkeypatch.setattr(enforcement.settings, "self_ips", "192.168.88.2")
    assert "192.168.88.2" in enforcement.get_self_ips()


class _FakeSession:
    def __enter__(self):
        return object()

    def __exit__(self, *exc):
        return False


def _router_up(monkeypatch, *, queues_error=None, desired_error=None):
    """A reachable router whose stages can be made to fail one by one."""
    monkeypatch.setattr(enforcement, "get_self_ips", lambda: {"192.168.88.2"})
    monkeypatch.setattr(enforcement.mikrotik, "api_session", _FakeSession)
    for name in ("get_answering_macs", "get_live_arp", "get_online_ips"):
        monkeypatch.setattr(enforcement.mikrotik, name, lambda api: {})
    monkeypatch.setattr(enforcement.mikrotik, "prune_pinned_leases", lambda api, macs: [])
    monkeypatch.setattr(enforcement.mikrotik, "address_list_sync", lambda api, name, ips: ([], []))
    monkeypatch.setattr(enforcement, "discover_devices", lambda db, api, macs: [])
    monkeypatch.setattr(enforcement, "follow_arp", lambda db, devices, arp: devices)
    monkeypatch.setattr(enforcement, "mark_seen", lambda db, devices, online: None)

    def desired(db, devices):
        if desired_error:
            raise desired_error
        return {"lists": {"hs-blocked": set()}, "queues": {}, "ag_clients": [],
                "quota_blocked": {}}

    monkeypatch.setattr(enforcement, "_desired_state", desired)

    def queues(api, wanted):
        if queues_error:
            raise queues_error

    monkeypatch.setattr(enforcement.mikrotik, "queues_sync", queues)
    synced = []
    monkeypatch.setattr(enforcement.adguard, "sync_clients", synced.append)
    return synced


def test_success_is_remembered_and_fresh(db, monkeypatch):
    _router_up(monkeypatch)
    assert enforcement.reconcile(db)["ok"] is True
    fresh, _error = enforcement.enforcement_status(db)
    assert fresh is True


def test_status_goes_stale_without_a_recent_success(db):
    enforcement.kv_set(db, enforcement.LAST_OK_KEY, "2026-11-13T10:00:00")
    later = datetime(2026, 11, 13, 10, 6)
    assert enforcement.enforcement_status(db, later)[0] is False
    assert enforcement.enforcement_status(db, datetime(2026, 11, 13, 10, 4))[0] is True


def test_never_succeeded_is_stale(db):
    assert enforcement.enforcement_status(db)[0] is False


def test_router_failure_is_recorded_for_the_alert(db, monkeypatch):
    monkeypatch.setattr(enforcement, "get_self_ips", lambda: {"192.168.88.2"})

    def dead_router():
        raise enforcement.mikrotik.MikrotikError("неверный пароль API")

    monkeypatch.setattr(enforcement.mikrotik, "api_session", dead_router)
    enforcement.reconcile(db)
    fresh, error = enforcement.enforcement_status(db)
    assert fresh is False and "неверный пароль" in error


def test_queue_failure_does_not_stop_adguard(db, monkeypatch):
    synced = _router_up(monkeypatch, queues_error=ValueError("could not convert 'abc'"))
    summary = enforcement.reconcile(db)
    assert synced == [[]]  # AdGuard still received its clients
    assert summary["ok"] is False and "очереди скорости" in summary["errors"][0]


def test_unexpected_error_is_reported_not_raised(db, monkeypatch):
    _router_up(monkeypatch, desired_error=KeyError("group"))
    summary = enforcement.reconcile(db)
    assert summary["ok"] is False and "KeyError" in summary["errors"][0]
    assert enforcement.enforcement_status(db)[1]


@pytest.mark.parametrize(
    ("limit", "valid"),
    [("5M/20M", True), ("512K/1.5M", True), ("10/10", True), ("abcM/1M", False), ("5M", False),
     ("", False)],
)
def test_speed_limit_validation(limit, valid):
    assert enforcement.mikrotik.valid_speed_limit(limit) is valid


def test_alert_says_rules_stopped_and_why(monkeypatch):
    from app.bot import health

    monitor = health.HealthMonitor(
        checks={"Правила дома": lambda: False},
        fail_after=1,
        failure_texts={"Правила дома": "ограничения не применяются"},
        details={"Правила дома": lambda: "последняя ошибка: неверный пароль API"},
    )
    [message] = monitor.tick()
    assert "ограничения не применяются" in message and "неверный пароль" in message


def test_heartbeat_is_not_sent_while_rules_are_stale(db, monkeypatch):
    from app import scheduler

    calls = []
    monkeypatch.setattr(scheduler.httpx, "get", lambda *a, **kw: calls.append(a))
    scheduler._heartbeat()
    assert calls == []
