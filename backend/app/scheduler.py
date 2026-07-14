"""Раз в минуту сверяет желаемое состояние с фактическим (reconcile).
Расписания-правила не требуют отдельных задач: активность окна вычисляется
на каждом тике, поэтому состояние переживает перезагрузку Pi и роутера."""

import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import delete

from . import db
from .models import EventLog, Pause
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


def _cleanup() -> None:
    """Ежедневная уборка: журнал событий старше 90 дней и истёкшие паузы."""
    session = db.session()
    try:
        now = datetime.now()
        cutoff = now - timedelta(days=EVENT_RETENTION_DAYS)
        session.execute(delete(EventLog).where(EventLog.ts < cutoff))
        session.execute(delete(Pause).where(Pause.until < now))
        session.commit()
    except Exception:
        log.exception("cleanup failed")
    finally:
        session.close()


def start() -> None:
    scheduler.add_job(_tick, "interval", minutes=1, id="reconcile", coalesce=True, max_instances=1)
    scheduler.add_job(_cleanup, "cron", hour=3, minute=30, id="cleanup", coalesce=True)
    scheduler.start()
    _tick()  # применить состояние сразу при старте


def stop() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
