"""Раз в минуту сверяет желаемое состояние с фактическим (reconcile).
Расписания-правила не требуют отдельных задач: активность окна вычисляется
на каждом тике, поэтому состояние переживает перезагрузку Pi и роутера."""

import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import delete

from . import db
from .models import (
    EventLog,
    Pause,
    PendingAction,
    QuotaBonus,
    QuotaUsage,
    kv_get,
    kv_set,
)
from .services import quota
from .services.enforcement import reconcile

log = logging.getLogger("homesec.scheduler")

scheduler = BackgroundScheduler()

EVENT_RETENTION_DAYS = 90


def _tick() -> None:
    session = db.session()
    try:
        reconcile(session)
    except Exception:
        log.exception("reconcile tick failed")
    finally:
        session.close()


def _asap_tick() -> None:
    """Быстрый опрос флага немедленной сверки (reconcile_asap): бот при мутациях
    ставит этот флаг в kv_state вместо прямого обращения к роутеру. Панель
    остаётся единственным писателем в RouterOS, гонки исключены."""
    session = db.session()
    try:
        if kv_get(session, "reconcile_asap", "") == "1":
            kv_set(session, "reconcile_asap", "")
            reconcile(session)
    except Exception:
        log.exception("reconcile_asap tick failed")
    finally:
        session.close()


def _quota_tick() -> None:
    """Учёт активных минут для квот и проверка порога предупреждений 80% (раз в
    минуту, отдельно от reconcile — reconcile дёргается и после каждого изменения
    в панели, а учёт должен идти ровно по расписанию, без двойного счёта)."""
    session = db.session()
    try:
        quota.record_activity(session)
        quota.check_quota_warnings(session)
    except Exception:
        log.exception("quota tick failed")
    finally:
        session.close()


def _cleanup() -> None:
    """Ежедневная уборка: журнал событий старше 90 дней, истёкшие паузы,
    счётчики квот старше 30 дней, неподтверждённые действия ИИ старше суток."""
    session = db.session()
    try:
        now = datetime.now()
        cutoff = now - timedelta(days=EVENT_RETENTION_DAYS)
        session.execute(delete(EventLog).where(EventLog.ts < cutoff))
        session.execute(delete(Pause).where(Pause.until < now))
        # Кнопка подтверждения старше суток бессмысленна: контекст протух
        session.execute(delete(PendingAction)
                        .where(PendingAction.created < now - timedelta(hours=24)))
        quota_cutoff = (now - timedelta(days=30)).strftime("%Y-%m-%d")
        session.execute(delete(QuotaUsage).where(QuotaUsage.date < quota_cutoff))
        session.execute(delete(QuotaBonus).where(QuotaBonus.date < quota_cutoff))
        session.commit()
    except Exception:
        log.exception("cleanup failed")
    finally:
        session.close()


def start() -> None:
    scheduler.add_job(_tick, "interval", minutes=1, id="reconcile", coalesce=True, max_instances=1)
    scheduler.add_job(_asap_tick, "interval", seconds=3, id="reconcile_asap",
                      coalesce=True, max_instances=1)
    scheduler.add_job(_quota_tick, "interval", minutes=1, id="quota",
                      coalesce=True, max_instances=1)
    scheduler.add_job(_cleanup, "cron", hour=3, minute=30, id="cleanup", coalesce=True)
    scheduler.start()
    _tick()  # применить состояние сразу при старте


def stop() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
