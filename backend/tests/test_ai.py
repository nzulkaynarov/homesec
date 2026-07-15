"""ИИ-слой с замоканным Claude API: бюджет, оркестратор с подтверждением
мутаций, эвристики watchdog, деградация дайджеста без ключа."""

import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.ai import analyst, client, orchestrator, watchdog
from app.config import settings
from app.db import Base, engine, session
from app.models import Device, EventLog, KVState, Pause, Person, QuotaUsage, kv_set


@pytest.fixture
def db():
    Base.metadata.create_all(engine)
    s = session()
    for model in (Pause, EventLog, KVState, QuotaUsage, Device, Person):
        s.query(model).delete()
    s.commit()
    yield s
    s.close()


@pytest.fixture
def kid_device(db):
    kid = Person(name="Миша", role="kid")
    db.add(kid)
    db.commit()
    d = Device(mac="AA:00:00:00:00:01", ip="192.168.88.30", name="Планшет", person_id=kid.id)
    db.add(d)
    db.commit()
    return d


def _block(**kw):
    return SimpleNamespace(**kw)


def _message(blocks, stop_reason="end_turn", input_tokens=100, output_tokens=50):
    return SimpleNamespace(
        content=blocks,
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
    )


# ---------- client: конфигурация и бюджет ----------

def test_ask_requires_api_key(db):
    assert not client.is_configured()
    with pytest.raises(client.AiError):
        client.ask(db, messages=[{"role": "user", "content": "hi"}])


def test_budget_guard(db, monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-test")
    monkeypatch.setattr(settings, "ai_daily_token_budget", 1000)
    assert client.budget_left(db) == 1000

    kv_set(db, client._usage_key(), json.dumps({"input": 900, "output": 200, "requests": 3}))
    assert client.budget_left(db) == -100
    with pytest.raises(client.BudgetExceeded):
        client.ask(db, messages=[{"role": "user", "content": "hi"}])

    monkeypatch.setattr(settings, "ai_daily_token_budget", 0)
    assert client.budget_left(db) is None  # 0 = лимит выключен


def test_usage_recording(db):
    client._record_usage(db, 100, 50)
    client._record_usage(db, 10, 5)
    usage = client.usage_today(db)
    assert usage == {"input": 110, "output": 55, "requests": 2}


# ---------- orchestrator: read сразу, мутации на подтверждение ----------

def test_orchestrator_reads_then_defers_mutation(db, kid_device, monkeypatch):
    calls = []

    def fake_ask(db_, messages, **kw):
        calls.append([m for m in messages])
        if len(calls) == 1:  # модель хочет список устройств
            return _message(
                [_block(type="tool_use", id="t1", name="list_devices", input={})],
                stop_reason="tool_use",
            )
        if len(calls) == 2:  # затем блокировку — она должна отложиться
            return _message(
                [_block(type="tool_use", id="t2", name="block_device",
                        input={"device_id": kid_device.id})],
                stop_reason="tool_use",
            )
        return _message([_block(type="text", text="Предложил блокировку, жду кнопку.")])

    monkeypatch.setattr(orchestrator.client, "ask", fake_ask)
    answer = orchestrator.handle(db, "заблокируй планшет")

    assert "жду кнопку" in answer.text
    assert len(answer.pending) == 1
    assert answer.pending[0].tool == "block_device"
    assert answer.pending[0].args == {"device_id": kid_device.id}
    assert not kid_device.blocked_manual  # НЕ выполнено без подтверждения
    assert any(e.kind == "ai_proposed" for e in db.query(EventLog))
    # список устройств исполнился и вернулся модели вторым запросом
    tool_result = calls[2][-1]["content"][0]
    assert tool_result["type"] == "tool_result"
    assert "подтверждени" in tool_result["content"]


def test_orchestrator_tool_error_reported_to_model(db, monkeypatch):
    def fake_ask(db_, messages, **kw):
        if len(messages) == 1:
            return _message(
                [_block(type="tool_use", id="t1", name="get_device_activity",
                        input={"device_id": 999})],
                stop_reason="tool_use",
            )
        # модель получила is_error и отвечает текстом
        assert messages[-1]["content"][0]["is_error"] is True
        return _message([_block(type="text", text="Такого устройства нет.")])

    monkeypatch.setattr(orchestrator.client, "ask", fake_ask)
    answer = orchestrator.handle(db, "что смотрел телевизор?")
    assert answer.text == "Такого устройства нет."
    assert answer.pending == []


# ---------- watchdog: эвристики без LLM ----------

def _querylog(entries):
    return [{"client": ip, "question": {"name": domain}} for ip, domain in entries]


def test_watchdog_night_activity(db, kid_device, monkeypatch):
    night = datetime(2026, 7, 14, 2, 30)
    monkeypatch.setattr(
        watchdog.adguard, "get_query_log",
        lambda limit=500: _querylog([("192.168.88.30", f"site{i}.com") for i in range(20)]),
    )
    alerts = watchdog.find_anomalies(db, now=night)
    assert len(alerts) == 1 and "Ночная активность" in alerts[0] and "Планшет" in alerts[0]
    # повтор в ту же ночь заглушен
    assert watchdog.find_anomalies(db, now=night) == []


def test_watchdog_doh_spike_and_quiet_day(db, kid_device, monkeypatch):
    day = datetime(2026, 7, 14, 15, 0)
    monkeypatch.setattr(
        watchdog.adguard, "get_query_log",
        lambda limit=500: _querylog([("192.168.88.30", "dns.google")] * 12),
    )
    alerts = watchdog.find_anomalies(db, now=day)
    assert len(alerts) == 1 and "обхода" in alerts[0]

    monkeypatch.setattr(
        watchdog.adguard, "get_query_log",
        lambda limit=500: _querylog([("192.168.88.30", "youtube.com")] * 12),
    )
    assert watchdog.find_anomalies(db, now=day) == []  # обычный трафик днём — тишина


def test_watchdog_alert_plain_without_key(db):
    assert not client.is_configured()
    assert watchdog.format_alert(db, "тест").startswith("🕵️")


# ---------- analyst: деградация без ключа ----------

def test_digest_fallback_without_key(db, kid_device, monkeypatch):
    monkeypatch.setattr(analyst.adguard, "get_stats",
                        lambda: {"num_dns_queries": 500, "num_blocked_filtering": 42})
    monkeypatch.setattr(analyst.adguard, "get_query_log", lambda limit=1000: [])
    from app.models import log_event

    log_event(db, "device_new", "Новое устройство: tv (AA:..., 192.168.88.77)")
    text = analyst.daily_digest(db)
    assert "500" in text and "Новое устройство" in text


def test_digest_data_collects_kid_domains(db, kid_device, monkeypatch):
    monkeypatch.setattr(analyst.adguard, "get_stats", lambda: {})
    monkeypatch.setattr(
        analyst.adguard, "get_query_log",
        lambda limit=1000: _querylog(
            [("192.168.88.30", "youtube.com")] * 3 + [("192.168.88.30", "roblox.com")]
        ),
    )
    data = analyst.collect_digest_data(db)
    assert data["kid_top_domains"]["Планшет"][0] == "youtube.com"


def test_digest_data_and_fallback_include_screen_time(db, kid_device, monkeypatch):
    """«Экранное время за день» из ТЗ фазы 2: активные минуты из QuotaUsage."""
    monkeypatch.setattr(analyst.adguard, "get_stats", lambda: {})
    monkeypatch.setattr(analyst.adguard, "get_query_log", lambda limit=1000: [])
    today = datetime.now().strftime("%Y-%m-%d")
    db.add(QuotaUsage(device_id=kid_device.id, date=today, category="games", minutes=45))
    db.add(QuotaUsage(device_id=kid_device.id, date=today, category="internet", minutes=90))
    # вчерашние минуты в «сегодня» не попадают
    db.add(QuotaUsage(device_id=kid_device.id, date="2020-01-01", category="games",
                      minutes=999))
    db.commit()

    data = analyst.collect_digest_data(db)
    assert data["screen_time"] == {
        "Планшет": {"Игры": 45, "Интернет целиком": 90}
    }
    assert not client.is_configured()
    text = analyst.daily_digest(db)  # деградация без ключа — строка всё равно есть
    assert "⏳ Планшет" in text and "45 мин" in text
