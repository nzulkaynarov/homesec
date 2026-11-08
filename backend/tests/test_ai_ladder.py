"""Own model first, cheap cloud next, paid models as support."""

from types import SimpleNamespace

import pytest

from app.ai import client, orchestrator, tools
from app.config import settings


def _text(text):
    return SimpleNamespace(
        stop_reason="end_turn", content=[SimpleNamespace(type="text", text=text)]
    )


def _tool(name):
    block = SimpleNamespace(type="tool_use", name=name, input={}, id="t1")
    return SimpleNamespace(stop_reason="tool_use", content=[block])


@pytest.fixture
def ladder(monkeypatch):
    monkeypatch.setattr(settings, "ai_model_local", "local")
    monkeypatch.setattr(settings, "ai_model", "cloud")
    monkeypatch.setattr(settings, "ai_model_support", "support")
    monkeypatch.setattr(orchestrator, "log_event", lambda *a: None)
    monkeypatch.setattr(orchestrator, "_run_read_tool", lambda db, name, args: ("ok", False))


def _script(monkeypatch, replies):
    calls = []

    def fake_ask(db, model, **kwargs):
        calls.append(model)
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(orchestrator.client, "ask", fake_ask)
    return calls


def test_local_answer_after_tool_use_is_accepted(ladder, monkeypatch):
    calls = _script(monkeypatch, [_tool("get_presence"), _text("Дома Миша")])
    assert orchestrator.handle(None, "кто дома").text == "Дома Миша"
    assert calls == ["local", "local"]


def test_open_question_escalates_from_local(ladder, monkeypatch):
    calls = _script(monkeypatch, [_text("плохой ответ"), _text("хороший ответ")])
    assert orchestrator.handle(None, "рецепт").text == "хороший ответ"
    assert calls == ["local", "cloud"]


def test_invented_tool_escalates(ladder, monkeypatch):
    calls = _script(monkeypatch, [_tool("recommend_recipe"), _text("рецепт")])
    assert orchestrator.handle(None, "рецепт").text == "рецепт"
    assert calls == ["local", "cloud"]


def test_error_climbs_to_paid_support(ladder, monkeypatch):
    calls = _script(monkeypatch, [client.AiError("down"), client.AiError("down"), _text("ok")])
    assert orchestrator.handle(None, "что-то").text == "ok"
    assert calls == ["local", "cloud", "support"]


def test_budget_limit_stops_the_ladder(ladder, monkeypatch):
    _script(monkeypatch, [client.BudgetExceeded("limit")])
    with pytest.raises(client.BudgetExceeded):
        orchestrator.handle(None, "что-то")


def test_repeated_call_escalates_instead_of_looping(ladder, monkeypatch):
    calls = _script(monkeypatch, [_tool("get_presence"), _tool("get_presence"), _text("готово")])
    assert orchestrator.handle(None, "кто дома").text == "готово"
    assert calls == ["local", "local", "cloud"]


def test_local_stops_after_proposing_one_action(ladder, monkeypatch):
    monkeypatch.setattr(orchestrator, "_describe_pending", lambda db, name, args: "pause")
    calls = _script(monkeypatch, [_tool("pause_internet")])
    answer = orchestrator.handle(None, "пауза")
    assert [p.tool for p in answer.pending] == ["pause_internet"]
    assert calls == ["local"]


def test_same_action_is_proposed_once_on_last_rung(ladder, monkeypatch):
    monkeypatch.setattr(settings, "ai_model_local", "")
    monkeypatch.setattr(settings, "ai_model_support", "cloud")
    monkeypatch.setattr(orchestrator, "_describe_pending", lambda db, name, args: "pause")
    _script(monkeypatch, [_tool("pause_internet"), _tool("pause_internet"), _text("ждёт кнопку")])
    assert len(orchestrator.handle(None, "пауза").pending) == 1


def test_known_tools_exist_for_ladder_check():
    assert "get_presence" in tools.REGISTRY


def test_ask_with_fallback_tries_next_model(monkeypatch):
    calls = []

    def fake_ask(db, model, **kwargs):
        calls.append(model)
        if model == "a":
            raise client.AiError("down")
        return "reply"

    monkeypatch.setattr(client, "ask", fake_ask)
    assert client.ask_with_fallback(None, ["a", "b"]) == "reply"
    assert calls == ["a", "b"]


def test_model_ladder_drops_blanks_and_repeats():
    assert client.model_ladder("", "a", "a", "b") == ["a", "b"]


def test_claude_options_only_reach_claude():
    system = [{"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}}]
    cloud = client.build_request("claude-haiku-4-5", [], system, None, 10, True, "medium")
    assert cloud["system"] == "sys" and "thinking" not in cloud and "output_config" not in cloud
    claude = client.build_request("claude-sonnet-4-6", [], system, None, 10, True, "medium")
    assert claude["system"] == system and claude["thinking"] == {"type": "adaptive"}
