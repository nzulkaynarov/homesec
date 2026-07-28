"""Строки, привязанные к устройству, но живущие без внешних ключей.

FK в SQLite выключены (см. db.py), а у большинства таблиц device_id — просто
int или строковый `target` вида "17". Поэтому удаление и объединение устройств
обязаны разбираться со связями руками:

* удаление без чистки оставляло осиротевшую строку device_macs, и когда тот же
  MAC снова появлялся в DHCP, UNIQUE-ошибка роняла весь reconcile-тик;
* объединение без переноса теряло дневной учёт минут, выданные бонусы и
  висящую заявку ребёнка — то есть телефон, сменивший «приватный» MAC днём,
  после нажатия «объединить» частично обнулял экранное время.
"""

from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..models import (
    BonusRequest,
    DeviceMac,
    Pause,
    Quota,
    QuotaBonus,
    QuotaUsage,
    RegistrationRequest,
    Rule,
)

# Таблицы, где устройство адресуется парой (target_type='device', target='<id>').
_TARGETED = (Pause, QuotaBonus, Quota, Rule)


def purge_device_rows(db: Session, device_id: int) -> None:
    """Удаляет всё, что ссылалось на устройство. Коммит — за вызывающим."""
    key = str(device_id)
    db.execute(delete(DeviceMac).where(DeviceMac.device_id == device_id))
    db.execute(delete(QuotaUsage).where(QuotaUsage.device_id == device_id))
    db.execute(delete(BonusRequest).where(BonusRequest.device_id == device_id))
    db.execute(delete(RegistrationRequest).where(RegistrationRequest.device_id == device_id))
    for model in _TARGETED:
        db.execute(delete(model).where(model.target_type == "device", model.target == key))


def move_device_rows(db: Session, from_id: int, to_id: int) -> None:
    """Перевешивает связанные строки с дубля на настоящее устройство (merge).
    Коммит — за вызывающим."""
    from_key, to_key = str(from_id), str(to_id)

    # Учёт минут: за один день на категорию должна остаться ОДНА строка,
    # иначе прогресс-бары считают дважды по-разному в разных местах.
    existing = {
        (u.date, u.category): u
        for u in db.scalars(select(QuotaUsage).where(QuotaUsage.device_id == to_id))
    }
    for row in list(db.scalars(select(QuotaUsage).where(QuotaUsage.device_id == from_id))):
        twin = existing.get((row.date, row.category))
        if twin is None:
            row.device_id = to_id
            existing[(row.date, row.category)] = row
        else:
            twin.minutes += row.minutes
            db.delete(row)

    for req in db.scalars(select(BonusRequest).where(BonusRequest.device_id == from_id)):
        req.device_id = to_id
    regs = select(RegistrationRequest).where(RegistrationRequest.device_id == from_id)
    for reg in db.scalars(regs):
        reg.device_id = to_id
    for model in _TARGETED:
        _retarget(db, model, from_key, to_key)


def _retarget(db: Session, model: Any, from_key: str, to_key: str) -> None:
    """Переписывает target у строк, адресующих устройство строкой-id."""
    stmt = select(model).where(model.target_type == "device", model.target == from_key)
    for entity in db.scalars(stmt):
        entity.target = to_key
