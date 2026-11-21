"""Every message a parent reads speaks plain Russian: no MAC or IP addresses, no ids or request
numbers, no settings names, no raw arguments and no English codes. The journal keeps its machine
fields for the bot's linkage; only what people read is rendered without them."""

import asyncio
import re
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import anthropic
import httpx
import pytest

from app.ai import analyst, client, confirmations, orchestrator, tools, watchdog
from app.bot import health, texts
from app.bot import main as bot_main
from app.config import settings
from app.db import Base, engine, session
from app.models import (
    Device,
    EventLog,
    KVState,
    Pause,
    Person,
    QuotaUsage,
    RegistrationRequest,
    log_event,
)
from app.services import enforcement, plain, uptime

MAC = "00:1B:63:84:45:E6"
RANDOM_MAC = "DA:A1:19:00:00:01"
TECHNICAL = re.compile(
    r"(?:[0-9A-F]{2}[:-]){5}[0-9A-F]{2}"  # MAC
    r"|\b\d{1,3}(?:\.\d{1,3}){3}\b"  # IP
    r"|\bHS_[A-Z_]+|\bid=|req#|\b[a-z_]+="  # ids, settings and raw arguments
    r"|\bMAC\b|\bIP\b|\bDNS\b|\bDoH\b|MikroTik|AdGuard|Claude API|[Тт]окен"
    r"|\b(?:kid|kids|school|weekend|internet|games|video|social|personal|shared|iot|infra)\b",
    re.IGNORECASE,
)


def assert_plain(text: str) -> None:
    found = TECHNICAL.search(text)
    assert found is None, f"«{found.group(0) if found else ''}» in: {text}"


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr(enforcement, "get_self_ips", lambda: {"192.168.88.2"})
    Base.metadata.create_all(engine)
    s = session()
    for model in (Pause, EventLog, KVState, QuotaUsage, RegistrationRequest, Device, Person):
        s.query(model).delete()
    s.commit()
    yield s
    s.close()


@pytest.fixture
def misha(db):
    kid = Person(name="Миша", role="kid")
    db.add(kid)
    db.commit()
    return kid


def _device(db, mac=MAC, name="iPhone-Misha", hostname="iPhone-Misha", **kwargs):
    device = Device(mac=mac, ip="192.168.88.45", name=name, hostname=hostname, **kwargs)
    db.add(device)
    db.commit()
    return device


# ---------- device notices ----------


def test_new_device_leads_with_what_it_probably_is():
    text = texts.format_new_device(
        {"name": "iPhone-Misha", "hostname": "iPhone-Misha", "mac": MAC, "random_mac": False},
        "Apple, Inc.",
    )
    assert text.startswith("🆕 В сети новое устройство\n«iPhone-Misha»\nПроизводитель: Apple")
    assert text.endswith("Кому оно принадлежит?")
    assert_plain(text)


def test_an_unnamed_device_with_a_changing_address_is_described_in_words():
    text = texts.format_new_device(
        {"name": RANDOM_MAC, "hostname": "", "mac": RANDOM_MAC, "random_mac": True}
    )
    assert "Без имени" in text and "Частный адрес Wi-Fi" in text
    assert "Производитель" not in text
    assert_plain(text)


def test_registration_says_who_asked_and_for_which_device():
    text = texts.format_registration(
        {
            "name": "Galaxy-Tab",
            "hostname": "Galaxy-Tab",
            "mac": MAC,
            "random_mac": False,
            "request_name": "Бабушка",
            "request_comment": "мой планшет",
        },
        "Samsung Electronics",
    )
    assert "Представился: Бабушка" in text and "«мой планшет»" in text
    assert "«Galaxy-Tab»" in text and "Samsung" in text
    assert_plain(text)


def test_the_loop_renders_every_device_notice_without_addresses(db, misha, monkeypatch):
    from app.bot import notify

    assert notify.collect_notifications(db) == []  # cursor starts at the end of the journal
    phone = _device(db, person_id=misha.id)
    unnamed = _device(db, mac=RANDOM_MAC, name=RANDOM_MAC, hostname="")
    rotated = _device(db, mac="DA:A1:19:00:00:02", name="iPhone-Misha")
    log_event(db, "device_new", f"Новое устройство: {unnamed.name} ({unnamed.mac}, 192.168.88.9)")
    db.add(RegistrationRequest(device_id=phone.id, name="Бабушка", comment="мой телефон"))
    db.commit()
    log_event(
        db,
        "register_request",
        f"Заявка на регистрацию: {phone.name} ({phone.mac}, {phone.ip}) — владелец: Бабушка",
    )
    log_event(
        db,
        "device_maybe_same",
        f"Похоже, «{rotated.name}» ({rotated.mac}) — это снова «{phone.name}» ({phone.mac}): "
        "совпадает имя хоста.",
    )
    monkeypatch.setattr(bot_main, "vendor_of", AsyncMock(return_value="Apple, Inc."))
    monkeypatch.setattr(bot_main, "_parent_ids", lambda: set())
    send = AsyncMock()

    async def one_round():
        async def stop(_seconds):
            raise asyncio.CancelledError

        monkeypatch.setattr(bot_main.asyncio, "sleep", stop)
        try:
            await bot_main.notify_loop(MagicMock(send_message=send), {1})
        except asyncio.CancelledError:
            pass

    asyncio.run(one_round())
    sent = [call.args[1] for call in send.await_args_list]
    assert len(sent) == 3
    new, registration, same = sent
    assert "Без имени" in new
    assert "Представился: Бабушка" in registration and "Apple" in registration
    assert "«iPhone-Misha» — это снова «iPhone-Misha»" in same and "Объединить" in same
    for text in sent:
        assert_plain(text)


def test_a_watched_device_is_named_without_its_address(db, monkeypatch):
    now = datetime(2026, 11, 21, 12, 30)
    monkeypatch.setattr(uptime.enforcement, "enforcement_status", lambda db, now: (True, ""))
    seen = now - timedelta(hours=1)
    _device(db, name="Камера", hostname="", kind="iot", watched=True, last_seen=seen)
    [change] = uptime.changes(db, now)
    assert "«Камера»" in change.text
    assert_plain(change.text)


# ---------- health ----------


def test_health_alerts_say_what_it_means_and_what_to_do():
    monitor = health.HealthMonitor(
        checks={name: (lambda: False) for name in health.FAILURE_TEXTS},
        fail_after=1,
        failure_texts=health.FAILURE_TEXTS,
        recovery_texts=health.RECOVERY_TEXTS,
    )
    messages = monitor.tick(datetime(2026, 11, 21, 14, 32))
    assert len(messages) == len(health.FAILURE_TEXTS)
    router = next(m for m in messages if m.startswith("🔴 Роутер"))
    assert "с 14:32" in router and "из розетки" in router
    for message in messages:
        assert "14:32" in message and "проверок" not in message
        assert_plain(message)
    monitor.checks = {name: (lambda: True) for name in health.FAILURE_TEXTS}
    monitor.ok_after = 1
    back = monitor.tick()
    assert "✅ Роутер снова работает." in back
    for message in back:
        assert_plain(message)


@pytest.mark.parametrize(
    ("causes", "expected"),
    [
        (["mikrotik"], "роутер не отвечает"),
        (["adguard"], "фильтр сайтов"),
        (["unexpected"], "журнал"),
        ([], "журнал"),
    ],
)
def test_stopped_rules_name_the_reason_in_words(db, causes, expected):
    errors = ["RouterOS 192.168.88.1: connection refused (HS_MIKROTIK_HOST)"] if causes else []
    enforcement._record_outcome(db, {"ok": not causes, "errors": errors, "causes": causes})
    detail = health.enforcement_detail()
    assert expected in detail
    assert_plain(detail)


def test_a_success_forgets_the_last_reason(db):
    enforcement._record_outcome(db, {"ok": False, "errors": ["x"], "causes": ["adguard"]})
    assert enforcement.last_causes(db) == ["adguard"]
    enforcement._record_outcome(db, {"ok": True, "errors": [], "causes": []})
    assert enforcement.last_causes(db) == []


# ---------- the assistant ----------


def test_ai_errors_read_as_unavailable_not_as_settings(db, monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    with pytest.raises(client.AiError) as missing:
        client.ask(db, messages=[{"role": "user", "content": "hi"}])
    assert str(missing.value) == client.UNAVAILABLE

    monkeypatch.setattr(settings, "anthropic_api_key", "sk-test")
    monkeypatch.setattr(settings, "ai_daily_token_budget", 10)
    client._record_usage(db, 20, 0)
    with pytest.raises(client.BudgetExceeded) as spent:
        client.ask(db, messages=[{"role": "user", "content": "hi"}])
    assert_plain(str(spent.value))
    monkeypatch.setattr(settings, "ai_daily_token_budget", 0)

    request = httpx.Request("POST", "http://gateway/v1/messages")
    for error in (
        anthropic.APIStatusError(
            "bad gateway", response=httpx.Response(502, request=request), body=None
        ),
        anthropic.APIConnectionError(request=request),
    ):

        def create(error=error, **kwargs):
            raise error

        fake = SimpleNamespace(messages=SimpleNamespace(create=create))
        monkeypatch.setattr(anthropic, "Anthropic", lambda fake=fake, **kwargs: fake)
        with pytest.raises(client.AiError) as failed:
            client.ask(db, messages=[{"role": "user", "content": "hi"}])
        assert str(failed.value) == client.UNAVAILABLE
        assert_plain(str(failed.value))


# Plausible calls of every action the assistant can propose.
PROPOSALS = {
    "block_device": {"device_id": "{phone}"},
    "unblock_device": {"device_id": "{phone}"},
    "set_speed_limit": {"device_id": "{phone}", "limit": "5M/20M"},
    "pause_internet": {"target": "kid", "until": "09:00", "reason": "уроки"},
    "set_bedtime": {"who": "kid", "nights": "school", "start": "21:30", "end": "07:00"},
    "resume_internet": {"target": "Миша"},
    "set_quota": {"target": "kid", "category": "games", "minutes_per_day": 90, "days": "0,1,2,3,4"},
    "add_bonus_time": {"target": "Миша", "minutes": 30, "category": "video"},
    "merge_devices": {"duplicate_id": "{gone}", "target_id": "{phone}"},
    "assign_device": {"device_id": "{phone}", "person_name": "Миша"},
    "set_device_kind": {"device_id": "{phone}", "kind": "shared"},
    "set_movie_mode": {"hours": 3},
    "save_family_note": {"content": "код домофона 4589"},
}


def test_every_proposed_action_is_confirmed_in_plain_words(db, misha):
    mutating = {name for name, spec in tools.REGISTRY.items() if spec.mutating}
    assert mutating == set(PROPOSALS) == set(confirmations.DESCRIBERS)
    phone = _device(db, person_id=misha.id)
    ids = {"{phone}": phone.id, "{gone}": phone.id + 100}
    now = datetime(2026, 11, 21, 20, 0)
    for name, raw in PROPOSALS.items():
        args = {key: ids.get(value, value) for key, value in raw.items()}
        text = confirmations.describe(db, name, args, now)
        assert_plain(text)
        assert str(ids["{gone}"]) not in text
    pause = confirmations.describe(db, "pause_internet", PROPOSALS["pause_internet"], now)
    assert pause.splitlines() == [
        "⏸ Выключить интернет на время",
        "Кому: все дети",
        "Когда включится: до 09:00",
        "Причина: уроки",
    ]
    bedtime = confirmations.describe(db, "set_bedtime", PROPOSALS["set_bedtime"], now)
    assert "Когда: ночи вс–чт" in bedtime and "Выключать в 21:30, включать в 07:00" in bedtime
    quota = confirmations.describe(db, "set_quota", PROPOSALS["set_quota"], now)
    assert "«Игры»" in quota and "1 ч 30 мин в день" in quota and "по будням" in quota
    speed = confirmations.describe(
        db, "set_speed_limit", {"device_id": phone.id, "limit": "5M/20M"}
    )
    assert "«iPhone-Misha» (Миша)" in speed and "до 20 Мбит/с на загрузку" in speed


def test_a_call_that_cannot_run_goes_back_to_the_model_not_to_a_parent(db, monkeypatch):
    calls = []

    def fake_ask(db_, messages, **kw):
        calls.append(messages)
        if len(calls) == 1:
            block = SimpleNamespace(
                type="tool_use", id="t1", name="pause_internet", input={"minutes": 30}
            )
            return SimpleNamespace(
                content=[block],
                stop_reason="tool_use",
                usage=SimpleNamespace(input_tokens=1, output_tokens=1),
            )
        result = messages[-1]["content"][0]
        assert result["is_error"] is True and "target" in result["content"]
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="Кому поставить паузу?")],
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        )

    monkeypatch.setattr(orchestrator.client, "ask", fake_ask)
    answer = orchestrator.handle(db, "пауза на полчаса")
    assert answer.pending == [] and answer.text == "Кому поставить паузу?"


def test_tool_errors_and_results_name_devices_not_addresses(db, misha):
    phone = _device(db, person_id=misha.id)
    with pytest.raises(tools.ToolError) as gone:
        tools.run_tool(
            db, "block_device", {"device_id": phone.id + 100}, source="bot", reconcile_after=False
        )
    with pytest.raises(tools.ToolError) as wrong:
        tools.run_tool(
            db, "block_device", {"device": "планшет"}, source="bot", reconcile_after=False
        )
    assert str(wrong.value) == tools.NOT_UNDERSTOOD
    with pytest.raises(tools.ToolError) as unknown:
        tools.run_tool(db, "format_router", {}, source="bot", reconcile_after=False)
    with pytest.raises(tools.ToolError) as nights:
        tools.run_tool(
            db,
            "set_bedtime",
            {"who": "kid", "nights": "weekdays", "start": "21:00", "end": "07:00"},
            source="bot",
            reconcile_after=False,
        )
    for error in (gone, wrong, unknown, nights):
        assert_plain(str(error.value))
    results = [
        tools.run_tool(db, "block_device", {"device_id": phone.id}, "bot", False),
        tools.run_tool(db, "unblock_device", {"device_id": phone.id}, "bot", False),
        tools.run_tool(
            db, "set_speed_limit", {"device_id": phone.id, "limit": "10M/10M"}, "bot", False
        ),
    ]
    assert results[2] == "Скорость «iPhone-Misha»: до 10 Мбит/с"
    for result in results:
        assert "iPhone-Misha" in result
        assert_plain(result)


def test_the_journal_reaches_the_assistant_without_machine_fields(db):
    log_event(
        db, "bonus_request", f"Миша просит ещё времени: Планшет ({MAC}, 192.168.88.7) [req#7]"
    )
    [event] = tools.get_recent_events(db, limit=1)
    assert event["message"] == "Миша просит ещё времени: Планшет"


# ---------- alerts written by code ----------


def test_watchdog_alerts_name_the_device_and_its_owner(db, misha, monkeypatch):
    _device(db, name="Планшет", hostname="", person_id=misha.id)
    day = datetime(2026, 11, 21, 15, 0)
    stamp = (day - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S.000000")
    entries = [{"client": "192.168.88.45", "question": {"name": "dns.google"}, "time": stamp}]
    monkeypatch.setattr(watchdog.adguard, "get_query_log", lambda limit=500: entries * 12)
    [anomaly] = watchdog.find_anomalies(db, now=day)
    monkeypatch.setattr(client, "is_configured", lambda: False)
    alert = watchdog.format_alert(db, anomaly)
    assert alert.endswith("Устройство: «Планшет» (Миша)") and "ребёнка" in alert
    assert_plain(alert)


def test_the_evening_summary_shows_names_not_addresses(db, monkeypatch):
    monkeypatch.setattr(
        analyst.adguard, "get_stats", lambda: {"num_dns_queries": 500, "num_blocked_filtering": 42}
    )
    monkeypatch.setattr(analyst.adguard, "get_query_log", lambda limit=1000: [])
    log_event(db, "device_new", f"Новое устройство: tv ({MAC}, 192.168.88.77) ⚠️ случайный MAC")
    log_event(db, "block", "Ручная блокировка: Планшет (192.168.88.30)")
    monkeypatch.setattr(client, "is_configured", lambda: False)
    text = analyst.daily_digest(db)
    assert "🆕 Новое устройство: tv" in text and "⛔ Ручная блокировка: Планшет" in text
    assert_plain(text)


def test_plain_speed_and_names():
    assert plain.speed_words("512K/1.5M") == "до 1,5 Мбит/с на загрузку и до 512 кбит/с на отдачу"
    assert plain.speed_words("") == "без ограничения"
    assert plain.device_name(MAC) == plain.UNNAMED
    assert plain.device_name(MAC, "Galaxy-Tab") == "Galaxy-Tab"
