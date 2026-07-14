"""Обёртка над RouterOS API (librouteros). Все изменения на роутере — только
через этот модуль. Соединение открывается на один сеанс работы (reconcile-тик
или обработку запроса) и закрывается — так не копятся зависшие сессии."""

import logging
import socket
from contextlib import contextmanager

import librouteros
from librouteros.query import Key

from ..config import settings

log = logging.getLogger("homesec.mikrotik")

API_PORT = 8728


class MikrotikError(Exception):
    pass


def _reachable(host: str, port: int = API_PORT, timeout: float = 1.0) -> bool:
    """Быстрая TCP-проверка перед полным API-сеансом: когда роутер недоступен
    (ещё не в сети), страницы панели не должны висеть на длинном таймауте."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@contextmanager
def api_session():
    if not _reachable(settings.mikrotik_host):
        raise MikrotikError(f"RouterOS {settings.mikrotik_host}:{API_PORT} недоступен")
    api = None
    try:
        api = librouteros.connect(
            host=settings.mikrotik_host,
            username=settings.mikrotik_user,
            password=settings.mikrotik_password,
            timeout=10,
        )
        yield api
    except (OSError, librouteros.exceptions.LibRouterosError) as e:
        raise MikrotikError(f"RouterOS API: {e}") from e
    finally:
        if api is not None:
            try:
                api.close()
            except Exception:
                pass


def get_leases(api) -> list[dict]:
    """DHCP-lease'ы: mac, ip, hostname, динамический/статический, статус."""
    out = []
    for row in api.path("ip", "dhcp-server", "lease"):
        out.append(
            {
                "mac": row.get("mac-address", ""),
                "ip": row.get("address", ""),
                "hostname": row.get("host-name", ""),
                "dynamic": row.get("dynamic", True),
                "status": row.get("status", ""),
                "server": row.get("server", ""),
                "id": row.get(".id"),
            }
        )
    return out


def make_lease_static(api, mac: str, ip: str, comment: str = "") -> None:
    """Закрепляет IP за MAC: удаляет динамический lease и создаёт статический."""
    leases = api.path("ip", "dhcp-server", "lease")
    existing = [row for row in leases if row.get("mac-address", "").upper() == mac.upper()]
    if existing and not existing[0].get("dynamic", False):
        return  # уже статический
    server = existing[0].get("server", "defconf") if existing else "defconf"
    for row in existing:
        leases.remove(row[".id"])
    leases.add(**{
        "mac-address": mac,
        "address": ip,
        "server": server,
        "comment": comment or "hs",
    })


def address_list_get(api, list_name: str) -> dict[str, str]:
    """{ip: .id} для заданного address-list."""
    path = api.path("ip", "firewall", "address-list")
    lst, addr = Key("list"), Key("address")
    result = {}
    for row in path.select(Key(".id"), lst, addr).where(lst == list_name):
        result[row["address"]] = row[".id"]
    return result


def address_list_sync(api, list_name: str, desired: set[str]) -> tuple[set[str], set[str]]:
    """Приводит address-list к desired. Возвращает (added, removed)."""
    current = address_list_get(api, list_name)
    path = api.path("ip", "firewall", "address-list")
    to_add = desired - set(current)
    to_remove = set(current) - desired
    for ip in to_add:
        path.add(list=list_name, address=ip, comment="hs")
    for ip in to_remove:
        path.remove(current[ip])
    return to_add, to_remove


def kill_connections(api, ip: str) -> None:
    """Рвёт активные соединения устройства — блокировка срабатывает мгновенно,
    а не после таймаута установленных сессий."""
    path = api.path("ip", "firewall", "connection")
    src = Key("src-address")
    rows = path.select(Key(".id"), src)
    ids = [row[".id"] for row in rows if row.get("src-address", "").split(":")[0] == ip]
    for cid in ids:
        try:
            path.remove(cid)
        except librouteros.exceptions.LibRouterosError:
            pass  # соединение могло закрыться само


def queues_sync(api, desired: dict[str, str]) -> None:
    """Приводит simple queues к желаемому виду. desired: {ip: "10M/10M"}.
    Управляем только очередями с именем hs-dev-*, чужие не трогаем."""
    path = api.path("queue", "simple")
    current = {}
    for row in path:
        name = row.get("name", "")
        if name.startswith("hs-dev-"):
            current[name] = row
    desired_named = {f"hs-dev-{ip.replace('.', '-')}": (ip, limit) for ip, limit in desired.items()}

    for name, row in current.items():
        if name not in desired_named:
            path.remove(row[".id"])
        elif row.get("max-limit") != _normalize_limit(desired_named[name][1]):
            path.update(**{".id": row[".id"], "max-limit": desired_named[name][1]})
    for name, (ip, limit) in desired_named.items():
        if name not in current:
            path.add(name=name, target=f"{ip}/32", **{"max-limit": limit})


def _normalize_limit(limit: str) -> str:
    # RouterOS возвращает лимиты в битах: "10M/10M" -> "10000000/10000000"
    def part(p: str) -> str:
        p = p.strip().upper()
        mult = {"K": 1_000, "M": 1_000_000, "G": 1_000_000_000}
        if p and p[-1] in mult:
            return str(int(float(p[:-1]) * mult[p[-1]]))
        return p

    return "/".join(part(p) for p in limit.split("/"))


def get_online_ips(api) -> set[str]:
    """IP устройств, замеченных в ARP-таблице (грубый признак «онлайн»)."""
    ips = set()
    for row in api.path("ip", "arp"):
        if row.get("address") and not row.get("invalid", False):
            ips.add(row["address"])
    return ips
