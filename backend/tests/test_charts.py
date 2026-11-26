"""Charts: the right numbers go in, a picture comes out, only the right people may ask."""

import json
from datetime import date, datetime, timedelta

import pytest

from app.bot import access, charts_menu
from app.db import Base, engine, session
from app.models import Device, HostSample, KVState, Person, QuotaUsage
from app.services import charts, hosts, plain

TODAY = date(2026, 11, 21)
PNG = b"\x89PNG"


@pytest.fixture
def db():
    Base.metadata.create_all(engine)
    s = session()
    for model in (QuotaUsage, Device, Person, HostSample, KVState):
        s.query(model).delete()
    s.commit()
    yield s
    s.close()


def _kid_with_devices(db):
    misha = Person(name="Миша", role="kid")
    mom = Person(name="Мама", role="owner")
    db.add_all([misha, mom])
    db.flush()
    tablet = Device(mac="AA:00:00:00:00:01", name="Планшет", person_id=misha.id)
    phone = Device(mac="AA:00:00:00:00:02", name="Телефон", person_id=misha.id)
    moms = Device(mac="AA:00:00:00:00:03", name="Мамин", person_id=mom.id)
    db.add_all([tablet, phone, moms])
    db.flush()
    return tablet, phone, moms


def test_screen_week_counts_each_device_once_per_day_and_only_children(db):
    tablet, phone, moms = _kid_with_devices(db)
    today = TODAY.isoformat()
    db.add_all(
        [
            QuotaUsage(device_id=tablet.id, date=today, category="internet", minutes=90),
            QuotaUsage(device_id=tablet.id, date=today, category="games", minutes=40),
            QuotaUsage(device_id=phone.id, date=today, category="internet", minutes=30),
            QuotaUsage(device_id=moms.id, date=today, category="internet", minutes=300),
            QuotaUsage(device_id=tablet.id, date="2026-11-01", category="internet", minutes=999),
        ]
    )
    db.commit()
    days, people = charts.screen_week(db, TODAY)
    assert days[-1] == TODAY and len(days) == 7
    assert people == {"Миша": [0, 0, 0, 0, 0, 0, 120]}
    assert charts.screen_week_png(days, people).startswith(PNG)


def test_host_day_skips_shutdown_reports_and_draws(db):
    now = datetime(2026, 11, 21, 12, 0)
    for minutes in (30, 25, 15):  # 11:30, 11:35 and 11:45 make two ten-minute steps
        moment = now - timedelta(minutes=minutes)
        hosts.record(db, hosts.PC, {"cpu_max_c": 60, "mem_avail_mb": 8192}, moment)
        hosts.record(db, hosts.PI, {"cpu_c": 55}, moment)
    hosts.record(db, hosts.PC, {"stopping": True}, now)
    pc_heat, memory, pi_heat = charts.host_day(db, now)
    assert pc_heat == [
        (datetime(2026, 11, 21, 11, 30), 60.0),
        (datetime(2026, 11, 21, 11, 40), 60.0),
    ]
    assert memory[0][1] == 8.0 and len(pi_heat) == 2
    assert charts.host_day_png(pc_heat, memory, pi_heat).startswith(PNG)


def test_spend_week_names_people_and_sums_by_day(db):
    misha = Person(name="Миша", role="kid")
    db.add(misha)
    db.flush()
    ledger = {
        f"person:{misha.id}": {"usd": 0.25, "requests": 3},
        "system": {"usd": 0.05, "requests": 1},
    }
    db.add(KVState(key=f"ai_ledger:{TODAY.isoformat()}", value=json.dumps(ledger)))
    db.commit()
    days, people = charts.spend_week(db, datetime.combine(TODAY, datetime.min.time()))
    assert people["Миша"][-1] == 0.25 and people["Система"][-1] == 0.05
    assert charts.spend_week_png(days, people).startswith(PNG)


def test_only_the_owner_may_ask_for_the_server_chart_and_children_none():
    assert access.check_callback("owner", charts_menu.SERVER).allowed
    assert not access.check_callback("admin", charts_menu.SERVER).allowed
    assert access.check_callback("admin", charts_menu.SCREEN).allowed
    assert not access.check_callback("kid", charts_menu.SCREEN).allowed


def test_money_below_a_dollar_reads_in_cents():
    assert plain.money(0.0012) == "0,12 ¢"
    assert plain.money(0.25) == "25 ¢"
    assert plain.money(0.015) == "1,5 ¢" and plain.money(0.05) == "5 ¢"
    assert plain.money(12.5) == "$12.50"


def test_a_week_of_fractions_of_a_cent_is_drawn_in_cents_without_idle_people(monkeypatch):
    drawn = {}
    real = charts._png

    def spy(plt, fig):
        ax = fig.axes[0]
        drawn["legend"] = [t.get_text() for t in ax.get_legend().get_texts()]
        drawn["labels"] = [t.get_text() for t in ax.texts]
        drawn["unit"] = ax.get_ylabel()
        drawn["title"] = ax.get_title()
        return real(plt, fig)

    monkeypatch.setattr(charts, "_png", spy)
    days = charts.week_days(TODAY)
    people = {"Нурбек": [0, 0.00117, 0, 0, 0, 0.00196, 0], "Система": [0.0] * 7}
    assert charts.spend_week_png(days, people).startswith(PNG)
    assert drawn == {
        "legend": ["Нурбек"],
        "labels": ["0,12 ¢", "0,20 ¢"],
        "unit": "центы, оценка",
        "title": "Расходы на ИИ за неделю: 0,31 ¢",
    }
