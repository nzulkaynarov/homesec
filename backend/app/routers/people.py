from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Person
from ..services import house_rules
from ..templates_env import templates

router = APIRouter()


@router.get("/people")
async def people_page(request: Request, db: Session = Depends(get_db)):
    people = list(db.scalars(select(Person).order_by(Person.name)))
    ctx = {"active": "people", "people": people}
    return templates.TemplateResponse(request, "people.html", ctx)


# The panel's forms call the same service as the bot, so validation, roles and cleanup agree.


@router.post("/people/add")
async def add_person(name: str = Form(...), role: str = Form("kid"), db: Session = Depends(get_db)):
    try:
        house_rules.add_person(db, name, role)
    except house_rules.RuleError:
        pass  # an empty or taken name, or an unknown role: the list shows nothing new
    return RedirectResponse("/people", status_code=302)


@router.post("/people/{person_id}/update")
async def update_person(person_id: int, role: str = Form(...), db: Session = Depends(get_db)):
    try:
        house_rules.set_role(db, person_id, role)
    except (house_rules.RuleError, LookupError):
        pass
    return RedirectResponse("/people", status_code=302)


@router.post("/people/{person_id}/delete")
async def delete_person(person_id: int, db: Session = Depends(get_db)):
    try:
        house_rules.delete_person(db, person_id)  # with the person's rules, limits and pauses
    except (house_rules.RuleError, LookupError):
        pass
    return RedirectResponse("/people", status_code=302)
