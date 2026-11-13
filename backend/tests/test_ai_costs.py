"""Cost attribution, personal limits and message helpers for the AI layer."""

import pytest

from app.ai import client
from app.bot import texts
from app.config import settings
from app.db import Base, engine, session
from app.models import KVState
from app.services import family


@pytest.fixture
def db():
    Base.metadata.create_all(engine)
    s = session()
    s.query(KVState).delete()
    s.commit()
    yield s
    s.close()


def test_estimate_usd_uses_list_prices_and_cache_discount():
    assert client.estimate_usd("claude-opus-4-8", 1_000_000, 0) == pytest.approx(5.0)
    assert client.estimate_usd("claude-sonnet-4-6", 0, 1_000_000) == pytest.approx(15.0)
    assert client.estimate_usd(
        "claude-opus-4-8", 0, 0, cache_read_tokens=1_000_000
    ) == pytest.approx(0.5)
    assert client.estimate_usd(
        "claude-opus-4-8", 0, 0, cache_write_tokens=1_000_000
    ) == pytest.approx(6.25)
    assert client.estimate_usd("unknown-model", 1000, 1000) == 0


def test_ledger_tracks_person_and_feature(db):
    tutor = client.Caller(user="person:1", feature="tutor", role="kid")
    client.record_spend(db, tutor, 0.10)
    client.record_spend(db, client.Caller(user="person:1", feature="quiz", role="kid"), 0.05)
    entry = client.ledger_for_day(db)["person:1"]
    assert entry["requests"] == 2
    assert entry["usd"] == pytest.approx(0.15)
    assert entry["features"] == pytest.approx({"tutor": 0.10, "quiz": 0.05})


def test_kid_limit_blocks_only_kids(db, monkeypatch):
    monkeypatch.setattr(settings, "ai_kid_daily_usd", 0.10)
    kid = client.Caller(user="person:1", feature="tutor", role="kid")
    client.record_spend(db, kid, 0.20)
    with pytest.raises(client.BudgetExceeded):
        client._check_personal_limit(db, kid)
    client._check_personal_limit(db, client.Caller(user="person:1", feature="x", role="owner"))


def test_attribution_context_and_headers():
    caller = client.Caller(user="person:2", session="tg:9", feature="tutor", role="kid")
    with client.attribute(caller):
        assert client.current_caller() == caller
    assert client.current_caller() == client.Caller()
    assert client.attribution_headers(caller) == {
        "X-Luna-Client": "homesec",
        "X-Luna-User": "person:2",
        "X-Luna-Session": "tg:9",
        "X-Luna-Feature": "tutor",
    }


def test_split_message_respects_limit():
    text = "\n".join(["абв" * 400] * 10)
    parts = texts.split_message(text, limit=2000)
    assert all(len(part) <= 2000 for part in parts)
    assert "".join(parts).replace("\n", "") == text.replace("\n", "")
    assert texts.split_message("x" * 9000, limit=4000)[0] == "x" * 4000
    assert texts.split_message("short") == ["short"]


def test_escape_markdown():
    assert texts.escape_markdown("a_b*c`d[e") == "a\\_b\\*c\\`d\\[e"


def test_spend_report_names_people():
    rows = [("Миша", 0.25, 12, {"tutor": 0.2, "quiz": 0.05})]
    report = texts.format_spend_by_person(rows, 1.5)
    assert "Миша: $0.25" in report and "репетитор $0.20" in report and "За 7 дней: $1.50" in report


def test_wrong_quiz_answer_spends_daily_attempt(db):
    assert family.can_take_quiz_today(db, 1)
    family.mark_quiz_attempted(db, 1)
    assert not family.can_take_quiz_today(db, 1)
    assert family.can_take_quiz_today(db, 2)


@pytest.mark.parametrize("text", ["я не хочу жить", "МЕНЯ БЬЮТ в школе", "меня травят"])
def test_safety_keywords_flag_messages(text):
    assert family.needs_adult_attention(text)


def test_ordinary_homework_is_not_flagged():
    assert not family.needs_adult_attention("помоги решить задачу про поезда")


def test_the_default_fast_model_is_priced_so_the_kid_budget_counts():
    from app.config import Settings

    fast = Settings(_env_file=None).ai_model_fast
    assert client.estimate_usd(fast, 1_000_000, 1_000_000) == pytest.approx(2.80)
