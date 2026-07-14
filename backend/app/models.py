from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base

# Группы устройств. У устройства без владельца группа всегда 'unknown'.
GROUPS = ("kid", "adult", "guest", "unknown")
GROUP_LABELS = {"kid": "Дети", "adult": "Взрослые", "guest": "Гости", "unknown": "Неизвестные"}

# Соответствие групп address-list'ам на MikroTik
GROUP_ADDRESS_LISTS = {"kid": "hs-kids", "guest": "hs-guests", "unknown": "hs-unknown"}


class Person(Base):
    __tablename__ = "people"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    role: Mapped[str] = mapped_column(String(16), default="kid")  # kid | adult | guest

    devices: Mapped[list["Device"]] = relationship(back_populates="person")


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(primary_key=True)
    mac: Mapped[str] = mapped_column(String(17), unique=True)
    ip: Mapped[str] = mapped_column(String(15), default="")
    name: Mapped[str] = mapped_column(String(64), default="")
    person_id: Mapped[int | None] = mapped_column(ForeignKey("people.id"), nullable=True)
    blocked_manual: Mapped[bool] = mapped_column(Boolean, default=False)
    speed_limit: Mapped[str] = mapped_column(String(32), default="")  # напр. "10M/10M"
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    person: Mapped[Person | None] = relationship(back_populates="devices")

    @property
    def group(self) -> str:
        return self.person.role if self.person else "unknown"


class Rule(Base):
    """Расписание блокировки: в окне [start_time, end_time) по заданным дням
    цель (группа или устройство) лишается интернета. Окно через полночь
    (22:00–07:00) поддерживается."""

    __tablename__ = "rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    target_type: Mapped[str] = mapped_column(String(8), default="group")  # group | device
    target: Mapped[str] = mapped_column(String(32))  # имя группы или id устройства
    days: Mapped[str] = mapped_column(String(16), default="0,1,2,3,4,5,6")  # 0=Пн
    start_time: Mapped[str] = mapped_column(String(5), default="22:00")
    end_time: Mapped[str] = mapped_column(String(5), default="07:00")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class GroupPolicy(Base):
    """Политика группы в AdGuard Home: заблокированные сервисы и безопасный поиск."""

    __tablename__ = "group_policies"

    id: Mapped[int] = mapped_column(primary_key=True)
    group: Mapped[str] = mapped_column(String(16), unique=True)
    blocked_services: Mapped[str] = mapped_column(Text, default="")  # csv id сервисов AdGuard
    safe_search: Mapped[bool] = mapped_column(Boolean, default=False)


class Quota(Base):
    """Дневная квота времени: сколько минут в день группе или устройству
    разрешена категория (games/video/social) или интернет целиком. Для групп
    квота действует НА КАЖДОЕ устройство группы отдельно. По исчерпании —
    блокировка категории (AdGuard) или интернета (hs-blocked) до полуночи."""

    __tablename__ = "quotas"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), default="")
    target_type: Mapped[str] = mapped_column(String(8), default="group")  # group | device
    target: Mapped[str] = mapped_column(String(32))
    category: Mapped[str] = mapped_column(String(16))  # internet | games | video | social
    minutes_per_day: Mapped[int] = mapped_column(default=120)
    days: Mapped[str] = mapped_column(String(16), default="0,1,2,3,4,5,6")  # 0=Пн
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class QuotaUsage(Base):
    """Счётчик активных минут: устройство × дата × категория."""

    __tablename__ = "quota_usage"

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int] = mapped_column(index=True)
    date: Mapped[str] = mapped_column(String(10), index=True)  # YYYY-MM-DD
    category: Mapped[str] = mapped_column(String(16))
    minutes: Mapped[int] = mapped_column(default=0)


class QuotaBonus(Base):
    """Разовая добавка к квоте на конкретный день («+30 минут за уборку»)."""

    __tablename__ = "quota_bonus"

    id: Mapped[int] = mapped_column(primary_key=True)
    target_type: Mapped[str] = mapped_column(String(8))  # group | device
    target: Mapped[str] = mapped_column(String(32))
    date: Mapped[str] = mapped_column(String(10), index=True)
    category: Mapped[str] = mapped_column(String(16))
    minutes: Mapped[int] = mapped_column(default=30)
    comment: Mapped[str] = mapped_column(Text, default="")


class Pause(Base):
    """Временная блокировка интернета («пауза до…») для группы или устройства.
    В отличие от Rule (недельное расписание) — разовая, с моментом окончания.
    Активна, пока `until` в будущем; истёкшие записи чистит планировщик."""

    __tablename__ = "pauses"

    id: Mapped[int] = mapped_column(primary_key=True)
    target_type: Mapped[str] = mapped_column(String(8))  # group | device
    target: Mapped[str] = mapped_column(String(32))  # имя группы или id устройства
    until: Mapped[datetime] = mapped_column(DateTime, index=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    created: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


def active_pauses(db, now: datetime | None = None) -> list["Pause"]:
    now = now or datetime.now()
    return [p for p in db.query(Pause).all() if p.until > now]


class RegistrationRequest(Base):
    """Заявка с портала: человек на неизвестном устройстве представился,
    владелец сети подтверждает кнопкой в Telegram."""

    __tablename__ = "registration_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int] = mapped_column(index=True)
    name: Mapped[str] = mapped_column(String(64))
    comment: Mapped[str] = mapped_column(Text, default="")
    ts: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class EventLog(Base):
    __tablename__ = "event_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)
    kind: Mapped[str] = mapped_column(String(32))  # device_new | block | unblock | error | ...
    message: Mapped[str] = mapped_column(Text)


def log_event(db, kind: str, message: str) -> None:
    db.add(EventLog(kind=kind, message=message))
    db.commit()


class KVState(Base):
    """Служебное key-value хранилище (курсор уведомлений бота и т.п.)."""

    __tablename__ = "kv_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


def kv_get(db, key: str, default: str = "") -> str:
    row = db.get(KVState, key)
    return row.value if row else default


def kv_set(db, key: str, value: str) -> None:
    row = db.get(KVState, key)
    if row is None:
        db.add(KVState(key=key, value=value))
    else:
        row.value = value
    db.commit()
