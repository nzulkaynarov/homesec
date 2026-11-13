"""Обёртка над Claude API: единая точка вызова модели с дневным бюджетом
токенов. Все ИИ-агенты (orchestrator, analyst, watchdog) ходят через ask() —
поэтому лимит расхода и учёт в одном месте. Расход пишется в kv_state по дням
и виден в журнале не хуже других событий.

Без ключа (HS_ANTHROPIC_API_KEY пуст) ИИ-фичи молчат: is_configured() = False,
вызов ask() кидает AiError — вызывающие обязаны деградировать вежливо."""

import base64
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime

import httpx
from sqlalchemy.orm import Session

from ..config import settings
from ..models import kv_get, kv_set

log = logging.getLogger("homesec.ai")

USAGE_KEY_PREFIX = "ai_usage:"  # ai_usage:2026-07-14 -> {"input":..,"output":..,"requests":..}
LEDGER_KEY_PREFIX = (
    "ai_ledger:"  # ai_ledger:2026-07-14 -> {caller: {"usd":..,"requests":..,"features":{..}}}
)

# USD per 1M tokens as (input, output). Keys match settings model names by substring.
# Matched as a substring of the gateway model name; luna-fast is Gemini Flash Lite.
MODEL_PRICES = {
    "luna-fast": (0.30, 2.50),
    "opus": (5.0, 25.0),
    "sonnet": (3.0, 15.0),
    "haiku": (0.30, 2.50),
}
LOCAL_MODEL_TIMEOUT_SECONDS = 60.0
CACHE_READ_MULTIPLIER = 0.1
CACHE_WRITE_MULTIPLIER = 1.25


class AiError(Exception):
    """Ошибка ИИ-слоя; текст безопасен для показа пользователю."""


class BudgetExceeded(AiError):
    pass


@dataclass(frozen=True)
class Caller:
    """Who a model call is made for, used for cost attribution and per-person limits."""

    user: str = "system"
    session: str = "system"
    feature: str = "unknown"
    role: str = "system"


_current_caller: ContextVar[Caller | None] = ContextVar("homesec_ai_caller", default=None)


def current_caller() -> Caller:
    return _current_caller.get() or Caller()


@contextmanager
def attribute(caller: Caller) -> Iterator[None]:
    """Attribute model calls made inside this block, including asyncio.to_thread workers."""
    token = _current_caller.set(caller)
    try:
        yield
    finally:
        _current_caller.reset(token)


def attribution_headers(caller: Caller) -> dict[str, str]:
    """Headers the LunaGPT gateway uses to attribute spend in Langfuse."""
    return {
        "X-Luna-Client": "homesec",
        "X-Luna-User": caller.user,
        "X-Luna-Session": caller.session,
        "X-Luna-Feature": caller.feature,
    }


def estimate_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    """Estimate request cost from list prices; unknown models count as free."""
    prices = next((p for name, p in MODEL_PRICES.items() if name in model), (0.0, 0.0))
    input_cost = (
        input_tokens
        + cache_read_tokens * CACHE_READ_MULTIPLIER
        + cache_write_tokens * CACHE_WRITE_MULTIPLIER
    ) * prices[0]
    return (input_cost + output_tokens * prices[1]) / 1_000_000


STT_TIMEOUT_SECONDS = 35.0
STT_INSTRUCTION = (
    "Transcribe this audio recording accurately in its spoken language. "
    "Return ONLY the verbatim transcribed text without any timestamps, explanation, or quotes."
)


async def transcribe(audio: bytes, audio_format: str, caller: Caller, language: str = "ru") -> str:
    """Speech to text through the LunaGPT gateway with HomeSec's own key and budget."""
    if not is_configured():
        raise AiError("Распознавание голоса не настроено: нужен шлюз LunaGPT (HS_AI_BASE_URL).")
    payload = {
        "model": settings.ai_model_stt,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "input_audio",
                 "input_audio": {"data": base64.b64encode(audio).decode(), "format": audio_format}},
                {"type": "text", "text": f"{STT_INSTRUCTION} The spoken language is {language}."},
            ],
        }],
        "temperature": 0.0,
        # A text-only fallback model would invent words instead of hearing them.
        "disable_fallbacks": True,
        "metadata": {
            "user_id": caller.user,
            "session_id": caller.session,
            "tags": ["client:homesec", "feature:stt", f"role:{caller.role}"],
        },
    }
    headers = {
        **attribution_headers(caller),
        "Authorization": f"Bearer {settings.anthropic_api_key}",
    }
    url = f"{settings.ai_base_url.rstrip('/')}/v1/chat/completions"
    async with httpx.AsyncClient(timeout=STT_TIMEOUT_SECONDS) as client:
        response = await client.post(url, json=payload, headers=headers)
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
    return str(content or "").strip()


def is_configured() -> bool:
    return bool(settings.anthropic_api_key)


def _usage_key(now: datetime | None = None) -> str:
    return USAGE_KEY_PREFIX + (now or datetime.now()).strftime("%Y-%m-%d")


def usage_today(db: Session) -> dict:
    raw = kv_get(db, _usage_key(), "")
    if not raw:
        return {"input": 0, "output": 0, "requests": 0}
    try:
        return json.loads(raw)
    except ValueError:
        return {"input": 0, "output": 0, "requests": 0}


def _record_usage(db: Session, input_tokens: int, output_tokens: int) -> None:
    usage = usage_today(db)
    usage["input"] += input_tokens
    usage["output"] += output_tokens
    usage["requests"] += 1
    kv_set(db, _usage_key(), json.dumps(usage))


def _ledger_key(now: datetime | None = None) -> str:
    return LEDGER_KEY_PREFIX + (now or datetime.now()).strftime("%Y-%m-%d")


def ledger_for_day(db: Session, now: datetime | None = None) -> dict:
    """Per-caller spend for one day."""
    raw = kv_get(db, _ledger_key(now), "")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        return {}


def record_spend(db: Session, caller: Caller, usd: float) -> None:
    """Add one request to the per-caller, per-feature ledger."""
    ledger = ledger_for_day(db)
    entry = ledger.setdefault(caller.user, {"usd": 0.0, "requests": 0, "features": {}})
    entry["usd"] += usd
    entry["requests"] += 1
    entry["features"][caller.feature] = entry["features"].get(caller.feature, 0.0) + usd
    kv_set(db, _ledger_key(), json.dumps(ledger))


def spent_today_usd(db: Session, user: str) -> float:
    return float(ledger_for_day(db).get(user, {}).get("usd", 0.0))


def _check_personal_limit(db: Session, caller: Caller) -> None:
    if caller.role != "kid" or settings.ai_kid_daily_usd <= 0:
        return
    if spent_today_usd(db, caller.user) >= settings.ai_kid_daily_usd:
        raise BudgetExceeded("На сегодня твой лимит ИИ закончился. Приходи завтра! 🌙")


def budget_left(db: Session) -> int | None:
    """Сколько токенов осталось на сегодня; None = лимит не задан."""
    if not settings.ai_daily_token_budget:
        return None
    usage = usage_today(db)
    return settings.ai_daily_token_budget - usage["input"] - usage["output"]


def is_local_model(model: str) -> bool:
    return bool(settings.ai_model_local) and model == settings.ai_model_local


def _is_claude_reasoning_model(model: str) -> bool:
    return "opus" in model or "sonnet" in model


def build_request(
    model: str,
    messages: list[dict],
    system: str | list[dict],
    tools: list[dict] | None,
    max_tokens: int,
    thinking: bool,
    effort: str | None,
) -> dict:
    """Build request arguments, keeping Claude-only options away from other models."""
    claude = _is_claude_reasoning_model(model)
    kwargs: dict = {"model": model, "max_tokens": max_tokens, "messages": messages}
    if system:
        if isinstance(system, list) and not claude:
            kwargs["system"] = "\n\n".join(block["text"] for block in system)
        else:
            kwargs["system"] = system
    if tools:
        kwargs["tools"] = tools
    if thinking and claude:
        kwargs["thinking"] = {"type": "adaptive"}
    if effort and claude:
        kwargs["output_config"] = {"effort": effort}
    return kwargs


def model_ladder(*models: str) -> list[str]:
    """Models in the order to try, without blanks or repeats."""
    return list(dict.fromkeys(model for model in models if model))


def ask_with_fallback(db: Session, models: list[str], **kwargs):
    """Try each model in order; a spending limit stops the ladder, other errors climb it."""
    last_error: AiError | None = None
    for model in models:
        try:
            return ask(db, model=model, **kwargs)
        except BudgetExceeded:
            raise
        except AiError as e:
            log.warning("model %s failed: %s", model, e)
            last_error = e
    raise last_error or AiError("Не настроена ни одна модель")


def ask(
    db: Session,
    messages: list[dict],
    system: str | list[dict] = "",
    tools: list[dict] | None = None,
    model: str | None = None,
    max_tokens: int = 4096,
    thinking: bool = False,
    effort: str | None = None,
):
    """Один вызов Claude API с проверкой бюджета и учётом расхода.
    Возвращает объект Message SDK; поднимает AiError/BudgetExceeded."""
    if not is_configured():
        raise AiError("ИИ не настроен: задайте HS_ANTHROPIC_API_KEY в .env")
    caller = current_caller()
    _check_personal_limit(db, caller)
    left = budget_left(db)
    if left is not None and left <= 0:
        raise BudgetExceeded(
            "Дневной бюджет ИИ исчерпан — продолжу завтра. "
            f"(лимит {settings.ai_daily_token_budget} токенов, HS_AI_DAILY_TOKEN_BUDGET)"
        )

    import anthropic  # импорт здесь: без ключа зависимость не нужна на горячем пути

    kwargs = build_request(
        model or settings.ai_model, messages, system, tools, max_tokens, thinking, effort
    )

    client_kwargs: dict = {
        "api_key": settings.anthropic_api_key,
        "timeout": 120.0,
    }
    if is_local_model(kwargs["model"]):
        # A busy GPU must not hold up the next rung of the ladder.
        client_kwargs["timeout"] = LOCAL_MODEL_TIMEOUT_SECONDS
        client_kwargs["max_retries"] = 0
    if settings.ai_base_url:
        client_kwargs["base_url"] = settings.ai_base_url

    kwargs["extra_headers"] = attribution_headers(caller)

    client = anthropic.Anthropic(**client_kwargs)
    try:
        response = client.messages.create(**kwargs)
    except anthropic.RateLimitError as e:
        raise AiError("Claude API: превышен лимит запросов, попробуйте позже") from e
    except anthropic.APIStatusError as e:
        raise AiError(f"Claude API: ошибка {e.status_code}") from e
    except anthropic.APIConnectionError as e:
        raise AiError("Claude API недоступен (нет интернета?)") from e

    usage = response.usage
    cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
    cache_write = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
    # Cache reads cost a tenth of input, so they do not consume the daily token budget.
    _record_usage(db, usage.input_tokens + cache_write, usage.output_tokens)
    record_spend(
        db,
        caller,
        estimate_usd(
            kwargs["model"], usage.input_tokens, usage.output_tokens, cache_read, cache_write
        ),
    )
    return response
