"""Обёртка над Claude API: единая точка вызова модели с дневным бюджетом
токенов. Все ИИ-агенты (orchestrator, analyst, watchdog) ходят через ask() —
поэтому лимит расхода и учёт в одном месте. Расход пишется в kv_state по дням
и виден в журнале не хуже других событий.

Без ключа (HS_ANTHROPIC_API_KEY пуст) ИИ-фичи молчат: is_configured() = False,
вызов ask() кидает AiError — вызывающие обязаны деградировать вежливо."""

import json
import logging
from datetime import datetime

from sqlalchemy.orm import Session

from ..config import settings
from ..models import kv_get, kv_set

log = logging.getLogger("homesec.ai")

USAGE_KEY_PREFIX = "ai_usage:"  # ai_usage:2026-07-14 -> {"input":..,"output":..,"requests":..}


class AiError(Exception):
    """Ошибка ИИ-слоя; текст безопасен для показа пользователю."""


class BudgetExceeded(AiError):
    pass


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


def budget_left(db: Session) -> int | None:
    """Сколько токенов осталось на сегодня; None = лимит не задан."""
    if not settings.ai_daily_token_budget:
        return None
    usage = usage_today(db)
    return settings.ai_daily_token_budget - usage["input"] - usage["output"]


def ask(
    db: Session,
    messages: list[dict],
    system: str = "",
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
    left = budget_left(db)
    if left is not None and left <= 0:
        raise BudgetExceeded(
            "Дневной бюджет ИИ исчерпан — продолжу завтра. "
            f"(лимит {settings.ai_daily_token_budget} токенов, HS_AI_DAILY_TOKEN_BUDGET)"
        )

    import anthropic  # импорт здесь: без ключа зависимость не нужна на горячем пути

    kwargs: dict = {
        "model": model or settings.ai_model,
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if system:
        kwargs["system"] = system
    if tools:
        kwargs["tools"] = tools
    if thinking:
        kwargs["thinking"] = {"type": "adaptive"}
    if effort:
        kwargs["output_config"] = {"effort": effort}

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key, timeout=120.0)
    try:
        response = client.messages.create(**kwargs)
    except anthropic.RateLimitError as e:
        raise AiError("Claude API: превышен лимит запросов, попробуйте позже") from e
    except anthropic.APIStatusError as e:
        raise AiError(f"Claude API: ошибка {e.status_code}") from e
    except anthropic.APIConnectionError as e:
        raise AiError("Claude API недоступен (нет интернета?)") from e

    _record_usage(db, response.usage.input_tokens, response.usage.output_tokens)
    return response
