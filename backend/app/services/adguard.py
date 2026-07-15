"""Клиент REST API AdGuard Home. Панель создаёт в AdGuard «клиентов» с
персональными настройками (блокировка сервисов, безопасный поиск) по IP
устройства. Управляем только клиентами с именами hs-* — ручные не трогаем."""

import logging

import httpx

from ..config import settings

log = logging.getLogger("homesec.adguard")

# Категории, доступные в UI панели -> id сервисов AdGuard Home.
# id сверяются с реестром AdGuard (HostlistsRegistry/assets/services.json):
# один неизвестный id валит ВЕСЬ clients/add|update с 400 «unknown
# blocked-service» (инцидент 2026-07-15: несуществующий "ea"). Дополнительная
# защита от расхождения версий — runtime-фильтр в sync_clients.
SERVICE_CATEGORIES = {
    "games": {
        "label": "Игры",
        "services": ["steam", "epic_games", "roblox", "minecraft", "battle_net",
                     "electronic_arts", "origin", "playstation", "xboxlive",
                     "riot_games", "wargaming"],
    },
    "video": {
        "label": "YouTube и видео",
        "services": ["youtube", "netflix", "twitch", "vimeo", "hulu"],
    },
    "social": {
        "label": "Соцсети и мессенджеры",
        "services": ["tiktok", "instagram", "facebook", "snapchat", "discord",
                     "telegram", "whatsapp", "reddit", "9gag", "vk"],
    },
}


class AdGuardError(Exception):
    pass


def _client() -> httpx.Client:
    return httpx.Client(
        base_url=settings.adguard_url,
        auth=(settings.adguard_username, settings.adguard_password),
        timeout=5,
    )


def _request(method: str, url: str, **kw):
    try:
        with _client() as c:
            r = c.request(method, url, **kw)
            r.raise_for_status()
            if r.headers.get("content-type", "").startswith("application/json"):
                return r.json()
            return None
    except httpx.HTTPStatusError as e:
        # В теле 400 AdGuard пишет причину («client already exists» и т.п.) —
        # без неё в журнале бессмысленный «400 Bad Request».
        detail = (e.response.text or "").strip()[:200]
        suffix = f" — {detail}" if detail else ""
        raise AdGuardError(f"AdGuard API {method} {url}: {e}{suffix}") from e
    except httpx.HTTPError as e:
        raise AdGuardError(f"AdGuard API {method} {url}: {e}") from e


def get_stats() -> dict:
    return _request("GET", "/control/stats") or {}


def get_query_log(limit: int = 50) -> list[dict]:
    data = _request("GET", f"/control/querylog?limit={limit}") or {}
    return data.get("data", [])


def _all_clients() -> list[dict]:
    data = _request("GET", "/control/clients") or {}
    return data.get("clients") or []


def list_clients() -> dict[str, dict]:
    """Наши (hs-*) клиенты AdGuard по имени."""
    return {c["name"]: c for c in _all_clients() if c["name"].startswith("hs-")}


def known_service_ids() -> set[str] | None:
    """id сервисов, которые знает ЭТОТ AdGuard (его встроенный реестр может
    отличаться от нашего списка по версии). None = узнать не удалось —
    тогда фильтрацию пропускаем, чтобы не отключить блокировки зря."""
    try:
        data = _request("GET", "/control/blocked_services/all") or {}
    except AdGuardError:
        return None
    services = data.get("blocked_services") or []
    return {s["id"] for s in services if s.get("id")}


def _foreign_ids(clients: list[dict]) -> set[str]:
    """Идентификаторы (IP/MAC/CIDR) клиентов, заведённых в AdGuard РУКАМИ.
    AdGuard требует уникальности id между всеми клиентами: попытка привязать
    hs-клиента к IP ручного клиента даёт 400 «another client uses the same IP»
    на каждом reconcile-тике."""
    return {
        i
        for c in clients
        if not c["name"].startswith("hs-")
        for i in (c.get("ids") or [])
    }


def _client_payload(name: str, ip: str, blocked_services: list[str], safe_search: bool) -> dict:
    return {
        "name": name,
        "ids": [ip],
        # per-client настройки работают только при use_global_settings=false
        "use_global_settings": False,
        "use_global_blocked_services": False,
        "blocked_services": sorted(blocked_services),
        "filtering_enabled": True,
        "safebrowsing_enabled": True,
        "parental_enabled": False,
        "ignore_querylog": False,
        "ignore_statistics": False,
        "safe_search": {
            "enabled": safe_search,
            "bing": True, "duckduckgo": True, "google": True,
            "pixabay": True, "yandex": True, "youtube": safe_search,
        },
        "safesearch_enabled": safe_search,  # совместимость со старыми версиями
        "tags": [],
        "upstreams": [],
    }


def sync_clients(desired: dict[str, dict]) -> None:
    """Приводит hs-клиентов AdGuard к желаемому виду.

    desired: {name: {"ip": ..., "blocked_services": [...], "safe_search": bool}}

    Ошибка по одному клиенту НЕ прерывает синхронизацию остальных: одна битая
    запись (дубль IP и т.п.) иначе блокировала бы весь AdGuard-слой на каждом
    reconcile-тике. Ошибки копятся и поднимаются одним AdGuardError в конце.

    IP, занятые РУЧНЫМИ клиентами AdGuard, пропускаются (см. _foreign_ids):
    такой клиент — осознанная настройка владельца, панель её не перебивает;
    per-client политика для устройства в этом случае не применится."""
    clients = _all_clients()
    current = {c["name"]: c for c in clients if c["name"].startswith("hs-")}
    foreign = _foreign_ids(clients)
    known = known_service_ids()
    errors: list[str] = []
    for name in set(current) - set(desired):
        try:
            _request("POST", "/control/clients/delete", json={"name": name})
        except AdGuardError as e:
            errors.append(str(e))
    for name, want in desired.items():
        if want["ip"] in foreign:
            log.warning("IP %s занят ручным клиентом AdGuard — %s пропущен "
                        "(политика панели для устройства не применится)", want["ip"], name)
            if name in current:  # не оставляем hs-клиента висеть на старом IP
                try:
                    _request("POST", "/control/clients/delete", json={"name": name})
                except AdGuardError as e:
                    errors.append(str(e))
            continue
        services = want["blocked_services"]
        if known is not None:
            unknown = [s for s in services if s not in known]
            if unknown:
                # Один неизвестный id валит весь запрос 400-кой — лучше молча
                # отфильтровать и заблокировать остальное, чем не применить ничего.
                log.warning("AdGuard не знает сервисы %s — пропущены для %s",
                            unknown, name)
                services = [s for s in services if s in known]
        payload = _client_payload(name, want["ip"], services, want["safe_search"])
        try:
            if name not in current:
                _request("POST", "/control/clients/add", json=payload)
                continue
            cur = current[name]
            cur_services = cur.get("blocked_services") or []
            if isinstance(cur_services, dict):  # новые версии: {"ids": [...], "schedule": ...}
                cur_services = cur_services.get("ids") or []
            legacy_safe = cur.get("safesearch_enabled", False)
            cur_safe = (cur.get("safe_search") or {}).get("enabled", legacy_safe)
            if (
                sorted(cur_services) != sorted(services)
                or cur.get("ids") != [want["ip"]]
                or bool(cur_safe) != want["safe_search"]
            ):
                _request("POST", "/control/clients/update", json={"name": name, "data": payload})
        except AdGuardError as e:
            errors.append(str(e))
    if errors:
        extra = f" (+ещё {len(errors) - 2})" if len(errors) > 2 else ""
        raise AdGuardError("; ".join(errors[:2]) + extra)
