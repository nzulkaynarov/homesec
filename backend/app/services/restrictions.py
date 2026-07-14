"""Почему устройству сейчас ограничен интернет — для публичной страницы
«время вышло» (/blocked) и портала регистрации. Только чтение состояния,
никаких мутаций."""

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Device, Rule, active_pauses
from . import quota
from .enforcement import rule_is_active


def _rule_until(rule: Rule, now: datetime) -> datetime | None:
    """Когда закончится активное сейчас окно правила."""
    try:
        end_h, end_m = (int(x) for x in rule.end_time.split(":"))
    except ValueError:
        return None
    end = now.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
    if end <= now:  # окно через полночь — конец завтра
        end += timedelta(days=1)
    return end


def restriction_for_ip(db: Session, ip: str) -> dict | None:
    """Описание действующего ограничения для IP или None. Формат:
    {kind: manual|pause|quota|rule|unknown, device, until?, rule_name?, quota: [...]}"""
    if not ip:
        return None
    dev = db.scalar(select(Device).where(Device.ip == ip))
    if dev is None:
        return None
    now = datetime.now()
    quota_rows = quota.progress(db, [dev], now).get(dev.id, [])
    base = {"device": dev, "quota": quota_rows}

    if dev.blocked_manual:
        return {"kind": "manual", **base}

    my_pauses = [
        p for p in active_pauses(db, now)
        if (p.target_type == "device" and p.target == str(dev.id))
        or (p.target_type == "group" and p.target == dev.group)
    ]
    if my_pauses:
        return {"kind": "pause", "until": max(p.until for p in my_pauses), **base}

    if any(r["category"] == "internet" and r["used"] >= r["limit"] for r in quota_rows):
        return {"kind": "quota", **base}

    for rule in db.scalars(select(Rule)):
        if not rule_is_active(rule, now):
            continue
        if (rule.target_type == "group" and rule.target == dev.group) or (
            rule.target_type == "device" and str(rule.target) == str(dev.id)
        ):
            return {"kind": "rule", "rule_name": rule.name,
                    "until": _rule_until(rule, now), **base}

    if dev.group == "unknown" and settings.block_unknown:
        return {"kind": "unknown", **base}
    return None
