"""A parent's "yes" changes what really holds the child back."""

import asyncio
from datetime import datetime, timedelta

import pytest

from app.ai import tools
from app.db import Base, engine, session
from app.models import (
    Device,
    EventLog,
    KVState,
    Pause,
    Person,
    Quota,
    QuotaBonus,
    QuotaUsage,
    Rule,
)
from app.services import (
    enforcement,
    house_rules,
    pauses,
    quota,
    restrictions,
    time_grants,
    unlock,
)

NOON = datetime(2026, 11, 13, 12, 0)
EVERY_DAY = "0,1,2,3,4,5,6"


@pytest.fixture
def db():
    Base.metadata.create_all(engine)
    s = session()
    models = (EventLog, KVState, Pause, Quota, QuotaBonus, QuotaUsage, Rule, Device, Person)
    for model in models:
        s.query(model).delete()
    s.commit()
    yield s
    for model in models:
        s.query(model).delete()
    s.commit()
    s.close()


def _kid(db) -> Person:
    kid = Person(name="Миша", role="kid", bot_role="kid")
    db.add(kid)
    db.commit()
    return kid


def _phone(db, kid: Person) -> Device:
    device = Device(mac="00:11:22:33:44:51", ip="192.168.88.51", name="Телефон", person_id=kid.id)
    db.add(device)
    db.commit()
    return device


def _used(db, device: Device, category: str, minutes: int, day: datetime) -> None:
    db.add(QuotaUsage(device_id=device.id, date=f"{day:%Y-%m-%d}", category=category,
                      minutes=minutes))
    db.commit()


def test_limits_grow_by_the_minutes(db):
    kid = _kid(db)
    for category in ("games", "video"):
        db.add(Quota(name=category, target_type="person", target=str(kid.id), category=category,
                     minutes_per_day=60, days="0,1,2,3,4,5,6", enabled=True))
    db.commit()
    said = time_grants.grant(db, kid.id, 30, "тест", NOON)
    assert said.text.startswith("+30 мин к лимитам: игры, ") and said.minutes == 30
    added = sorted((b.category, b.minutes) for b in db.query(QuotaBonus))
    assert added == [("games", 30), ("video", 30)]


def test_a_pause_opens_for_the_minutes(db):
    kid = _kid(db)
    now = datetime.now()
    db.add(Pause(target_type="person", target=str(kid.id), until=now + timedelta(hours=2)))
    db.commit()
    said = time_grants.grant(db, kid.id, 30, "тест")
    assert said.text.startswith("интернет открыт до") and "пауза продолжится" in said.text
    assert unlock.open_until(db, kid.id) is not None
    assert db.query(QuotaBonus).count() == 0


def test_nothing_is_added_at_night(db):
    kid = _kid(db)
    now = datetime.now()
    start = (now - timedelta(minutes=30)).strftime("%H:%M")
    end = (now + timedelta(hours=2)).strftime("%H:%M")
    db.add(Rule(name="Отбой", target_type="person", target=str(kid.id), days="0,1,2,3,4,5,6",
                start_time=start, end_time=end, enabled=True))
    db.commit()
    with pytest.raises(time_grants.GrantError, match="ночной режим"):
        time_grants.grant(db, kid.id, 30, "тест")


def test_without_restrictions_it_says_so_and_adds_nothing(db):
    kid = _kid(db)
    said = time_grants.grant(db, kid.id, 30, "тест", NOON)
    assert said == time_grants.Granted("ограничений сейчас нет, интернет и так открыт", 0)
    assert db.query(QuotaBonus).count() == 0


def test_a_limit_set_in_the_panel_grows_too(db):
    """The panel sets only group and device limits, and the internet one switches the child
    off: a grant has to reach it, not only the limits set for the child in the bot."""
    kid = _kid(db)
    phone = _phone(db, kid)
    db.add(Quota(target_type="group", target="kid", category="internet", minutes_per_day=120,
                 days=EVERY_DAY))
    db.add(Quota(name="Игры", target_type="person", target=str(kid.id), category="games",
                 minutes_per_day=60, days=EVERY_DAY))
    db.commit()
    _used(db, phone, "internet", 120, NOON)
    assert quota.exhausted(db, [phone], NOON) == {phone.id: {"internet"}}

    said = time_grants.grant(db, kid.id, 15, "тест", NOON)
    assert said == time_grants.Granted("+15 мин к лимитам: интернет целиком, игры", 15)
    assert quota.exhausted(db, [phone], NOON) == {}


def test_a_device_limit_grows_too(db):
    kid = _kid(db)
    phone = _phone(db, kid)
    db.add(Quota(target_type="device", target=str(phone.id), category="video",
                 minutes_per_day=30, days=EVERY_DAY))
    db.commit()
    _used(db, phone, "video", 30, NOON)
    said = time_grants.grant_limits(db, kid.id, 30, "тест", NOON)
    assert said.text == "+30 мин к лимитам: youtube и видео"
    assert quota.exhausted(db, [phone], NOON) == {}


def test_more_time_never_shortens_an_open_window(db):
    """A parent opened the pause until in two hours; «+15» later adds to that, not takes away."""
    kid = _kid(db)
    now = datetime.now()
    db.add(Pause(target_type="person", target=str(kid.id), until=now + timedelta(hours=3)))
    db.commit()
    unlock.open_window(db, kid.id, now + timedelta(hours=2))
    said = time_grants.grant(db, kid.id, 15, "тест")
    opened = unlock.open_until(db, kid.id)
    assert opened is not None and opened >= now + timedelta(hours=2, minutes=14)
    assert "потом пауза продолжится" in said.text


def test_until_the_end_of_the_pause_nothing_continues_after_it(db):
    kid = _kid(db)
    until = datetime.now() + timedelta(hours=2)
    db.add(Pause(target_type="person", target=str(kid.id), until=until))
    db.commit()
    said = time_grants.grant(db, kid.id, time_grants.WHOLE_DAY, "тест")
    assert said.text == f"интернет открыт до {until:%H:%M}, до конца паузы"
    opened = unlock.open_until(db, kid.id)
    assert opened is not None and abs(opened - until) < timedelta(seconds=1)


def test_the_buttons_under_a_limit_alert_add_to_the_limits_and_leave_the_pause(db):
    kid = _kid(db)
    _phone(db, kid)
    db.add(Quota(name="Игры", target_type="person", target=str(kid.id), category="games",
                 minutes_per_day=60, days=EVERY_DAY))
    until = datetime.now() + timedelta(hours=2)
    db.add(Pause(target_type="person", target=str(kid.id), until=until))
    db.commit()
    said = time_grants.grant_limits(db, kid.id, 15, "тест")
    assert said.text == f"+15 мин к лимитам: игры; пауза до {until:%H:%M} остаётся"
    assert unlock.open_until(db, kid.id) is None
    assert [(b.category, b.minutes) for b in db.query(QuotaBonus)] == [("games", 15)]


def test_the_buttons_under_a_limit_alert_add_nothing_at_night(db):
    kid = _kid(db)
    db.add(Quota(name="Игры", target_type="person", target=str(kid.id), category="games",
                 minutes_per_day=60, days=EVERY_DAY))
    db.add(Rule(name="Отбой", target_type="person", target=str(kid.id), days=EVERY_DAY,
                start_time="11:00", end_time="13:00", enabled=True))
    db.commit()
    with pytest.raises(time_grants.GrantError, match="ночной режим до 13:00"):
        time_grants.grant_limits(db, kid.id, 15, "тест", NOON)
    assert db.query(QuotaBonus).count() == 0


def test_a_device_switched_off_by_hand_is_not_called_open(db):
    kid = _kid(db)
    phone = _phone(db, kid)
    phone.blocked_manual = True
    db.commit()
    said = time_grants.grant(db, kid.id, 30, "тест", NOON)
    assert said.minutes == 0
    assert "выключено вручную: «Телефон»" in said.text and "открыт" not in said.text


PHONE_CLIENT = "hs-001122334451"  # the phone's AdGuard client


def _state(db, now: datetime) -> dict:
    """What the router and AdGuard would get at `now`: the real enforcement answer."""
    return enforcement._desired_state(db, list(db.query(Device)), now, self_ips=set())


def _spent(db, kid: Person, phone: Device, category: str, *days: datetime) -> None:
    db.add(Quota(target_type="person", target=str(kid.id), category=category,
                 minutes_per_day=60, days=EVERY_DAY))
    db.commit()
    for day in {f"{d:%Y-%m-%d}": d for d in days}.values():  # each date the test looks at
        _used(db, phone, category, 70, day)


def test_a_grant_during_a_pause_opens_a_used_up_internet_limit_for_its_minutes(db):
    """The hole: «➕ 15 мин» opened a window through the pause, the used-up internet limit still
    held the child, and the parent read that it opened."""
    kid = _kid(db)
    phone = _phone(db, kid)
    now = datetime.now()
    db.add(Pause(target_type="person", target=str(kid.id), until=now + timedelta(hours=2)))
    db.commit()
    _spent(db, kid, phone, "internet", now, now + timedelta(minutes=16))
    before = _state(db, now)
    assert phone.ip in before["lists"]["hs-blocked"]
    assert before["quota_blocked"] == {}  # under a pause the pause is the reason

    said = time_grants.grant(db, kid.id, 15, "тест")
    assert said.minutes == 15 and said.text.startswith("интернет открыт до ")
    assert said.text.endswith(
        ", исчерпанные лимиты (интернет целиком) до этого времени тоже не мешают; "
        "потом пауза продолжится"
    )
    assert phone.ip not in _state(db, datetime.now())["lists"]["hs-blocked"]
    assert restrictions.restriction_for_ip(db, phone.ip) is None
    assert db.query(QuotaBonus).count() == 0  # the window opens them, nothing is added

    closed = _state(db, datetime.now() + timedelta(minutes=16))
    assert phone.ip in closed["lists"]["hs-blocked"]  # the pause holds again
    assert closed["quota_blocked"] == {}  # and the parent hears no "the limit ran out"


def test_a_grant_during_a_pause_opens_a_used_up_category_limit_too(db):
    kid = _kid(db)
    phone = _phone(db, kid)
    now = datetime.now()
    db.add(Pause(target_type="person", target=str(kid.id), until=now + timedelta(hours=2)))
    db.commit()
    _spent(db, kid, phone, "games", now)
    assert "roblox" in _state(db, now)["ag_clients"][PHONE_CLIENT]["blocked_services"]

    said = time_grants.grant(db, kid.id, 30, "тест")
    assert "исчерпанные лимиты (игры) до этого времени тоже не мешают" in said.text
    after = _state(db, datetime.now())
    assert phone.ip not in after["lists"]["hs-blocked"] and PHONE_CLIENT not in after["ag_clients"]


def test_until_the_end_of_the_pause_says_the_used_up_limit_holds_after_it(db):
    kid = _kid(db)
    phone = _phone(db, kid)
    now = datetime.now()
    until = now + timedelta(minutes=40)
    db.add(Pause(target_type="person", target=str(kid.id), until=until))
    db.commit()
    later = until + timedelta(minutes=1)
    _spent(db, kid, phone, "internet", now, later)

    said = time_grants.grant(db, kid.id, time_grants.WHOLE_DAY, "тест")
    assert said.text == (
        f"интернет открыт до {until:%H:%M}, до конца паузы, исчерпанные лимиты (интернет "
        "целиком) до этого времени тоже не мешают; потом лимиты снова действуют"
    )
    assert phone.ip not in _state(db, datetime.now())["lists"]["hs-blocked"]
    after = _state(db, later)
    assert phone.ip in after["lists"]["hs-blocked"]
    assert after["quota_blocked"] == {phone.ip: phone}  # now the limit is the reason, and told


def test_a_grant_during_a_pause_says_a_device_switched_off_by_hand_stays_off(db):
    kid = _kid(db)
    phone = _phone(db, kid)
    phone.blocked_manual = True
    db.add(Pause(target_type="person", target=str(kid.id),
                 until=datetime.now() + timedelta(hours=1)))
    db.commit()
    said = time_grants.grant(db, kid.id, 15, "тест")
    assert said.text.endswith("; выключено вручную: «Телефон», время это не изменит")
    assert phone.ip in _state(db, datetime.now())["lists"]["hs-blocked"]


def test_a_window_does_not_open_bedtime(db):
    kid = _kid(db)
    phone = _phone(db, kid)
    now = datetime.now()
    db.add(Pause(target_type="person", target=str(kid.id), until=now + timedelta(hours=2)))
    db.commit()
    _spent(db, kid, phone, "internet", now)
    time_grants.grant(db, kid.id, 30, "тест")
    start = (now - timedelta(minutes=5)).strftime("%H:%M")
    end = (now + timedelta(hours=1)).strftime("%H:%M")
    db.add(Rule(name="Отбой", target_type="person", target=str(kid.id), days=EVERY_DAY,
                start_time=start, end_time=end, enabled=True))
    db.commit()
    assert phone.ip in _state(db, datetime.now())["lists"]["hs-blocked"]


def test_a_grant_during_a_pause_opens_only_the_limits_used_up_then(db):
    """The hole: a grant during a pause switched off every limit for the whole window, also
    the games limit that had 40 minutes left, so games stayed open after it ran out."""
    kid = _kid(db)
    phone = _phone(db, kid)
    now = datetime.now()
    db.add(Pause(target_type="person", target=str(kid.id), until=now + timedelta(hours=2)))
    db.commit()
    _spent(db, kid, phone, "internet", now)
    db.add(Quota(target_type="person", target=str(kid.id), category="games", minutes_per_day=60,
                 days=EVERY_DAY))
    db.commit()
    _used(db, phone, "games", 20, now)  # 40 minutes of games left when the parent says yes

    said = time_grants.grant(db, kid.id, time_grants.WHOLE_DAY, "тест")
    assert "исчерпанные лимиты (интернет целиком)" in said.text and "игры" not in said.text
    window = unlock.window(db, kid.id)
    assert window is not None and window.spent == frozenset({"internet"})
    opened = _state(db, datetime.now())
    assert phone.ip not in opened["lists"]["hs-blocked"]
    assert PHONE_CLIENT not in opened["ag_clients"]

    _used(db, phone, "games", 50, now)  # the games limit runs out during the window
    held = _state(db, datetime.now())
    assert phone.ip not in held["lists"]["hs-blocked"]  # the internet stays open
    assert "roblox" in held["ag_clients"][PHONE_CLIENT]["blocked_services"]  # games do not


def test_more_time_keeps_the_limits_the_window_already_opened(db):
    kid = _kid(db)
    now = datetime.now()
    db.add(Pause(target_type="person", target=str(kid.id), until=now + timedelta(hours=3)))
    db.commit()
    unlock.open_window(db, kid.id, now + timedelta(minutes=30), ["games"])
    time_grants.grant(db, kid.id, 15, "тест")
    window = unlock.window(db, kid.id)
    assert window is not None and window.spent == frozenset({"games"})


def test_a_window_stored_before_the_limits_were_kept_opens_every_limit(db):
    kid = _kid(db)
    until = datetime.now() + timedelta(minutes=30)
    db.add(KVState(key=f"{unlock.PREFIX}{kid.id}", value=until.isoformat(timespec="seconds")
                   + ";" + datetime.now().isoformat()))
    db.commit()
    window = unlock.window(db, kid.id)
    assert window is not None and window.spent is None
    assert window.lets_through_limit("games") and window.lets_through_limit("internet")


def _resume_rules_button(db, kid: Person) -> None:
    from app.bot import rules_menu

    rules_menu._typing.clear()
    asyncio.run(rules_menu.cb_resume(_press(f"hrr:{kid.id}")))


def _press(data: str):
    from unittest.mock import AsyncMock, MagicMock

    from aiogram.types import Message

    cb = MagicMock(data=data, answer=AsyncMock())
    cb.from_user.id = 1
    cb.message = MagicMock(spec=Message, edit_text=AsyncMock())
    cb.message.chat = MagicMock(id=1)
    return cb


RESUMES = {
    "pause screen, the child": lambda db, kid: pauses.resume(db, pauses.Target("person", kid.id)),
    "pause screen, all children": lambda db, kid: pauses.resume(db, pauses.ALL_KIDS),
    "all pauses": lambda db, kid: house_rules.resume_all(db),
    "rules screen": _resume_rules_button,
    "AI": lambda db, kid: tools.run_tool(db, "resume_internet", {"target": kid.name}, "bot"),
}


@pytest.mark.parametrize("way", RESUMES)
def test_lifting_the_pause_gives_the_limits_back(db, way):
    """The window outlived the pause: after «▶️ Снять паузу» the used-up limits stayed open
    until the window's end, up to 12 hours, and no screen said why."""
    kid = _kid(db)
    phone = _phone(db, kid)
    now = datetime.now()
    db.add(Pause(target_type="person", target=str(kid.id), until=now + timedelta(hours=2)))
    db.commit()
    _spent(db, kid, phone, "internet", now)
    time_grants.grant(db, kid.id, time_grants.WHOLE_DAY, "тест")
    assert phone.ip not in _state(db, datetime.now())["lists"]["hs-blocked"]

    RESUMES[way](db, kid)
    db.expire_all()
    assert unlock.open_until(db, kid.id) is None, way
    after = _state(db, datetime.now())
    assert after["quota_blocked"] == {phone.ip: phone}, way  # the limit holds and is told


def test_lifting_a_device_pause_keeps_the_window_while_the_child_is_paused(db):
    kid = _kid(db)
    phone = _phone(db, kid)
    now = datetime.now()
    db.add(Pause(target_type="person", target=str(kid.id), until=now + timedelta(hours=2)))
    db.add(Pause(target_type="device", target=str(phone.id), until=now + timedelta(hours=1)))
    db.commit()
    time_grants.grant(db, kid.id, 30, "тест")
    pauses.resume(db, pauses.Target("device", phone.id))
    assert unlock.open_until(db, kid.id) is not None  # the child's own pause is still there
    pauses.resume(db, pauses.Target("person", kid.id))
    assert unlock.open_until(db, kid.id) is None
