"""Публичные страницы для устройств за NAT-перехватом (enable-block-page.rsc):
«время вышло» с причиной блокировки и портал регистрации неизвестных
устройств. Без авторизации; портал принимает только заявку (никаких действий),
владелец подтверждает её кнопкой в Telegram."""

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Device, RegistrationRequest, log_event
from ..services.restrictions import restriction_for_ip
from ..templates_env import templates

router = APIRouter()

REGISTER_COOLDOWN = timedelta(minutes=10)  # антиспам: одна заявка с устройства


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


@router.get("/blocked")
async def blocked_page(request: Request, db: Session = Depends(get_db)):
    info = restriction_for_ip(db, _client_ip(request))
    return templates.TemplateResponse(request, "blocked.html", {"info": info})


def _register_state(db: Session, ip: str) -> tuple[str, Device | None]:
    """-> (state, device): unknown-устройство и можно ли слать заявку."""
    dev = db.scalar(select(Device).where(Device.ip == ip)) if ip else None
    if dev is None:
        return "no_device", None
    if dev.group != "unknown":
        return "already", dev
    last = db.scalar(
        select(RegistrationRequest)
        .where(RegistrationRequest.device_id == dev.id)
        .order_by(RegistrationRequest.ts.desc())
    )
    if last and datetime.now() - last.ts < REGISTER_COOLDOWN:
        return "pending", dev
    return "form", dev


@router.get("/register")
async def register_page(request: Request, db: Session = Depends(get_db)):
    state, dev = _register_state(db, _client_ip(request))
    return templates.TemplateResponse(request, "register.html",
                                      {"state": state, "device": dev})


@router.post("/register")
async def register_submit(
    request: Request,
    name: str = Form(""),
    comment: str = Form(""),
    db: Session = Depends(get_db),
):
    state, dev = _register_state(db, _client_ip(request))
    name = name.strip()[:64]
    comment = comment.strip()[:200]
    if state == "form" and dev is not None and name:
        db.add(RegistrationRequest(device_id=dev.id, name=name, comment=comment))
        db.commit()
        log_event(db, "register_request",
                  f"Заявка на регистрацию: {dev.name} ({dev.mac}, {dev.ip}) — "
                  f"владелец: {name}" + (f", «{comment}»" if comment else ""))
        state = "sent"
    elif state == "form":
        state = "form_error"
    return templates.TemplateResponse(request, "register.html",
                                      {"state": state, "device": dev})
