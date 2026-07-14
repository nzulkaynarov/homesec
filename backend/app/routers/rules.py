import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import db as dbmod
from ..db import get_db
from ..models import Device, GroupPolicy, Quota, Rule, log_event
from ..services.adguard import SERVICE_CATEGORIES
from ..services.enforcement import reconcile, rule_is_active
from ..services.quota import QUOTA_CATEGORIES, QUOTA_CATEGORY_LABELS
from ..templates_env import templates

log = logging.getLogger("homesec.rules")
router = APIRouter()


def _reconcile_bg() -> None:
    session = dbmod.session()
    try:
        reconcile(session)
    except Exception:
        log.exception("background reconcile failed")
    finally:
        session.close()


@router.get("/rules")
async def rules_page(request: Request, db: Session = Depends(get_db)):
    rules = list(db.scalars(select(Rule).order_by(Rule.id)))
    devices = list(db.scalars(select(Device).order_by(Device.name)))
    policies = {p.group: p for p in db.scalars(select(GroupPolicy))}
    device_names = {str(d.id): d.name for d in devices}
    return templates.TemplateResponse(request, "rules.html", {
        "active": "rules",
        "rules": rules,
        "devices": devices,
        "policies": policies,
        "device_names": device_names,
        "rule_is_active": rule_is_active,
        "quotas": list(db.scalars(select(Quota).order_by(Quota.id))),
        "QUOTA_LABELS": QUOTA_CATEGORY_LABELS,
    })


@router.post("/rules/add")
async def add_rule(
    tasks: BackgroundTasks,
    name: str = Form(...),
    target_type: str = Form("group"),
    target: str = Form(...),
    days: list[str] = Form([]),
    start_time: str = Form("22:00"),
    end_time: str = Form("07:00"),
    db: Session = Depends(get_db),
):
    db.add(Rule(
        name=name.strip() or "Без названия",
        target_type=target_type if target_type in ("group", "device") else "group",
        target=target,
        days=",".join(sorted(set(days) & {"0", "1", "2", "3", "4", "5", "6"})),
        start_time=start_time,
        end_time=end_time,
    ))
    db.commit()
    log_event(db, "rule", f"Создано правило «{name}» ({start_time}–{end_time})")
    tasks.add_task(_reconcile_bg)
    return RedirectResponse("/rules", status_code=302)


@router.post("/rules/{rule_id}/toggle")
async def toggle_rule(rule_id: int, tasks: BackgroundTasks, db: Session = Depends(get_db)):
    rule = db.get(Rule, rule_id)
    if rule:
        rule.enabled = not rule.enabled
        db.commit()
    tasks.add_task(_reconcile_bg)
    return RedirectResponse("/rules", status_code=302)


@router.post("/rules/{rule_id}/delete")
async def delete_rule(rule_id: int, tasks: BackgroundTasks, db: Session = Depends(get_db)):
    rule = db.get(Rule, rule_id)
    if rule:
        db.delete(rule)
        db.commit()
    tasks.add_task(_reconcile_bg)
    return RedirectResponse("/rules", status_code=302)


@router.post("/quotas/add")
async def add_quota(
    tasks: BackgroundTasks,
    name: str = Form(""),
    target_type: str = Form("group"),
    target: str = Form(...),
    category: str = Form(...),
    minutes_per_day: int = Form(120),
    days: list[str] = Form([]),
    db: Session = Depends(get_db),
):
    if category not in QUOTA_CATEGORIES:
        return RedirectResponse("/rules", status_code=302)
    db.add(Quota(
        name=name.strip() or QUOTA_CATEGORY_LABELS[category],
        target_type=target_type if target_type in ("group", "device") else "group",
        target=target,
        category=category,
        minutes_per_day=max(1, min(int(minutes_per_day), 1440)),
        days=",".join(sorted(set(days) & {"0", "1", "2", "3", "4", "5", "6"})) or "0,1,2,3,4,5,6",
    ))
    db.commit()
    log_event(db, "quota", f"Создана квота «{name}»: {category}, {minutes_per_day} мин/день")
    tasks.add_task(_reconcile_bg)
    return RedirectResponse("/rules", status_code=302)


@router.post("/quotas/{quota_id}/toggle")
async def toggle_quota(quota_id: int, tasks: BackgroundTasks, db: Session = Depends(get_db)):
    q = db.get(Quota, quota_id)
    if q:
        q.enabled = not q.enabled
        db.commit()
    tasks.add_task(_reconcile_bg)
    return RedirectResponse("/rules", status_code=302)


@router.post("/quotas/{quota_id}/delete")
async def delete_quota(quota_id: int, tasks: BackgroundTasks, db: Session = Depends(get_db)):
    q = db.get(Quota, quota_id)
    if q:
        db.delete(q)
        db.commit()
    tasks.add_task(_reconcile_bg)
    return RedirectResponse("/rules", status_code=302)


@router.post("/policies/{group}")
async def update_policy(
    group: str,
    tasks: BackgroundTasks,
    categories: list[str] = Form([]),
    safe_search: str = Form(""),
    db: Session = Depends(get_db),
):
    policy = db.scalar(select(GroupPolicy).where(GroupPolicy.group == group))
    if policy:
        policy.blocked_services = ",".join(c for c in categories if c in SERVICE_CATEGORIES)
        policy.safe_search = bool(safe_search)
        db.commit()
        log_event(db, "policy", f"Обновлена политика группы «{group}»")
    tasks.add_task(_reconcile_bg)
    return RedirectResponse("/rules", status_code=302)
