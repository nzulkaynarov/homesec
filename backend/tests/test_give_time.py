"""Time for a child is given one way, whatever the parent uses: the request's buttons, «➕» in
«⏳ Квоты», a typed or spoken request to the AI, or /bonus with the child's name."""

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.filters import CommandObject
from aiogram.types import Message

from app.ai import confirmations, tools
from app.bot import handlers
from app.db import Base, engine, session
from app.models import (
    BonusRequest,
    Device,
    EventLog,
    KVState,
    Pause,
    PendingAction,
    Person,
    Quota,
    QuotaBonus,
    QuotaUsage,
    Rule,
)
from app.services import bonus, unlock

MODELS = (
    BonusRequest,
    EventLog,
    KVState,
    Pause,
    PendingAction,
    Quota,
    QuotaBonus,
    QuotaUsage,
    Rule,
    Device,
    Person,
)
EVERY_DAY = "0,1,2,3,4,5,6"


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr(tools.enforcement, "reconcile", lambda s: None)  # no network in tests
    Base.metadata.create_all(engine)
    s = session()
    for model in MODELS:
        s.query(model).delete()
    s.commit()
    yield s
    for model in MODELS:
        s.query(model).delete()
    s.commit()
    s.close()


@pytest.fixture
def family(db):
    misha = Person(name="Миша", role="kid", bot_role="kid")
    masha = Person(name="Маша", role="kid", bot_role="kid")
    mom = Person(name="Мама", role="adult", bot_role="owner")
    db.add_all([misha, masha, mom])
    db.commit()
    phone = Device(mac="00:11:22:33:44:61", ip="192.168.88.61", name="Телефон", person_id=misha.id)
    tablet = Device(mac="00:11:22:33:44:62", ip="192.168.88.62", name="Планшет", person_id=masha.id)
    db.add_all([phone, tablet])
    db.commit()
    return misha, masha, mom, phone, tablet


def _confirmed(db, name: str, args: dict) -> tuple[str, str]:
    """What the parent reads before pressing ✅, and what the bot says after it."""
    description = confirmations.describe(db, name, args)
    pending = tools.save_pending(db, name, args, description)
    cb = MagicMock(data=f"act:{pending}:yes", answer=AsyncMock())
    cb.message = MagicMock(spec=Message, text=description, edit_text=AsyncMock())
    asyncio.run(handlers.cb_confirm_action(cb))
    return description, cb.message.edit_text.await_args.args[0]


def _typed(text: str) -> MagicMock:
    message = MagicMock(spec=Message, text=text, answer=AsyncMock())
    message.chat = MagicMock(id=900)
    message.from_user = MagicMock(id=9)
    return message


def _command(handler, command: str, args: str) -> str:
    message = _typed(f"/{command} {args}")
    asyncio.run(handler(message, CommandObject(command=command, args=args)))
    return message.answer.await_args.args[0]


def test_asking_the_ai_for_time_during_a_pause_opens_it_and_answers_the_request(db, family):
    """The hole: «дай Мише ещё 30 минут» became +30 to an internet limit nobody has, so the
    parent read «Не получилось», the pause kept Misha offline and his request stayed open."""
    misha, _, _, phone, _ = family
    db.add(
        Pause(target_type="person", target=str(misha.id), until=datetime.now() + timedelta(hours=2))
    )
    db.commit()
    request = bonus.create_request(db, phone, bonus.PAUSE_CATEGORY)
    assert request is not None

    asked, result = _confirmed(db, "give_time", {"who": "Миша", "minutes": 30})
    assert asked.splitlines()[:3] == ["➕ Дать ещё времени", "Кому: Миша", "Сколько: +30 мин"]
    assert "➡ ✅ Миша: интернет открыт до" in result and "Не получилось" not in result
    assert unlock.open_until(db, misha.id) is not None
    db.expire_all()
    assert (request.status, request.minutes) == ("approved", 30)  # Luna tells Misha


def test_without_a_pause_the_minutes_go_to_every_limit(db, family):
    misha, *_ = family
    db.add(
        Quota(
            target_type="person",
            target=str(misha.id),
            category="games",
            minutes_per_day=60,
            days=EVERY_DAY,
        )
    )
    db.commit()
    _, result = _confirmed(db, "give_time", {"who": "миша", "minutes": 15})
    assert result.endswith("➡ ✅ Миша: +15 мин к лимитам: игры.")
    assert [(b.category, b.minutes) for b in db.query(QuotaBonus)] == [("games", 15)]


def test_until_the_end_of_the_pause_is_said_in_words(db, family):
    asked = confirmations.describe(db, "give_time", {"who": "дети", "minutes": 0})
    assert asked.splitlines()[1:3] == [
        "Кому: все дети",
        "Сколько: до конца паузы, без паузы — весь день без лимитов",
    ]


def test_time_for_all_children_and_who_cannot_get_it(db, family):
    result = tools.run_tool(db, "give_time", {"who": "дети", "minutes": 30}, "bot")
    assert result.splitlines() == [
        "✅ Маша: ограничений сейчас нет, интернет и так открыт.",
        "✅ Миша: ограничений сейчас нет, интернет и так открыт.",
    ]
    with pytest.raises(tools.ToolError, match="взрослый"):
        tools.run_tool(db, "give_time", {"who": "Мама", "minutes": 30}, "bot")
    with pytest.raises(tools.ToolError, match="Не нашёл ребёнка «Петя». Дети: Маша, Миша."):
        tools.run_tool(db, "give_time", {"who": "Петя", "minutes": 30}, "bot")
    with pytest.raises(tools.ToolError, match="от 1 до 720"):
        tools.run_tool(db, "give_time", {"who": "Миша", "minutes": 900}, "bot")


def test_a_limit_for_all_children_runs_as_confirmed(db, family):
    """The confirmation said «Кому: все дети» for «дети», and the tool then refused «дети»."""
    asked, result = _confirmed(
        db, "set_quota", {"target": "дети", "category": "games", "minutes_per_day": 60}
    )
    assert "Кому: все дети" in asked
    assert "Не нашёл" not in result and "Не получилось" not in result
    quota = db.query(Quota).one()
    assert (quota.target_type, quota.target, quota.minutes_per_day) == ("group", "kid", 60)


def test_bonus_command_with_a_childs_name_gives_time_like_the_buttons(db, family):
    misha, _, _, phone, _ = family
    db.add(
        Pause(target_type="person", target=str(misha.id), until=datetime.now() + timedelta(hours=2))
    )
    db.commit()
    request = bonus.create_request(db, phone, bonus.PAUSE_CATEGORY)
    assert request is not None
    said = _command(handlers.cmd_bonus, "bonus", "Миша 30")
    assert said.startswith("✅ Миша: интернет открыт до")
    db.expire_all()
    assert unlock.open_until(db, misha.id) is not None and request.status == "approved"
    assert _command(handlers.cmd_bonus, "bonus", "Мама 30") == (
        "Мама — взрослый, время дают только детям."
    )


def test_pause_and_resume_commands_understand_a_childs_name(db, family):
    """The usage promised «кто», but only device names were understood."""
    _, masha, _, _, tablet = family
    said = _command(handlers.cmd_pause, "pause", "Маша 30")
    assert said.startswith("⏸ Маша: без интернета до")
    assert {(p.target_type, p.target) for p in db.query(Pause)} == {("person", str(masha.id))}
    assert _command(handlers.cmd_resume, "resume", "Маша") == "▶️ Маша: интернет снова включён"
    assert db.query(Pause).count() == 0
    assert _command(handlers.cmd_pause, "pause", "Планшет 30").startswith("⏸ Планшет")
    assert _command(handlers.cmd_pause, "pause", "Петя 30") == handlers.NOBODY.format("Петя")


def test_the_plus_buttons_in_the_quota_report_answer_a_waiting_request(db, family):
    from app.bot import reports

    misha, _, _, phone, _ = family
    request = bonus.create_request(db, phone, "internet")
    assert request is not None
    said, given = reports._grant_sync(misha.id, 15, limits_only=False)
    assert given and said == "✅ Миша: ограничений сейчас нет, интернет и так открыт."
    db.expire_all()
    assert (request.status, request.minutes) == ("approved", 0)  # nothing added: Luna says so
