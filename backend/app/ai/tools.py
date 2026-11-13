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
    kv_set,
    log_event,
)
from ..services import adguard, enforcement, house_rules, mikrotik
from ..services import family as family_svc
from ..services import quota as quota_svc
from ..services.device_links import move_device_rows


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
            if source == "bot":
                # Бот не обращается к RouterOS напрямую: ставит флаг быстрой сверки
                # в kv_state, планировщик панели подхватывает его за секунды.
                kv_set(db, "reconcile_asap", "1")
            else:
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


def find_device_candidates(db: Session, query: str = "") -> list[Device]:
    """Кандидаты для выбора устройства кнопками: пустой запрос — все
    устройства, иначе частичные совпадения по имени (без регистра).
    Дополняет find_device: тот при 2+ совпадениях возвращает None, и бот
    вместо ложного «не нашёл» показывает инлайн-кнопки с кандидатами."""
    q = query.strip().lower()
    devices = sorted(db.scalars(select(Device)), key=lambda d: d.name.lower())
    if not q:
        return devices
    return [d for d in devices if q in d.name.lower()]


def _device_or_error(db: Session, device_id: int) -> Device:
    dev = db.get(Device, device_id)
    if dev is None:
        raise ToolError(f"Устройство id={device_id} не найдено")
    return dev


def apply_merge(db: Session, dup: Device, target: Device) -> None:
    """Moves a duplicate's addresses and linked rows to the target and removes it.
    The caller commits, so several merges can share one transaction."""
    for dm in db.scalars(select(DeviceMac).where(DeviceMac.device_id == dup.id)):
        dm.device_id = target.id
    if not db.scalar(select(DeviceMac).where(DeviceMac.mac == dup.mac)):
        db.add(DeviceMac(device_id=target.id, mac=dup.mac))
    if dup.ip:  # дубль — более свежий lease, актуальный адрес у него
        target.ip = dup.ip
    # Учёт минут, бонусы, заявки и паузы дубля переезжают вместе с MAC —
    # иначе телефон, сменивший «приватный» MAC днём, после объединения терял
    # накопленное за день экранное время, а висящая заявка ребёнка указывала
    # на удалённую запись (кнопка родителя падала с «Не нашёл устройство»).
    move_device_rows(db, dup.id, target.id)
    db.delete(dup)
    db.flush()  # sessions run without autoflush; the next merge must see these rows


def _guard_self(dev: Device) -> None:
    if dev.kind == "infra":
        raise ToolError(f"{dev.name} — это инфраструктура сети. Её блокировать нельзя.")
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
    owner = d.person.name if d.person else None
    if d.kind == "shared":
        owner = "📺 Общие (ТВ/Колонки)"
    elif d.kind == "iot":
        owner = "🤖 Умный дом (IoT)"
    elif d.kind == "infra":
        owner = "🛠 Инфраструктура"
    return {
        "id": d.id,
        "name": d.name,
        "mac": d.mac,
        "ip": d.ip,
        "kind": d.kind,
        "group": d.group,
        "group_label": GROUP_LABELS[d.group],
        "owner": owner,
        "blocked_manual": d.blocked_manual,
        "speed_limit": d.speed_limit or None,
        "paused_until": until,
    }




# ---------- инструменты: чтение ----------

@tool()
def list_devices(db: Session) -> list[dict]:
    """Список всех известных устройств домашней сети: имя, MAC, IP, группа
    (kid/adult/guest/unknown), владелец, онлайн (true/false; null — роутер
    недоступен, статус неизвестен), ручная блокировка, лимит скорости,
    активная пауза."""
    online: set[str] = set()
    router_ok = True
    try:
        with mikrotik.api_session() as api:  # одна сессия на весь список
            online = mikrotik.get_online_ips(api)
    except mikrotik.MikrotikError:
        router_ok = False  # пометка в ответе: online=null у всех устройств
    pauses = active_pauses(db)
    rows = [_device_row(d, pauses) for d in db.scalars(select(Device))]
    for r in rows:
        r["online"] = (bool(r["ip"]) and r["ip"] in online) if router_ok else None
    return rows


@tool()
def get_status(db: Session) -> dict:
    """Сводка состояния системы: доступность роутера и AdGuard, счётчики
    устройств (всего/онлайн/заблокировано), активные паузы, статистика DNS,
    прогресс квот экранного времени по устройствам (screen_time)."""
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
    prog = quota_svc.progress(db, devices)  # экранное время: только у кого есть квоты
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
        "screen_time": [
            {"device": dev.name, "category": p["category"], "label": p["label"],
             "used_minutes": p["used"], "limit_minutes": p["limit"]}
            for dev in devices for p in prog.get(dev.id, [])
        ],
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
    if limit and not mikrotik.valid_speed_limit(limit):
        raise ToolError(f"Неверный формат лимита «{limit}», ожидается вида 5M/20M")
    dev.speed_limit = limit
    db.commit()
    if limit:
        return f"Лимит скорости {limit} для {dev.name} ({dev.ip or dev.mac})"
    return f"Лимит скорости снят: {dev.name} ({dev.ip or dev.mac})"


def next_clock(clock: str, now: datetime) -> datetime:
    """The next moment the clock shows HH:MM. The model asked for a pause "until 9:00"
    at 00:44 once and computed 600 minutes, so the code does the arithmetic."""
    try:
        moment = datetime.strptime(clock.strip(), "%H:%M")
    except ValueError as exc:
        raise ToolError(f"Время «{clock}» не похоже на ЧЧ:ММ") from exc
    target = now.replace(hour=moment.hour, minute=moment.minute, second=0, microsecond=0)
    return target if target > now else target + timedelta(days=1)


def minutes_until(clock: str, now: datetime) -> int:
    return max(1, round((next_clock(clock, now) - now).total_seconds() / 60))


@tool(mutating=True)
def pause_internet(
    db: Session,
    target: Annotated[str, "группа (kid/guest/unknown) или устройство: id, имя или MAC"],
    minutes: Annotated[int, "длительность паузы в минутах (1–1440); 0, если задано until"] = 0,
    reason: Annotated[str, "короткая причина, попадает в журнал"] = "",
    until: Annotated[
        str, "до какого времени ЧЧ:ММ, например 09:00; ближайшее такое время, минуты не считай"
    ] = "",
) -> str:
    """Временно выключает интернет группе или устройству («пауза до …»).
    Если человек назвал время окончания, передай until, а не minutes.
    Снимается автоматически по истечении времени или через resume_internet."""
    now = datetime.now()
    if until.strip():
        until_at = next_clock(until, now)
        minutes = minutes_until(until, now)
    else:
        minutes = int(minutes)
        until_at = now + timedelta(minutes=minutes)
    if not 1 <= minutes <= 1440:
        raise ToolError("Длительность паузы — от 1 до 1440 минут")
    t = target.strip()
    if t in GROUPS:
        if t == "adult":
            raise ToolError("Группу взрослых ставить на паузу нельзя")
        db.add(Pause(target_type="group", target=t, until=until_at, reason=reason))
        db.commit()
        label = GROUP_LABELS[t]
    else:
        dev = find_device(db, t)
        if dev is None:
            raise ToolError(f"Не нашёл устройство «{target}» — уточните имя или id")
        _guard_self(dev)
        db.add(Pause(target_type="device", target=str(dev.id), until=until_at, reason=reason))
        db.commit()
        label = f"{dev.name} ({dev.ip or dev.mac})"
    return f"Пауза для {label} до {until_at:%H:%M} ({minutes} мин)"


@tool(mutating=True)
def set_bedtime(
    db: Session,
    who: Annotated[str, "имя ребёнка или kid — все дети"],
    nights: Annotated[str, "school — ночи вс–чт (перед будним днём), weekend — пт–сб, all — все"],
    start: Annotated[str, "когда выключать, ЧЧ:ММ, например 21:30"],
    end: Annotated[str, "когда включать утром, ЧЧ:ММ, например 07:00"],
) -> str:
    """Постоянный отбой: каждую ночь интернет детей выключается в start и
    включается в end. Используй, когда просят выключать интернет регулярно
    («каждую ночь», «по будням»); разовую паузу ставит pause_internet."""
    choice = {"school": ["school"], "weekend": ["weekend"], "all": ["school", "weekend"]}
    if nights not in choice:
        raise ToolError("nights: school, weekend или all")
    key = who.strip()
    people = list(db.scalars(select(Person).order_by(Person.name)))
    if key.lower() in ("kid", "kids", "дети"):
        targets = [p for p in people if p.role == "kid"]
    else:
        targets = [p for p in people if p.name.lower() == key.lower()]
    if not targets:
        raise ToolError(f"Не нашёл ребёнка «{who}». Есть: " + ", ".join(p.name for p in people))
    try:
        for person in targets:
            for night in choice[nights]:
                house_rules.set_bedtime(db, person.id, night, start, end)
    except house_rules.RuleError as exc:
        raise ToolError(str(exc)) from exc
    names = ", ".join(p.name for p in targets)
    return f"Отбой {start}–{end} ({nights}) для: {names}"


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
    person = db.scalar(select(Person).where(Person.name.ilike(t)))
    if person is not None:
        role_label = GROUP_LABELS.get(person.role, person.role)
        return "person", str(person.id), f"{person.name} ({role_label})"
    dev = find_device(db, t)
    if dev is not None:
        return "device", str(dev.id), f"{dev.name} ({dev.ip or dev.mac})"
    raise ToolError(f"Не нашёл устройство или человека «{target}» — уточните имя или id")


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
    dev_person_id = ""
    if target_type == "device":
        dev = _device_or_error(db, int(target_key))
        dev_group = dev.group
        dev_person_id = str(dev.person_id) if dev.person_id else ""
    elif target_type == "person":
        person = db.scalar(select(Person).where(Person.id == int(target_key)))
        dev_group = person.role if person else ""
        dev_person_id = target_key
    quotas = [q for q in db.scalars(select(Quota)) if q.enabled and q.category == category]
    applies = any(
        (q.target_type == target_type and q.target == target_key)
        or (target_type == "device" and q.target_type == "group" and q.target == dev_group)
        or (
            target_type == "device"
            and dev_person_id
            and q.target_type == "person"
            and q.target == dev_person_id
        )
        or (target_type == "person" and q.target_type == "group" and q.target == dev_group)
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
    dup_label = f"{dup.name} ({dup.mac})"
    apply_merge(db, dup, target)
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


@tool(mutating=True)
def set_device_kind(
    db: Session,
    device_id: Annotated[int, "id устройства из list_devices"],
    kind: Annotated[
        str, "personal (личное) | shared (ТВ/колонка) | iot (умный дом) | infra (роутер, сервер)"
    ],
) -> str:
    """Устанавливает категорию устройства: личное, общее, умный дом или инфраструктура."""
    dev = _device_or_error(db, device_id)
    k = kind.strip().lower()
    if k not in ("personal", "shared", "iot", "infra"):
        raise ToolError("Категория должна быть personal, shared, iot или infra")
    dev.kind = k
    if k == "infra":
        dev.person = None  # equipment belongs to the home, so no person's limits can reach it
    db.commit()
    labels = {
        "personal": "Личное",
        "shared": "Общее (ТВ/Колонка)",
        "iot": "Умный дом (IoT)",
        "infra": "Инфраструктура",
    }
    return f"{dev.name}: установлена категория «{labels[k]}»"


@tool()
def get_presence(db: Session) -> dict:
    """Радар «Кто дома»: кто из людей сейчас в сети Wi-Fi (по смартфонам),
    статус общих устройств (ТВ, приставки) и техники умного дома."""
    return family_svc.get_presence_status(db)


@tool(mutating=True)
def set_movie_mode(
    db: Session,
    hours: Annotated[int, "на сколько часов включить режим взрослых (по умолчанию 8, до утра)"] = 8,
) -> str:
    """Режим «Смотрят взрослые»: временно разблокирует общие ТВ и приставки до утра."""
    return family_svc.set_movie_mode(db, hours)


@tool(mutating=True)
def save_family_note(
    db: Session,
    content: Annotated[str, "текст семейной заметки (размер одежды, код домофона, список и т.п.)"],
    author: Annotated[str, "автор заметки"] = "",
) -> str:
    """Сохраняет запись в общую семейную память."""
    note = family_svc.save_family_note(db, author=author, content=content)
    return f"Заметка сохранена: «{note.content}»"


@tool()
def get_family_notes(
    db: Session,
    limit: Annotated[int, "максимальное число заметок"] = 10,
) -> list[dict]:
    """Возвращает список последних семейных заметок."""
    notes = family_svc.get_family_notes(db, limit=limit)
    return [
        {
            "id": n.id,
            "author": n.author,
            "content": n.content,
            "created": n.created.strftime("%d.%m %H:%M"),
        }
        for n in notes
    ]
