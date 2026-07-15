"""Заявки ребёнка «попросить ещё времени». Общая логика для детской страницы
(создание заявки с антиспамом) и бота (одобрение/отклонение). Мутации квоты
делает add_bonus_time из реестра инструментов — здесь только жизненный цикл
самой заявки."""

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import BonusRequest, Device, log_event
from . import quota

# Антиспам: не чаще одной заявки с устройства за это окно (как cooldown портала).
REQUEST_COOLDOWN = timedelta(minutes=10)
# Заявки старше суток считаются протухшими (родитель не ответил) — не блокируют
# новые и не показываются как «ожидает».
REQUEST_TTL = timedelta(hours=24)


def latest_request(db: Session, device_id: int) -> BonusRequest | None:
    return db.scalar(
        select(BonusRequest)
        .where(BonusRequest.device_id == device_id)
        .order_by(BonusRequest.created.desc())
    )


def has_fresh_pending(db: Session, device_id: int, now: datetime | None = None) -> bool:
    """Есть ли необработанная заявка младше TTL — тогда новую не плодим."""
    now = now or datetime.now()
    last = latest_request(db, device_id)
    return (last is not None and last.status == "pending"
            and now - last.created < REQUEST_TTL)


def can_request(db: Session, device_id: int, now: datetime | None = None) -> bool:
    """Разрешено ли устройству подать новую заявку (антиспам + нет висящей)."""
    now = now or datetime.now()
    last = latest_request(db, device_id)
    if last is None:
        return True
    if last.status == "pending" and now - last.created < REQUEST_TTL:
        return False  # уже ждёт ответа родителя
    return now - last.created >= REQUEST_COOLDOWN


def create_request(db: Session, dev: Device, category: str,
                   reason: str = "") -> BonusRequest | None:
    """Создаёт заявку, если антиспам позволяет. Пишет событие bonus_request
    (MAC + req#id в тексте) — его подхватывает бот и шлёт родителю кнопки.
    Возвращает заявку или None, если сейчас нельзя (ждёт/слишком часто)."""
    if category not in quota.QUOTA_CATEGORIES:
        category = "internet"
    if not can_request(db, dev.id):
        return None
    req = BonusRequest(device_id=dev.id, category=category,
                       reason=reason.strip()[:200], status="pending")
    db.add(req)
    db.commit()
    cat_label = quota.QUOTA_CATEGORY_LABELS.get(category, category)
    log_event(db, "bonus_request",
              f"Запрос времени: {dev.name} ({dev.mac}, {dev.ip}) просит «{cat_label}»"
              + (f" — «{req.reason}»" if req.reason else "") + f" [req#{req.id}]")
    return req


def resolve(db: Session, req: BonusRequest, status: str, minutes: int = 0) -> None:
    req.status = status
    req.minutes = minutes
    req.resolved = datetime.now()
    db.commit()
