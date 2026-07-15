"""Синхронизация клиентов AdGuard: изоляция ошибок по отдельным клиентам,
фильтрация неизвестных сервисов, ручные клиенты."""

import pytest

from app.services import adguard


def test_sync_clients_isolates_bad_client(monkeypatch):
    """Битый клиент (400: дубль IP) не блокирует синхронизацию остальных;
    ошибка всё равно поднимается в конце — reconcile её журналирует."""
    calls: list[str] = []

    def fake_request(method, url, **kw):
        if url == "/control/clients":
            return {"clients": []}
        if url == "/control/blocked_services/all":
            return {"blocked_services": []}
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
        if url == "/control/blocked_services/all":
            return {"blocked_services": [{"id": "steam"}]}
        calls.append((url, kw["json"]["name"]))
        return None

    monkeypatch.setattr(adguard, "_request", fake_request)
    desired = {"hs-old": {"ip": "192.168.88.77", "blocked_services": ["steam"],
                          "safe_search": False}}
    adguard.sync_clients(desired)  # без исключений
    assert calls == [("/control/clients/delete", "hs-old")]  # ни add, ни update


def test_sync_clients_filters_unknown_services(monkeypatch):
    """id, которого нет в реестре ЭТОГО AdGuard, отбрасывается с warning
    вместо 400 на весь запрос (инцидент 2026-07-15: unknown "ea")."""
    sent: dict[str, list[str]] = {}

    def fake_request(method, url, **kw):
        if url == "/control/clients":
            return {"clients": []}
        if url == "/control/blocked_services/all":
            return {"blocked_services": [{"id": "steam"}, {"id": "electronic_arts"}]}
        sent[kw["json"]["name"]] = kw["json"]["blocked_services"]
        return None

    monkeypatch.setattr(adguard, "_request", fake_request)
    adguard.sync_clients({"hs-kid": {"ip": "192.168.88.30",
                                     "blocked_services": ["steam", "ea", "kick"],
                                     "safe_search": False}})
    assert sent["hs-kid"] == ["steam"]


def test_sync_clients_without_registry_keeps_services(monkeypatch):
    """Если /blocked_services/all недоступен (старый AdGuard) — фильтрацию
    пропускаем, блокировки не режем."""
    sent: dict[str, list[str]] = {}

    def fake_request(method, url, **kw):
        if url == "/control/clients":
            return {"clients": []}
        if url == "/control/blocked_services/all":
            raise adguard.AdGuardError("404")
        sent[kw["json"]["name"]] = kw["json"]["blocked_services"]
        return None

    monkeypatch.setattr(adguard, "_request", fake_request)
    adguard.sync_clients({"hs-kid": {"ip": "192.168.88.30",
                                     "blocked_services": ["steam"],
                                     "safe_search": False}})
    assert sent["hs-kid"] == ["steam"]
