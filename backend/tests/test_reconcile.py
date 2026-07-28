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
