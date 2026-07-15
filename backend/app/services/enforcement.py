"""Ядро HomeSec: считает желаемое состояние сети из базы (устройства, группы,
правила-расписания, политики) и приводит к нему MikroTik и AdGuard Home.

Вызывается планировщиком раз в минуту и сразу после любого изменения в панели.
Идемпотентно: применяются только отличия."""

import fcntl
import logging
import os
import socket
from contextlib import contextmanager
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import settings
from ..models import (
    GROUP_ADDRESS_LISTS,
    Device,
    DeviceMac,
    GroupPolicy,
    Rule,
    active_pauses,
    is_random_mac,
    log_event,
)
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
    правилами блокировки, и весь дом останется без DNS.

    Автоопределение (маршрут + hostname) — основной путь, но оно может отдать
    пустой набор (нет маршрута по умолчанию в момент старта, hostname без A-записи).
    Пустой набор — авария: тогда снимается ЕДИНСТВЕННАЯ защита малинки. Поэтому
    к автоопределению всегда добавляется статический якорь HS_SELF_IPS (по
    умолчанию LAN-адрес Pi) — он работает, даже если автоопределение отвалилось.
    Реальная защита от пустого набора — fail-closed в reconcile()."""
    ips: set[str] = set()
    for part in settings.self_ips.split(","):  # статический якорь из конфига
        part = part.strip()
        if part:
            ips.add(part)
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


# Один писатель на роутере за раз: reconcile зовут планировщик и веб-хендлеры
# панели (один процесс) И бот (отдельный процесс homesec-bot). Без межпроцессной
# блокировки два address_list_sync (read-modify-write) наложились бы и дали дубли
# в hs-списках. Файловый flock снимается ядром автоматически при смерти процесса.
_RECONCILE_LOCK_PATH = os.path.join(
    os.path.dirname(os.path.abspath(settings.database_path)) or ".",
    ".homesec-reconcile.lock",
)


@contextmanager
def _reconcile_lock():
    f = open(_RECONCILE_LOCK_PATH, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX)  # ждёт, пока освободит другой процесс
        yield
    finally:
        try:
            fcntl.flock(f, fcntl.LOCK_UN)
        finally:
            f.close()


# Троттлинг журнала ошибок: когда роутер/AdGuard недоступны, reconcile-тик раз в
# минуту иначе спамил бы событие error (→ пуш в бот) каждые 60 секунд. В journald
# (log.warning) пишем всегда, в БД/бот — не чаще, чем раз в _ERROR_LOG_COOLDOWN.
_last_error_log: dict[str, datetime] = {}
_ERROR_LOG_COOLDOWN = timedelta(minutes=30)


def _log_error_throttled(db: Session, key: str, msg: str) -> None:
    now = datetime.now()
    last = _last_error_log.get(key)
    if last is None or now - last >= _ERROR_LOG_COOLDOWN:
        _last_error_log[key] = now
        log_event(db, "error", msg)


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


def _release_stale_ip(db: Session, devices: list[Device], owner: Device, ip: str) -> None:
    """DHCP выдал ip устройству owner — забираем его у всех остальных.
    Протухший адрес (обычно после ротации «приватного» MAC: старая запись
    держит IP, который DHCP уже выдал новому MAC) иначе дублируется в базе,
    а AdGuard отвергает клиентов с одинаковым ids → 400 на clients/update
    каждый reconcile-тик. Ловили вживую (16:33–17:22, 2026-07-15)."""
    if not ip:
        return
    for other in devices:
        if other.id != owner.id and other.ip == ip:
            other.ip = ""
            db.commit()
            log_event(db, "ip_conflict",
                      f"IP {ip} переехал к «{owner.name}» ({owner.mac}); "
                      f"у «{other.name}» ({other.mac}) адрес сброшен.")


def discover_devices(db: Session, api) -> list[Device]:
    """Синхронизирует DHCP-lease'ы с базой: новые MAC → новые устройства.
    Матчит по ВСЕМ MAC устройства (device_macs) — телефон с рандомизацией,
    объединённый с исходным устройством, не плодит дублей. Новое устройство
    с hostname существующего — кандидат на объединение (device_maybe_same)."""
    devices = list(db.scalars(select(Device)))
    known: dict[str, Device] = {d.mac.upper(): d for d in devices}
    by_id = {d.id: d for d in devices}
    for dm in db.scalars(select(DeviceMac)):
        dev = by_id.get(dm.device_id)
        if dev is not None:
            known.setdefault(dm.mac.upper(), dev)

    for lease in mikrotik.get_leases(api):
        mac = lease["mac"].upper()
        if not mac:
            continue
        hostname = (lease["hostname"] or "").strip()
        dev = known.get(mac)
        if dev is None:
            dev = Device(mac=mac, ip=lease["ip"], name=hostname or mac, hostname=hostname)
            db.add(dev)
            try:
                db.commit()
            except IntegrityError:
                # Другой процесс (бот или планировщик) успел создать это же
                # устройство между нашим select и commit — UNIQUE по mac упал.
                # Берём существующее, дублей не плодим; ip/hostname подтянет
                # следующий тик через ветку обновления ниже.
                db.rollback()
                dev = db.scalar(select(Device).where(Device.mac == mac))
                if dev is not None:
                    known[mac] = dev
                    by_id[dev.id] = dev
                continue
            db.add(DeviceMac(device_id=dev.id, mac=mac))
            db.commit()
            known[mac] = dev
            by_id[dev.id] = dev
            note = " ⚠️ случайный MAC" if is_random_mac(mac) else ""
            log_event(db, "device_new",
                      f"Новое устройство: {dev.name} ({mac}, {lease['ip']}){note}")
            if hostname:
                twin = next(
                    (d for d in devices if d.hostname and d.hostname == hostname), None
                )
                if twin is not None:
                    log_event(db, "device_maybe_same",
                              f"Похоже, «{dev.name}» ({mac}) — это снова "
                              f"«{twin.name}» ({twin.mac}): совпадает имя хоста.")
            devices.append(dev)
        else:
            if lease["ip"] and dev.ip != lease["ip"]:
                dev.ip = lease["ip"]
                db.commit()
            if hostname and dev.hostname != hostname:
                dev.hostname = hostname
                db.commit()
        _release_stale_ip(db, devices, dev, lease["ip"])
    return devices


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
    quota_blocked: dict[str, Device] = {}  # ip -> устройство с исчерпанной интернет-квотой
    queues: dict[str, str] = {}
    ag_clients: dict[str, dict] = {}
    ag_client_ips: set[str] = set()

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
            if "internet" in spent:  # причина известна: квота — reconcile
                quota_blocked[dev.ip] = dev  # оформит отдельное событие для бота
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
            # Страховка от дублей IP (AdGuard отвергает клиентов с одинаковым
            # ids). Нормально конфликт разруливает _release_stale_ip; сюда
            # попадаем только в окно между тиками или при ручной правке базы.
            if dev.ip in ag_client_ips:
                log.warning("дубль IP %s: клиент AdGuard для «%s» (%s) пропущен",
                            dev.ip, dev.name, dev.mac)
            else:
                ag_client_ips.add(dev.ip)
                ag_clients[f"hs-{dev.mac.lower().replace(':', '')}"] = {
                    "ip": dev.ip,
                    "blocked_services": sorted(set(services)),
                    "safe_search": safe_search,
                }

    return {"lists": lists, "queues": queues, "ag_clients": ag_clients,
            "quota_blocked": quota_blocked}


def reconcile(db: Session) -> dict:
    """Полный цикл: обнаружить устройства, применить состояние. Возвращает
    сводку; ошибки интеграций пишет в журнал, но не роняет планировщик."""
    summary: dict = {"ok": True, "errors": [], "newly_blocked": []}
    quota_blocked: dict[str, Device] = {}

    # Fail-closed: если не знаем собственный IP, снимается единственная защита
    # малинки от попадания в списки блокировки — лучше НЕ трогать роутер вовсе,
    # чем рискнуть оставить весь дом без DNS. Штатно набор непустой (в нём
    # статический якорь HS_SELF_IPS), так что это страховка на крайний случай.
    if not get_self_ips():
        msg = ("не удалось определить собственный IP малинки — reconcile "
               "пропущен, состояние роутера не тронуто (проверьте HS_SELF_IPS)")
        log.error(msg)
        _log_error_throttled(db, "self_ip", msg)
        summary["ok"] = False
        summary["errors"].append(msg)
        return summary

    try:
        with _reconcile_lock(), mikrotik.api_session() as api:
            devices = discover_devices(db, api)
            desired = _desired_state(db, devices)
            quota_blocked = desired["quota_blocked"]
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
        _log_error_throttled(db, "mikrotik", str(e))
        return summary  # без роутера состояние AdGuard считать рано

    try:
        adguard.sync_clients(desired["ag_clients"])
    except adguard.AdGuardError as e:
        summary["ok"] = False
        summary["errors"].append(str(e))
        log.warning("%s", e)
        _log_error_throttled(db, "adguard", str(e))

    # События — только при ПЕРЕХОДЕ в заблокированное состояние (newly_blocked:
    # IP реально добавлен в hs-blocked), иначе бот спамил бы каждый тик.
    for ip in summary["newly_blocked"]:
        dev = quota_blocked.get(ip)
        if dev is not None:
            # MAC в тексте обязателен: notify.py восстанавливает устройство
            # регексом по MAC и вешает кнопку «+30 мин»
            log_event(db, "quota_block",
                      f"Квота на интернет исчерпана: {dev.name} ({dev.mac}, {ip}) — "
                      "доступ выключен до полуночи.")
        else:
            log_event(db, "block", f"Заблокирован доступ: {ip}")
    return summary
