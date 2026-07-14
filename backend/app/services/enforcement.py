"""Ядро HomeSec: считает желаемое состояние сети из базы (устройства, группы,
правила-расписания, политики) и приводит к нему MikroTik и AdGuard Home.

Вызывается планировщиком раз в минуту и сразу после любого изменения в панели.
Идемпотентно: применяются только отличия."""

import logging
import socket
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import GROUP_ADDRESS_LISTS, Device, GroupPolicy, Rule, active_pauses, log_event
from . import adguard, mikrotik, quota
from .adguard import SERVICE_CATEGORIES

log = logging.getLogger("homesec.enforce")

# Известные публичные DoH-серверы: HTTPS к ним дропается для управляемых
# устройств (обычный DNS на порт 53 к этим же адресам перехватывает NAT).
DOH_SERVER_IPS = {
    "8.8.8.8", "8.8.4.4",                    # dns.google
    "1.1.1.1", "1.0.0.1",                    # cloudflare-dns.com
    "104.16.248.249", "104.16.249.249",      # cloudflare-dns.com (CDN)
    "9.9.9.9", "149.112.112.112",            # dns.quad9.net
    "208.67.222.222", "208.67.220.220",      # doh.opendns.com
    "94.140.14.14", "94.140.15.15",          # dns.adguard-dns.com
    "76.76.2.0", "76.76.10.0",               # freedns.controld.com
    "185.222.222.222", "45.11.45.11",        # dns.sb
}


def get_self_ips() -> set[str]:
    """IP самой малинки — их НЕЛЬЗЯ добавлять в списки контроля, иначе AdGuard
    (на этом же хосте) окажется «управляемым» и его upstream-трафик срежется
    правилами блокировки. Определяем основной LAN-адрес по маршруту."""
    ips: set[str] = set()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 53))  # пакет не шлётся, только выбирается маршрут
        ips.add(s.getsockname()[0])
    except OSError:
        pass
    finally:
        s.close()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(str(info[4][0]))
    except OSError:
        pass
    ips.discard("127.0.0.1")
    return ips


def rule_is_active(rule: Rule, now: datetime | None = None) -> bool:
    if not rule.enabled:
        return False
    now = now or datetime.now()
    try:
        days = {int(d) for d in rule.days.split(",") if d.strip() != ""}
        start = datetime.strptime(rule.start_time, "%H:%M").time()
        end = datetime.strptime(rule.end_time, "%H:%M").time()
    except ValueError:
        return False
    t = now.time()
    if start <= end:
        return now.weekday() in days and start <= t < end
    # Окно через полночь: до полуночи — день начала, после — предыдущий день
    if t >= start:
        return now.weekday() in days
    if t < end:
        return (now - timedelta(days=1)).weekday() in days
    return False


def discover_devices(db: Session, api) -> list[Device]:
    """Синхронизирует DHCP-lease'ы с базой: новые MAC → новые устройства."""
    known = {d.mac.upper(): d for d in db.scalars(select(Device))}
    for lease in mikrotik.get_leases(api):
        mac = lease["mac"].upper()
        if not mac:
            continue
        dev = known.get(mac)
        if dev is None:
            dev = Device(mac=mac, ip=lease["ip"], name=lease["hostname"] or mac)
            db.add(dev)
            db.commit()
            known[mac] = dev
            log_event(db, "device_new", f"Новое устройство: {dev.name} ({mac}, {lease['ip']})")
        elif lease["ip"] and dev.ip != lease["ip"]:
            dev.ip = lease["ip"]
            db.commit()
    return list(known.values())


def _desired_state(db: Session, devices: list[Device]) -> dict:
    now = datetime.now()
    rules = list(db.scalars(select(Rule)))
    policies = {p.group: p for p in db.scalars(select(GroupPolicy))}

    active_group_blocks = set()
    active_device_blocks = set()
    for r in rules:
        if rule_is_active(r, now):
            if r.target_type == "group":
                active_group_blocks.add(r.target)
            else:
                active_device_blocks.add(str(r.target))
    for p in active_pauses(db, now):  # разовые «паузы до …» поверх расписаний
        if p.target_type == "group":
            active_group_blocks.add(p.target)
        else:
            active_device_blocks.add(str(p.target))

    lists: dict[str, set[str]] = {name: set() for name in GROUP_ADDRESS_LISTS.values()}
    lists["hs-blocked"] = set()
    lists["hs-managed"] = set()
    lists["hs-doh"] = set(DOH_SERVER_IPS)
    queues: dict[str, str] = {}
    ag_clients: dict[str, dict] = {}

    quota_spent = quota.exhausted(db, devices, now)  # {device_id: категории}
    self_ips = get_self_ips()
    for dev in devices:
        if not dev.ip or dev.ip in self_ips:
            continue  # пропускаем саму малинку — она инфраструктура, не клиент
        group = dev.group
        spent = quota_spent.get(dev.id, set())
        if group in GROUP_ADDRESS_LISTS:
            lists[GROUP_ADDRESS_LISTS[group]].add(dev.ip)
        blocked = (
            dev.blocked_manual
            or group in active_group_blocks
            or str(dev.id) in active_device_blocks
            or (group == "unknown" and settings.block_unknown)
            or "internet" in spent  # квота на интернет исчерпана — до полуночи
        )
        if blocked:
            lists["hs-blocked"].add(dev.ip)
        # Управляемые: все, кроме взрослых без ограничений (им — fasttrack)
        if group != "adult" or dev.speed_limit or blocked:
            lists["hs-managed"].add(dev.ip)
        if dev.speed_limit:
            queues[dev.ip] = dev.speed_limit

        policy = policies.get(group)
        services: list[str] = []
        safe_search = bool(policy.safe_search) if policy else False
        if policy:
            for cat in policy.blocked_services.split(","):
                if cat.strip() in SERVICE_CATEGORIES:
                    services += SERVICE_CATEGORIES[cat.strip()]["services"]
        for cat in sorted(spent - {"internet"}):  # исчерпанные категории-квоты
            if cat in SERVICE_CATEGORIES:
                services += SERVICE_CATEGORIES[cat]["services"]
        if services or safe_search:
            ag_clients[f"hs-{dev.mac.lower().replace(':', '')}"] = {
                "ip": dev.ip,
                "blocked_services": sorted(set(services)),
                "safe_search": safe_search,
            }

    return {"lists": lists, "queues": queues, "ag_clients": ag_clients}


def reconcile(db: Session) -> dict:
    """Полный цикл: обнаружить устройства, применить состояние. Возвращает
    сводку; ошибки интеграций пишет в журнал, но не роняет планировщик."""
    summary: dict = {"ok": True, "errors": [], "newly_blocked": []}

    try:
        with mikrotik.api_session() as api:
            devices = discover_devices(db, api)
            desired = _desired_state(db, devices)
            for list_name, ips in desired["lists"].items():
                added, _removed = mikrotik.address_list_sync(api, list_name, ips)
                if list_name == "hs-blocked":
                    for ip in added:
                        mikrotik.kill_connections(api, ip)
                        summary["newly_blocked"].append(ip)
            mikrotik.queues_sync(api, desired["queues"])
    except mikrotik.MikrotikError as e:
        summary["ok"] = False
        summary["errors"].append(str(e))
        log.warning("%s", e)
        log_event(db, "error", str(e))
        return summary  # без роутера состояние AdGuard считать рано

    try:
        adguard.sync_clients(desired["ag_clients"])
    except adguard.AdGuardError as e:
        summary["ok"] = False
        summary["errors"].append(str(e))
        log.warning("%s", e)
        log_event(db, "error", str(e))

    for ip in summary["newly_blocked"]:
        log_event(db, "block", f"Заблокирован доступ: {ip}")
    return summary
