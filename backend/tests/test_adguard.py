"""Синхронизация клиентов AdGuard: изоляция ошибок по отдельным клиентам."""

import pytest

from app.services import adguard


def test_sync_clients_isolates_bad_client(monkeypatch):
    """Битый клиент (400: дубль IP) не блокирует синхронизацию остальных;
    ошибка всё равно поднимается в конце — reconcile её журналирует."""
    calls: list[str] = []

    def fake_request(method, url, **kw):
        if url == "/control/clients":
            return {"clients": []}
        name = kw["json"]["name"]
        calls.append(name)
        if name == "hs-bad":
            raise adguard.AdGuardError("AdGuard API POST /control/clients/add: 400 — dup ip")
        return None

    monkeypatch.setattr(adguard, "_request", fake_request)
    desired = {
        "hs-bad": {"ip": "192.168.88.248", "blocked_services": [], "safe_search": True},
        "hs-good": {"ip": "192.168.88.10", "blocked_services": ["steam"], "safe_search": False},
    }
    with pytest.raises(adguard.AdGuardError, match="dup ip"):
        adguard.sync_clients(desired)
    assert calls == ["hs-bad", "hs-good"]  # хороший клиент создан несмотря на ошибку


def test_sync_clients_skips_ip_owned_by_manual_client(monkeypatch):
    """IP ручного клиента AdGuard не перебивается: hs-клиента к нему не
    привязываем (иначе вечный 400 «another client uses the same IP»), а его
    существующая hs-запись со старым IP удаляется, чтобы политика не висела
    на чужом адресе."""
    calls: list[tuple[str, str]] = []

    def fake_request(method, url, **kw):
        if url == "/control/clients":
            return {"clients": [
                {"name": "Мой ноут", "ids": ["192.168.88.77"]},
                {"name": "hs-old", "ids": ["192.168.88.5"]},
            ]}
        calls.append((url, kw["json"]["name"]))
        return None

    monkeypatch.setattr(adguard, "_request", fake_request)
    desired = {"hs-old": {"ip": "192.168.88.77", "blocked_services": ["steam"],
                          "safe_search": False}}
    adguard.sync_clients(desired)  # без исключений
    assert calls == [("/control/clients/delete", "hs-old")]  # ни add, ни update
