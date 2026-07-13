"""Раз в минуту сверяет желаемое состояние с фактическим (reconcile).
Расписания-правила не требуют отдельных задач: активность окна вычисляется
на каждом тике, поэтому состояние переживает перезагрузку Pi и роутера."""

import logging

from apscheduler.schedulers.background import BackgroundScheduler

from . import db
from .services.enforcement import reconcile

log = logging.getLogger("homesec.scheduler")

scheduler = BackgroundScheduler()


def _tick() -> None:
    session = db.session()
    try:
        reconcile(session)
    except Exception:
        log.exception("reconcile tick failed")
    finally:
        session.close()


def start() -> None:
    scheduler.add_job(_tick, "interval", minutes=1, id="reconcile", coalesce=True, max_instances=1)
    scheduler.start()
    _tick()  # применить состояние сразу при старте


def stop() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
