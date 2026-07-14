from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import GROUPS, Person
from ..templates_env import templates

router = APIRouter()


@router.get("/people")
async def people_page(request: Request, db: Session = Depends(get_db)):
    people = list(db.scalars(select(Person).order_by(Person.name)))
    ctx = {"active": "people", "people": people}
    return templates.TemplateResponse(request, "people.html", ctx)


@router.post("/people/add")
async def add_person(name: str = Form(...), role: str = Form("kid"), db: Session = Depends(get_db)):
    name = name.strip()
    if name and role in GROUPS and not db.scalar(select(Person).where(Person.name == name)):
        db.add(Person(name=name, role=role))
        db.commit()
    return RedirectResponse("/people", status_code=302)


@router.post("/people/{person_id}/update")
async def update_person(person_id: int, role: str = Form(...), db: Session = Depends(get_db)):
    person = db.get(Person, person_id)
    if person and role in GROUPS:
        person.role = role
        db.commit()
    return RedirectResponse("/people", status_code=302)


@router.post("/people/{person_id}/delete")
async def delete_person(person_id: int, db: Session = Depends(get_db)):
    person = db.get(Person, person_id)
    if person:
        for dev in person.devices:
            dev.person_id = None
        db.delete(person)
        db.commit()
    return RedirectResponse("/people", status_code=302)
