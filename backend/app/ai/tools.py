"""Реестр инструментов — единственная точка, через которую ИИ-агенты и
Telegram-бот читают и меняют состояние системы. Из сигнатур и докстрингов
генерируются tool-схемы для Claude API (`anthropic_schemas()`).

Правила безопасности зашиты ЗДЕСЬ, в коде, а не в промптах:
- мутирующие инструменты помечены mutating=True — оркестратор исполняет их
  только после подтверждения человеком кнопкой в Telegram;
- инструментов для firewall/NAT/mangle/DNS-конфига в реестре НЕТ и быть не
  должно: ИИ управляет только теми же address-list'ами/очередями/паузами,
  что и панель (см. CLAUDE.md);
- малинку (self IP) заблокировать нельзя;
- каждое исполнение мутирующего инструмента пишется в EventLog.
"""

import inspect
import json
import re
import typing
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated, Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    GROUP_LABELS,
    GROUPS,
    Device,
    DeviceMac,
    EventLog,
    Pause,
    PendingAction,
    Person,
    Quota,
    QuotaBonus,
    active_pauses,
    log_event,
)
from ..services import adguard, enforcement, mikrotik
from ..services import quota as quota_svc


class ToolError(Exception):
    """Ошибка уровня инструмента: текст безопасен для показа модели и человеку."""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict
    func: Callable
    mutating: bool


REGISTRY: dict[str, ToolSpec] = {}

_JSON_TYPES = {int: "integer", str: "string", bool: "boolean", float: "number"}


def tool(mutating: bool = False):
    """Регистрирует функцию как инструмент. Первый параметр `db` — служебный
    (в схему не попадает); описания аргументов берутся из Annotated-метаданных."""

    def deco(fn: Callable) -> Callable:
        hints = typing.get_type_hints(fn, include_extras=True)
        props: dict[str, dict] = {}
        required: list[str] = []
        for pname, param in inspect.signature(fn).parameters.items():
            if pname == "db":
                continue
            ann = hints.get(pname, str)
            desc = ""
            if typing.get_origin(ann) is Annotated:
                ann, *meta = typing.get_args(ann)
                desc = next((m for m in meta if isinstance(m, str)), "")
            schema: dict[str, Any] = {"type": _JSON_TYPES.get(ann, "string")}
            if desc:
                schema["description"] = desc
            props[pname] = schema
            if param.default is inspect.Parameter.empty:
                required.append(pname)
        REGISTRY[fn.__name__] = ToolSpec(
            name=fn.__name__,
            description=inspect.getdoc(fn) or "",
            input_schema={"type": "object", "properties": props, "required": required},
            func=fn,
            mutating=mutating,
        )
        return fn

    return deco


def anthropic_schemas() -> list[dict]:
    """Схемы всех инструментов в формате Claude API (tools=[...])."""
    return [
        {"name": t.name, "description": t.description, "input_schema": t.input_schema}
        for t in REGISTRY.values()
    ]


def is_mutating(name: str) -> bool:
    spec = REGISTRY.get(name)
    return spec is not None and spec.mutating


def run_tool(db: Session, name: str, args: dict, source: str = "ai",
             reconcile_after: bool = True) -> Any:
    """Исполняет инструмент. Мутации логируются в EventLog (kind=<source>_action)
    и сразу применяются к сети (reconcile). Подтверждение человеком — забота
    ВЫЗЫВАЮЩЕГО: сюда мутирующий вызов должен приходить уже одобренным."""
    spec = REGISTRY.get(name)
    if spec is None:
        raise ToolError(f"Неизвестный инструмент: {name}")
    try:
        result = spec.func(db, **args)
    except TypeError as e:
        raise ToolError(f"Неверные аргументы {name}: {e}") from e
    if spec.mutating:
        log_event(db, f"{source}_action", str(result))
        if reconcile_after:
            enforcement.reconcile(db)
    return result


# ---------- помощники (не инструменты) ----------

def save_pending(db: Session, tool_name: str, args: dict, description: str) -> int:
    """Сохраняет мутацию, ожидающую кнопку подтверждения в Telegram.
    В базе, а не в памяти бота: кнопки переживают деплой (CD рестартит
    бота на каждый push в main)."""
    row = PendingAction(tool=tool_name, args=json.dumps(args, ensure_ascii=False),
                        description=description)
    db.add(row)
    db.commit()
    return row.id


def pop_pending(db: Session, pending_id: int) -> tuple[str, dict, str] | None:
    """Забирает отложенную мутацию — одноразово: повторное нажатие кнопки
    получит None. Возвращает (tool, args, description)."""
    row = db.get(PendingAction, pending_id)
    if row is None:
        return None
    out = (row.tool, json.loads(row.args or "{}"), row.description)
    db.delete(row)
    db.commit()
    return out


def find_device(db: Session, query: str) -> Device | None:
    """Ищет устройство по id, MAC, точному или частичному имени (без регистра).
    Используется и ботом (/block <имя>), и инструментами с target-строкой."""
    q = query.strip()
    if not q:
        return None
    if q.isdigit():
        dev = db.get(Device, int(q))
        if dev:
            return dev
    devices = list(db.scalars(select(Device)))
    for d in devices:
        if d.mac.upper() == q.upper() or d.ip == q:
            return d
    exact = [d for d in devices if d.name.lower() == q.lower()]
    if len(exact) == 1:
        return exact[0]
    partial = [d for d in devices if q.lower() in d.name.lower()]
    return partial[0] if len(partial) == 1 else None


def _device_or_error(db: Session, device_id: int) -> Device:
    dev = db.get(Device, device_id)
    if dev is None:
        raise ToolError(f"Устройство id={device_id} не найдено")
    return dev


def _guard_self(dev: Device) -> None:
    if dev.ip and dev.ip in enforcement.get_self_ips():
        raise ToolError(
            f"{dev.name} ({dev.ip}) — это сама малинка (DNS и панель). "
            "Инфраструктуру блокировать нельзя."
        )


def _device_row(d: Device, pauses: list[Pause]) -> dict:
    paused = [p for p in pauses
              if (p.target_type == "device" and p.target == str(d.id))
              or (p.target_type == "group" and p.target == d.group)]
    until = max(p.until for p in paused).isoformat(timespec="minutes") if paused else None
    return {
        "id": d.id,
        "name": d.name,
        "mac": d.mac,
        "ip": d.ip,
        "group": d.group,
        "group_label": GROUP_LABELS[d.group],
        "owner": d.person.name if d.person else None,
        "blocked_manual": d.blocked_manual,
        "speed_limit": d.speed_limit or None,
        "paused_until": until,
    }


_LIMIT_RE = re.compile(r"^\d+(\.\d+)?[KMG]?/\d+(\.\d+)?[KMG]?$", re.IGNORECASE)


# ---------- инструменты: чтение ----------

@tool()
def list_devices(db: Session) -> list[dict]:
    """Список всех известных устройств домашней сети: имя, MAC, IP, группа
    (kid/adult/guest/unknown), владелец, ручная блокировка, лимит скорости,
    активная пауза."""
    pauses = active_pauses(db)
    return [_device_row(d, pauses) for d in db.scalars(select(Device))]


@tool()
def get_status(db: Session) -> dict:
    """Сводка состояния системы: доступность роутера и AdGuard, счётчики
    устройств (всего/онлайн/заблокировано), активные паузы, статистика DNS."""
    online: set[str] = set()
    router_ok = False
    try:
        with mikrotik.api_session() as api:
            online = mikrotik.get_online_ips(api)
            router_ok = True
    except mikrotik.MikrotikError:
        pass
    stats: dict = {}
    adguard_ok = False
    try:
        stats = adguard.get_stats()
        adguard_ok = True
    except adguard.AdGuardError:
        pass
    devices = list(db.scalars(select(Device)))
    pauses = active_pauses(db)
    return {
        "router_ok": router_ok,
        "adguard_ok": adguard_ok,
        "devices_total": len(devices),
        "devices_online": sum(1 for d in devices if d.ip and d.ip in online),
        "devices_blocked": sum(1 for d in devices if d.blocked_manual),
        "devices_unknown": sum(1 for d in devices if d.group == "unknown"),
        "active_pauses": [
            {"target_type": p.target_type, "target": p.target,
             "until": p.until.isoformat(timespec="minutes"), "reason": p.reason}
            for p in pauses
        ],
        "dns_queries_today": stats.get("num_dns_queries"),
        "dns_blocked_today": stats.get("num_blocked_filtering"),
    }


@tool()
def get_recent_events(
    db: Session,
    limit: Annotated[int, "сколько последних событий вернуть (1–200)"] = 20,
) -> list[dict]:
    """Последние события журнала: новые устройства, блокировки, ошибки,
    действия панели/бота/ИИ."""
    limit = max(1, min(int(limit), 200))
    events = db.scalars(select(EventLog).order_by(EventLog.ts.desc()).limit(limit))
    return [
        {"ts": e.ts.isoformat(timespec="minutes"), "kind": e.kind, "message": e.message}
        for e in events
    ]


@tool()
def get_device_activity(
    db: Session,
    device_id: Annotated[int, "id устройства из list_devices"],
    limit: Annotated[int, "сколько последних DNS-запросов вернуть (1–100)"] = 30,
) -> list[dict]:
    """Последние DNS-запросы устройства из журнала AdGuard: какие домены
    посещались и что было заблокировано."""
    dev = _device_or_error(db, device_id)
    if not dev.ip:
        raise ToolError(f"{dev.name}: у устройства нет IP (офлайн)")
    limit = max(1, min(int(limit), 100))
    try:
        entries = adguard.get_query_log(limit=500)
    except adguard.AdGuardError as e:
        raise ToolError(str(e)) from e
    out = []
    for entry in entries:
        if entry.get("client") != dev.ip:
            continue
        reason = (entry.get("reason") or "").lower()
        out.append({
            "time": entry.get("time", ""),
            "domain": (entry.get("question") or {}).get("name", ""),
            "blocked": reason.startswith("filtered"),
        })
        if len(out) >= limit:
            break
    return out


# ---------- инструменты: мутации (только после подтверждения человеком) ----------

@tool(mutating=True)
def block_device(
    db: Session,
    device_id: Annotated[int, "id устройства из list_devices"],
) -> str:
    """Полностью блокирует устройству доступ в интернет (ручная блокировка,
    действует до снятия). Активные соединения рвутся сразу."""
    dev = _device_or_error(db, device_id)
    _guard_self(dev)
    dev.blocked_manual = True
    db.commit()
    return f"Заблокировано: {dev.name} ({dev.ip or dev.mac})"


@tool(mutating=True)
def unblock_device(
    db: Session,
    device_id: Annotated[int, "id устройства из list_devices"],
) -> str:
    """Снимает ручную блокировку устройства (расписания и паузы продолжают
    действовать независимо)."""
    dev = _device_or_error(db, device_id)
    dev.blocked_manual = False
    db.commit()
    return f"Разблокировано: {dev.name} ({dev.ip or dev.mac})"


@tool(mutating=True)
def set_speed_limit(
    db: Session,
    device_id: Annotated[int, "id устройства из list_devices"],
    limit: Annotated[str, "лимит upload/download, напр. '5M/20M'; пустая строка снимает лимит"],
) -> str:
    """Ставит или снимает лимит скорости для устройства."""
    dev = _device_or_error(db, device_id)
    limit = limit.strip()
    if limit and not _LIMIT_RE.match(limit):
        raise ToolError(f"Неверный формат лимита «{limit}», ожидается вида 5M/20M")
    dev.speed_limit = limit
    db.commit()
    if limit:
        return f"Лимит скорости {limit} для {dev.name} ({dev.ip or dev.mac})"
    return f"Лимит скорости снят: {dev.name} ({dev.ip or dev.mac})"


@tool(mutating=True)
def pause_internet(
    db: Session,
    target: Annotated[str, "группа (kid/guest/unknown) или устройство: id, имя или MAC"],
    minutes: Annotated[int, "длительность паузы в минутах (1–1440)"],
    reason: Annotated[str, "короткая причина, попадает в журнал"] = "",
) -> str:
    """Временно выключает интернет группе или устройству («пауза до …»).
    Снимается автоматически по истечении времени или через resume_internet."""
    minutes = int(minutes)
    if not 1 <= minutes <= 1440:
        raise ToolError("Длительность паузы — от 1 до 1440 минут")
    until = datetime.now() + timedelta(minutes=minutes)
    t = target.strip()
    if t in GROUPS:
        if t == "adult":
            raise ToolError("Группу взрослых ставить на паузу нельзя")
        db.add(Pause(target_type="group", target=t, until=until, reason=reason))
        db.commit()
        label = GROUP_LABELS[t]
    else:
        dev = find_device(db, t)
        if dev is None:
            raise ToolError(f"Не нашёл устройство «{target}» — уточните имя или id")
        _guard_self(dev)
        db.add(Pause(target_type="device", target=str(dev.id), until=until, reason=reason))
        db.commit()
        label = f"{dev.name} ({dev.ip or dev.mac})"
    return f"Пауза для {label} до {until:%H:%M} ({minutes} мин)"


@tool(mutating=True)
def resume_internet(
    db: Session,
    target: Annotated[str, "группа (kid/guest/unknown) или устройство: id, имя или MAC"],
) -> str:
    """Досрочно снимает активные паузы с группы или устройства."""
    t = target.strip()
    if t in GROUPS:
        key = ("group", t)
        label = GROUP_LABELS[t]
    else:
        dev = find_device(db, t)
        if dev is None:
            raise ToolError(f"Не нашёл устройство «{target}» — уточните имя или id")
        key = ("device", str(dev.id))
        label = f"{dev.name} ({dev.ip or dev.mac})"
    removed = 0
    for p in active_pauses(db):
        if (p.target_type, p.target) == key:
            db.delete(p)
            removed += 1
    db.commit()
    if not removed:
        raise ToolError(f"Активных пауз для «{label}» нет")
    return f"Пауза снята: {label}"


@tool()
def get_quota_status(db: Session) -> list[dict]:
    """Статус квот времени на сегодня: по каждому устройству с квотой —
    категория, сколько минут использовано и каков лимит (с учётом бонусов)."""
    devices = list(db.scalars(select(Device)))
    prog = quota_svc.progress(db, devices)
    out = []
    for dev in devices:
        for p in prog.get(dev.id, []):
            out.append({
                "device_id": dev.id,
                "device": dev.name,
                "category": p["category"],
                "category_label": p["label"],
                "used_minutes": p["used"],
                "limit_minutes": p["limit"],
                "exhausted": p["used"] >= p["limit"],
            })
    return out


def _parse_quota_target(db: Session, target: str) -> tuple[str, str, str]:
    """-> (target_type, target, человекочитаемое имя)."""
    t = target.strip()
    if t in GROUPS:
        return "group", t, GROUP_LABELS[t]
    dev = find_device(db, t)
    if dev is None:
        raise ToolError(f"Не нашёл устройство «{target}» — уточните имя или id")
    return "device", str(dev.id), f"{dev.name} ({dev.ip or dev.mac})"


@tool(mutating=True)
def set_quota(
    db: Session,
    target: Annotated[str, "группа (kid/guest/unknown/adult) или устройство: id, имя, MAC"],
    category: Annotated[str, "internet | games | video | social"],
    minutes_per_day: Annotated[int, "минут в день (1–1440); 0 удаляет квоту"],
    days: Annotated[str, "дни недели csv, 0=Пн (по умолчанию все)"] = "0,1,2,3,4,5,6",
) -> str:
    """Ставит, меняет или удаляет дневную квоту времени. Для группы квота
    действует на каждое устройство группы отдельно."""
    if category not in quota_svc.QUOTA_CATEGORIES:
        raise ToolError(f"Неизвестная категория «{category}», есть: "
                        + ", ".join(quota_svc.QUOTA_CATEGORIES))
    target_type, target_key, label = _parse_quota_target(db, target)
    minutes_per_day = int(minutes_per_day)
    existing = db.scalar(select(Quota).where(
        Quota.target_type == target_type, Quota.target == target_key,
        Quota.category == category))
    cat_label = quota_svc.QUOTA_CATEGORY_LABELS[category]
    if minutes_per_day <= 0:
        if existing is None:
            raise ToolError(f"Квоты «{cat_label}» для {label} нет — удалять нечего")
        db.delete(existing)
        db.commit()
        return f"Квота удалена: {cat_label} для {label}"
    if not 1 <= minutes_per_day <= 1440:
        raise ToolError("Квота — от 1 до 1440 минут в день")
    day_set = ",".join(sorted(set(days.split(",")) & {"0", "1", "2", "3", "4", "5", "6"}))
    if existing is None:
        db.add(Quota(name=f"{cat_label} — {label}", target_type=target_type,
                     target=target_key, category=category,
                     minutes_per_day=minutes_per_day, days=day_set or "0,1,2,3,4,5,6"))
    else:
        existing.minutes_per_day = minutes_per_day
        existing.days = day_set or existing.days
        existing.enabled = True
    db.commit()
    return f"Квота: {cat_label} для {label} — {minutes_per_day} мин/день"


@tool(mutating=True)
def add_bonus_time(
    db: Session,
    target: Annotated[str, "группа или устройство: id, имя, MAC"],
    minutes: Annotated[int, "сколько минут добавить сегодня (1–720)"],
    category: Annotated[str, "internet | games | video | social"] = "internet",
    comment: Annotated[str, "за что бонус, попадает в журнал"] = "",
) -> str:
    """Добавляет минуты к СЕГОДНЯШНЕЙ квоте («+30 минут игр за уборку»).
    Работает, только если подходящая квота существует."""
    minutes = int(minutes)
    if not 1 <= minutes <= 720:
        raise ToolError("Бонус — от 1 до 720 минут")
    if category not in quota_svc.QUOTA_CATEGORIES:
        raise ToolError(f"Неизвестная категория «{category}», есть: "
                        + ", ".join(quota_svc.QUOTA_CATEGORIES))
    target_type, target_key, label = _parse_quota_target(db, target)
    dev_group = ""
    if target_type == "device":
        dev = _device_or_error(db, int(target_key))
        dev_group = dev.group
    quotas = [q for q in db.scalars(select(Quota)) if q.enabled and q.category == category]
    applies = any(
        (q.target_type == target_type and q.target == target_key)
        or (target_type == "device" and q.target_type == "group" and q.target == dev_group)
        for q in quotas
    )
    if not applies:
        raise ToolError(
            f"У {label} нет активной квоты «{quota_svc.QUOTA_CATEGORY_LABELS[category]}» — "
            "бонус не к чему добавлять"
        )
    db.add(QuotaBonus(target_type=target_type, target=target_key,
                      date=datetime.now().strftime("%Y-%m-%d"),
                      category=category, minutes=minutes, comment=comment))
    db.commit()
    return (f"Бонус +{minutes} мин ({quota_svc.QUOTA_CATEGORY_LABELS[category]}) "
            f"для {label} на сегодня" + (f": {comment}" if comment else ""))


@tool(mutating=True)
def merge_devices(
    db: Session,
    duplicate_id: Annotated[int, "id устройства-дубля (обычно свежее, со случайным MAC)"],
    target_id: Annotated[int, "id настоящего устройства (сохраняет имя, владельца, правила)"],
) -> str:
    """Объединяет дубль (новый MAC того же физического устройства, обычно
    из-за «приватного адреса») с настоящим устройством: MAC-адреса дубля
    переходят к настоящему, дубль удаляется, настройки сохраняются."""
    if int(duplicate_id) == int(target_id):
        raise ToolError("Нельзя объединить устройство с самим собой")
    dup = _device_or_error(db, int(duplicate_id))
    target = _device_or_error(db, int(target_id))
    _guard_self(dup)
    for dm in db.scalars(select(DeviceMac).where(DeviceMac.device_id == dup.id)):
        dm.device_id = target.id
    if not db.scalar(select(DeviceMac).where(DeviceMac.mac == dup.mac)):
        db.add(DeviceMac(device_id=target.id, mac=dup.mac))
    if dup.ip:  # дубль — более свежий lease, актуальный адрес у него
        target.ip = dup.ip
    dup_label = f"{dup.name} ({dup.mac})"
    db.delete(dup)
    db.commit()
    return f"Объединено: {dup_label} → «{target.name}»; правила и владелец сохранены"


@tool(mutating=True)
def assign_device(
    db: Session,
    device_id: Annotated[int, "id устройства из list_devices"],
    person_name: Annotated[str, "имя владельца из списка людей; пустая строка — отвязать"],
) -> str:
    """Назначает устройству владельца (устройство наследует группу владельца)
    или отвязывает его (группа станет unknown)."""
    dev = _device_or_error(db, device_id)
    name = person_name.strip()
    if not name:
        dev.person = None  # правим связь, а не FK: сессия живёт с expire_on_commit=False
        db.commit()
        return f"{dev.name}: владелец снят, группа — Неизвестные"
    person = db.scalar(select(Person).where(Person.name == name))
    if person is None:
        known = ", ".join(p.name for p in db.scalars(select(Person))) or "список пуст"
        raise ToolError(f"Человек «{name}» не найден. Есть: {known}")
    dev.person = person
    db.commit()
    return f"{dev.name} → {person.name} ({GROUP_LABELS[person.role]})"
