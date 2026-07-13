"""Клиент REST API AdGuard Home. Панель создаёт в AdGuard «клиентов» с
персональными настройками (блокировка сервисов, безопасный поиск) по IP
устройства. Управляем только клиентами с именами hs-* — ручные не трогаем."""

import logging

import httpx

from ..config import settings

log = logging.getLogger("homesec.adguard")

# Категории, доступные в UI панели -> id сервисов AdGuard Home
SERVICE_CATEGORIES = {
    "games": {
        "label": "Игры",
        "services": ["steam", "epic_games", "roblox", "minecraft", "battle_net",
                     "ea", "playstation", "xboxlive", "riot_games", "wargaming"],
    },
    "video": {
        "label": "YouTube и видео",
        "services": ["youtube", "netflix", "twitch", "kick", "vimeo", "hulu"],
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
    except httpx.HTTPError as e:
        raise AdGuardError(f"AdGuard API {method} {url}: {e}") from e


def get_stats() -> dict:
    return _request("GET", "/control/stats") or {}


def get_query_log(limit: int = 50) -> list[dict]:
    data = _request("GET", f"/control/querylog?limit={limit}") or {}
    return data.get("data", [])


def list_clients() -> dict[str, dict]:
    """Наши (hs-*) клиенты AdGuard по имени."""
    data = _request("GET", "/control/clients") or {}
    return {c["name"]: c for c in (data.get("clients") or []) if c["name"].startswith("hs-")}


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
    """
    current = list_clients()
    for name in set(current) - set(desired):
        _request("POST", "/control/clients/delete", json={"name": name})
    for name, want in desired.items():
        payload = _client_payload(name, want["ip"], want["blocked_services"], want["safe_search"])
        if name not in current:
            _request("POST", "/control/clients/add", json=payload)
            continue
        cur = current[name]
        cur_services = cur.get("blocked_services") or []
        if isinstance(cur_services, dict):  # новые версии: {"ids": [...], "schedule": ...}
            cur_services = cur_services.get("ids") or []
        cur_safe = (cur.get("safe_search") or {}).get("enabled", cur.get("safesearch_enabled", False))
        if (
            sorted(cur_services) != sorted(want["blocked_services"])
            or cur.get("ids") != [want["ip"]]
            or bool(cur_safe) != want["safe_search"]
        ):
            _request("POST", "/control/clients/update", json={"name": name, "data": payload})
