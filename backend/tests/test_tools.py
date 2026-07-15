"""Тесты реестра инструментов ИИ/бота: схемы, исполнение, guardrails."""

from datetime import datetime, timedelta

import pytest

from app.ai import tools
from app.db import Base, engine, session
from app.models import Device, EventLog, Pause, PendingAction, Person, active_pauses
from app.services.enforcement import _desired_state


@pytest.fixture
def db():
    Base.metadata.create_all(engine)
    s = session()
    for model in (Pause, EventLog, PendingAction, Device, Person):
        s.query(model).delete()
    s.commit()
    yield s
    s.close()


@pytest.fixture
def dev(db):
    kid = Person(name="Миша", role="kid")
    db.add(kid)
    db.commit()
    d = Device(mac="AA:00:00:00:00:01", ip="192.168.88.30", name="Планшет", person_id=kid.id)
    db.add(d)
    db.commit()
    return d


@pytest.fixture
def no_reconcile(monkeypatch):
    """Мутирующие инструменты зовут reconcile — в тестах сеть недоступна."""
    calls = []
    monkeypatch.setattr(tools.enforcement, "reconcile", lambda s: calls.append(1))
    return calls


def test_schemas_for_claude():
    schemas = {s["name"]: s for s in tools.anthropic_schemas()}
    assert {"list_devices", "get_status", "block_device", "pause_internet"} <= set(schemas)
    blk = schemas["block_device"]["input_schema"]
    assert blk["properties"]["device_id"]["type"] == "integer"
    assert "device_id" in blk["required"]
    # у необязательных параметров есть default — они не в required
    assert "limit" not in schemas["get_recent_events"]["input_schema"]["required"]
    assert all(s["description"] for s in schemas.values())
    assert tools.is_mutating("block_device") and not tools.is_mutating("list_devices")


def test_unknown_tool_and_bad_args(db, no_reconcile):
    with pytest.raises(tools.ToolError):
        tools.run_tool(db, "reboot_router", {})
    with pytest.raises(tools.ToolError):
        tools.run_tool(db, "block_device", {"nonsense": 1})


def test_block_unblock_logs_and_reconciles(db, dev, no_reconcile):
    msg = tools.run_tool(db, "block_device", {"device_id": dev.id}, source="ai")
    assert dev.blocked_manual and "Планшет" in msg
    assert no_reconcile == [1]
    events = [e for e in db.query(EventLog) if e.kind == "ai_action"]
    assert events and "Планшет" in events[-1].message

    tools.run_tool(db, "unblock_device", {"device_id": dev.id}, source="bot")
    assert not dev.blocked_manual
    assert any(e.kind == "bot_action" for e in db.query(EventLog))


def test_self_ip_protected(db, dev, no_reconcile, monkeypatch):
    monkeypatch.setattr(tools.enforcement, "get_self_ips", lambda: {"192.168.88.30"})
    with pytest.raises(tools.ToolError, match="малинка"):
        tools.run_tool(db, "block_device", {"device_id": dev.id})
    with pytest.raises(tools.ToolError, match="малинка"):
        tools.run_tool(db, "pause_internet", {"target": "Планшет", "minutes": 10})
    assert not dev.blocked_manual and not active_pauses(db)


def test_pause_group_blocks_in_desired_state(db, dev, no_reconcile):
    msg = tools.run_tool(db, "pause_internet", {"target": "kid", "minutes": 30, "reason": "обед"})
    assert "Дети" in msg
    st = _desired_state(db, [dev])
    assert dev.ip in st["lists"]["hs-blocked"]

    tools.run_tool(db, "resume_internet", {"target": "kid"})
    st = _desired_state(db, [dev])
    assert dev.ip not in st["lists"]["hs-blocked"]


def test_pause_device_by_name_and_validation(db, dev, no_reconcile):
    tools.run_tool(db, "pause_internet", {"target": "планшет", "minutes": 15})
    assert active_pauses(db)[0].target == str(dev.id)
    with pytest.raises(tools.ToolError):
        tools.run_tool(db, "pause_internet", {"target": "kid", "minutes": 0})
    with pytest.raises(tools.ToolError):
        tools.run_tool(db, "pause_internet", {"target": "adult", "minutes": 10})
    with pytest.raises(tools.ToolError):
        tools.run_tool(db, "resume_internet", {"target": "guest"})  # пауз нет


def test_expired_pause_inactive(db, dev):
    db.add(Pause(target_type="group", target="kid", until=datetime.now() - timedelta(minutes=1)))
    db.commit()
    assert not active_pauses(db)
    assert dev.ip not in _desired_state(db, [dev])["lists"]["hs-blocked"]


def test_speed_limit_validation(db, dev, no_reconcile):
    tools.run_tool(db, "set_speed_limit", {"device_id": dev.id, "limit": "5M/20M"})
    assert dev.speed_limit == "5M/20M"
    with pytest.raises(tools.ToolError):
        tools.run_tool(db, "set_speed_limit", {"device_id": dev.id, "limit": "быстро"})
    tools.run_tool(db, "set_speed_limit", {"device_id": dev.id, "limit": ""})
    assert dev.speed_limit == ""


def test_assign_device(db, dev, no_reconcile):
    db.add(Person(name="Папа", role="adult"))
    db.commit()
    msg = tools.run_tool(db, "assign_device", {"device_id": dev.id, "person_name": "Папа"})
    assert "Взрослые" in msg and dev.group == "adult"
    with pytest.raises(tools.ToolError, match="Папа"):
        tools.run_tool(db, "assign_device", {"device_id": dev.id, "person_name": "Дядя"})
    tools.run_tool(db, "assign_device", {"device_id": dev.id, "person_name": ""})
    assert dev.group == "unknown"


def test_pending_actions_persist(db):
    """Кнопки подтверждения ИИ-мутаций переживают рестарт бота: состояние в базе."""
    pid = tools.save_pending(db, "block_device", {"device_id": 5}, "Блокировка Планшета")
    assert tools.pop_pending(db, pid) == (
        "block_device", {"device_id": 5}, "Блокировка Планшета"
    )
    assert tools.pop_pending(db, pid) is None  # одноразово: повторный тап — «устарело»
    assert tools.pop_pending(db, 999) is None


def test_pending_actions_cleanup_after_ttl(db):
    """Ежедневная уборка планировщика выкидывает неподтверждённое старше суток."""
    from app.scheduler import _cleanup

    old_id = tools.save_pending(db, "block_device", {}, "старое")
    fresh_id = tools.save_pending(db, "block_device", {}, "свежее")
    db.get(PendingAction, old_id).created = datetime.now() - timedelta(hours=25)
    db.commit()
    _cleanup()  # своя сессия — сбрасываем кэш текущей
    db.expire_all()
    assert tools.pop_pending(db, old_id) is None
    assert tools.pop_pending(db, fresh_id) is not None


def test_find_device(db, dev):
    db.add(Device(mac="AA:00:00:00:00:02", ip="192.168.88.31", name="Планшет старый"))
    db.commit()
    assert tools.find_device(db, str(dev.id)) is dev
    assert tools.find_device(db, "aa:00:00:00:00:01") is dev
    assert tools.find_device(db, "Планшет") is dev  # точное имя выигрывает у частичного
    assert tools.find_device(db, "планш") is None  # неоднозначно
    assert tools.find_device(db, "старый") is not None
    assert tools.find_device(db, "нет такого") is None


def test_find_device_candidates(db, dev):
    db.add(Device(mac="AA:00:00:00:00:02", ip="192.168.88.31", name="Планшет старый"))
    db.commit()
    # неоднозначный запрос, на котором find_device возвращает None
    assert tools.find_device(db, "планш") is None
    names = [d.name for d in tools.find_device_candidates(db, "планш")]
    assert names == ["Планшет", "Планшет старый"]  # сортировка по имени
    # пустой запрос — все устройства (для команды без аргумента)
    assert len(tools.find_device_candidates(db, "")) == 2
    assert tools.find_device_candidates(db, "нет такого") == []


def test_list_devices_and_events(db, dev, no_reconcile):
    tools.run_tool(db, "pause_internet", {"target": "kid", "minutes": 30})
    rows = tools.run_tool(db, "list_devices", {})
    assert rows[0]["name"] == "Планшет" and rows[0]["group"] == "kid"
    assert rows[0]["paused_until"]  # пауза группы видна на устройстве
    events = tools.run_tool(db, "get_recent_events", {"limit": 5})
    assert events and events[0]["kind"] == "ai_action"
