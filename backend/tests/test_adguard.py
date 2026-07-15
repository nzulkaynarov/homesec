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
