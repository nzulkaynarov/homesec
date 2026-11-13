"""Network API for Luna AI Assistant on the GPU host.

Luna owns the family conversation; this API keeps screen-time decisions on the
Pi, which remains the only writer to the network state."""

import hmac
import json
from datetime import datetime

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..ai import tools
from ..config import settings
from ..db import get_db
from ..models import BonusRequest, Device, Person, kv_set
from ..services import bonus, family, house_rules

router = APIRouter(prefix="/api/luna/v1", tags=["luna"])

HEARTBEAT_KEY = "luna_last_heartbeat"
MEMBERS_KEY = "luna_member_chat_ids"
BOT_ROLE_BY_PERSON_ROLE = {"adult": "admin", "kid": "kid", "guest": "guest"}


def require_token(authorization: str = Header(default="")) -> None:
    expected = settings.luna_api_token
    if not expected:
        raise HTTPException(status_code=503, detail="Luna API is not configured")
    if not hmac.compare_digest(authorization, f"Bearer {expected}"):
        raise HTTPException(status_code=401, detail="Invalid token")


class Heartbeat(BaseModel):
    member_chat_ids: list[int] = Field(default_factory=list, max_length=100)


class TimeRequest(BaseModel):
    reason: str = Field(default="", max_length=500)


class TelegramLink(BaseModel):
    telegram_id: int


class QuizResult(BaseModel):
    correct: bool
    topic: str = Field(default="Эрудиция", max_length=64)


def _person_or_404(db: Session, name: str) -> Person:
    # SQLite compares only ASCII case-insensitively, and family names are Cyrillic.
    wanted = name.casefold()
    for person in db.scalars(select(Person)):
        if person.name.casefold() == wanted:
            return person
    raise HTTPException(status_code=404, detail="Unknown person")


def _devices_of(db: Session, person: Person) -> list[Device]:
    query = select(Device).where(Device.person_id == person.id, Device.kind == "personal")
    return list(db.scalars(query.order_by(Device.id)))


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/heartbeat", dependencies=[Depends(require_token)])
def heartbeat(body: Heartbeat, db: Session = Depends(get_db)) -> dict[str, str]:
    """Luna proves it is alive and shares who must be told if it stops."""
    kv_set(db, HEARTBEAT_KEY, datetime.now().isoformat())
    kv_set(db, MEMBERS_KEY, json.dumps(body.member_chat_ids))
    return {"status": "ok"}


@router.get("/people", dependencies=[Depends(require_token)])
def list_people(db: Session = Depends(get_db)) -> list[dict]:
    """The family as the Pi knows it; Luna never keeps its own copy of who exists."""
    counts: dict[int, int] = {}
    for device in db.scalars(select(Device).where(Device.kind == "personal")):
        if device.person_id is not None:
            counts[device.person_id] = counts.get(device.person_id, 0) + 1
    return [
        {
            "id": p.id,
            "name": p.name,
            "role": p.role,
            "devices": counts.get(p.id, 0),
            "telegram_id": p.telegram_id,
        }
        for p in db.scalars(select(Person).order_by(Person.name))
    ]


@router.put("/people/{person_id}/telegram", dependencies=[Depends(require_token)])
def link_telegram(
    person_id: int, body: TelegramLink, db: Session = Depends(get_db)
) -> dict[str, str]:
    """Attach a Telegram account chosen by the owner through a single-use Luna link."""
    person = db.get(Person, person_id)
    if person is None:
        raise HTTPException(status_code=404, detail="Unknown person")
    other = db.scalar(
        select(Person).where(Person.telegram_id == body.telegram_id, Person.id != person_id)
    )
    if other is not None:
        raise HTTPException(status_code=409, detail="Telegram account is linked to another person")
    if person.telegram_id is not None and person.telegram_id != body.telegram_id:
        # Relinking someone already linked would let the Luna token take over a parent's admin
        # rights; that change belongs to the owner in the HomeSec bot.
        raise HTTPException(status_code=409, detail="Person is linked to another Telegram account")
    person.telegram_id = body.telegram_id
    person.bot_role = BOT_ROLE_BY_PERSON_ROLE.get(person.role, "guest")
    db.commit()
    return {"status": "linked"}


@router.get("/people/{name}/quota", dependencies=[Depends(require_token)])
def person_quota(name: str, db: Session = Depends(get_db)) -> list[dict]:
    device_ids = {d.id for d in _devices_of(db, _person_or_404(db, name))}
    return [row for row in tools.get_quota_status(db) if row["device_id"] in device_ids]


def _clock(moment: datetime | None) -> str | None:
    return moment.isoformat(timespec="minutes") if moment is not None else None


def _restriction_view(db: Session, person: Person) -> dict | None:
    found = house_rules.restriction(db, person.id)
    if found is None:
        return None
    return {key: _clock(value) if key != "kind" else value for key, value in found.items()}


@router.get("/people/{name}/restriction", dependencies=[Depends(require_token)])
def person_restriction(name: str, db: Session = Depends(get_db)) -> dict:
    """What keeps the person offline now: bedtime or a pause, with local times
    (kind, until, pause_until after bedtime, open_until of a parent's window)."""
    return {"restriction": _restriction_view(db, _person_or_404(db, name))}


@router.post("/people/{name}/time-requests", status_code=201, dependencies=[Depends(require_token)])
def create_time_request(name: str, body: TimeRequest, db: Session = Depends(get_db)) -> dict:
    """More minutes, or during a pause a window through it. Never during bedtime:
    parents do not answer at night, and the morning opens it anyway."""
    person = _person_or_404(db, name)
    devices = _devices_of(db, person)
    if not devices:
        raise HTTPException(status_code=404, detail="Person has no devices")
    restriction = house_rules.restriction(db, person.id)
    if restriction is not None and restriction["kind"] == "bedtime":
        raise HTTPException(status_code=409, detail="Bedtime: requests open in the morning")
    category = bonus.PAUSE_CATEGORY if restriction is not None else "internet"
    request = bonus.create_request(db, devices[0], category, body.reason)
    if request is None:
        raise HTTPException(
            status_code=409, detail="A request is already pending or was sent recently"
        )
    return {"id": request.id, "status": request.status}


@router.get("/time-requests/{request_id}", dependencies=[Depends(require_token)])
def time_request_status(request_id: int, db: Session = Depends(get_db)) -> dict:
    request = db.get(BonusRequest, request_id)
    if request is None:
        raise HTTPException(status_code=404, detail="Unknown request")
    return {
        "id": request.id,
        "status": request.status,
        "minutes": request.minutes,
        "category": request.category,
    }


@router.get("/people/{name}/quiz", dependencies=[Depends(require_token)])
def quiz_availability(name: str, db: Session = Depends(get_db)) -> dict[str, bool]:
    return {"available": family.can_take_quiz_today(db, _person_or_404(db, name).id)}


@router.post("/people/{name}/quiz-result", dependencies=[Depends(require_token)])
def quiz_result(name: str, body: QuizResult, db: Session = Depends(get_db)) -> dict:
    """Records today's single attempt; a correct answer earns the bonus minutes."""
    person = _person_or_404(db, name)
    if not family.can_take_quiz_today(db, person.id):
        raise HTTPException(status_code=409, detail="Quiz already taken today")
    if not body.correct:
        family.mark_quiz_attempted(db, person.id)
        return {"awarded_minutes": 0}
    family.award_quiz_bonus(db, person, minutes=family.QUIZ_BONUS_MINUTES, topic=body.topic)
    return {"awarded_minutes": family.QUIZ_BONUS_MINUTES}
