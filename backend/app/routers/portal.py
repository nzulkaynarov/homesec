"""Публичные страницы для устройств за NAT-перехватом (enable-block-page.rsc):
«время вышло» с причиной блокировки. Без авторизации — только чтение
собственного статуса по IP клиента."""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from ..db import get_db
from ..services.restrictions import restriction_for_ip
from ..templates_env import templates

router = APIRouter()


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


@router.get("/blocked")
async def blocked_page(request: Request, db: Session = Depends(get_db)):
    info = restriction_for_ip(db, _client_ip(request))
    return templates.TemplateResponse(request, "blocked.html", {"info": info})
