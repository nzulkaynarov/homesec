"""Агент-оркестратор: свободный текст из Telegram -> tool calls из реестра
app/ai/tools.py. Цикл tool use написан вручную, потому что мутирующие
инструменты НЕ исполняются моделью: они откладываются в PendingAction и ждут
кнопку подтверждения в Telegram (принцип №1 из ТЗ). Читающие инструменты
исполняются сразу.

Модель физически не может сделать больше, чем есть в реестре: firewall, NAT,
mangle и DNS-конфиг для неё не существуют."""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session

from ..config import settings
from ..models import log_event
from . import client, confirmations, tools

log = logging.getLogger("homesec.ai.orchestrator")

MAX_TOOL_ROUNDS = 8  # предохранитель от зацикливания

SYSTEM = """Ты — интеллектуальный семейный ассистент HomeSec и управляющий домашней
сетью (панель на Raspberry Pi + роутер MikroTik + фильтр AdGuard Home). Ты общаешься
со взрослыми членами семьи в Telegram.

Твои возможности:
1. Управление сетью: блокировка устройств, пауза интернета, экранные квоты детей,
   режим взрослых на ТВ (set_movie_mode), радар «Кто дома» (get_presence).
   Мутирующие действия (блокировки, паузы, лимиты, переключения ТВ) не выполняются сразу:
   они уходят пользователю на подтверждение кнопкой. Когда инструмент ответил
   «ожидает подтверждения» — скажи, что предложил действие и ждёшь подтверждения.
2. Семейная память: сохраняй важные заметки (save_family_note) — размеры одежды/обуви детей,
   коды домофонов, списки покупок и важные факты, и находи их по запросу (get_family_notes).
3. Универсальный домашний помощник: отвечай на любые вопросы семьи (подбор фильмов на вечер,
   быстрые рецепты, советы по дому, досуг).
4. Правила: отвечай по-русски, лаконично и по делу. Не меняй системный firewall/NAT роутера.
5. Пиши для родителей простыми словами: называй устройства по имени и владельцу, не показывай
   MAC- и IP-адреса, номера (id), названия параметров и английские слова (kid, internet, games),
   не упоминай инструменты по названию."""


@dataclass
class PendingAction:
    """Мутация, ожидающая подтверждения человеком."""

    tool: str
    args: dict
    description: str  # человекочитаемо: что произойдёт


@dataclass
class Answer:
    text: str
    pending: list[PendingAction] = field(default_factory=list)


def _run_read_tool(db: Session, name: str, args: dict) -> tuple[str, bool]:
    """Возвращает (текст результата для модели, is_error)."""
    try:
        result = tools.run_tool(db, name, args, source="ai", reconcile_after=False)
        return json.dumps(result, ensure_ascii=False, default=str), False
    except tools.ToolError as e:
        return str(e), True
    except Exception:
        log.exception("инструмент %s упал", name)
        return "Внутренняя ошибка инструмента", True


def _describe_pending(db: Session, name: str, args: dict) -> str:
    """The confirmation a parent reads: who, what and until when, in plain words."""
    return confirmations.describe(db, name, args)


def handle(db: Session, user_text: str, history: list[dict] | None = None) -> Answer:
    """Обрабатывает сообщение пользователя. Возвращает ответ + отложенные
    мутации (бот навешивает на них кнопки подтверждения)."""
    now = datetime.now()
    messages: list[dict] = list(history or [])
    messages.append({"role": "user", "content": f"[{now:%Y-%m-%d %H:%M}] {user_text}"})
    pending: list[PendingAction] = []

    ladder = client.model_ladder(
        settings.ai_model_local, settings.ai_model, settings.ai_model_support
    )
    rung = 0
    tool_used = False
    seen_calls: set[tuple[str, str]] = set()

    for _ in range(MAX_TOOL_ROUNDS):
        model = ladder[rung]
        can_escalate = rung + 1 < len(ladder)
        try:
            response = client.ask(
                db,
                model=model,
                system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
                messages=messages,
                tools=tools.anthropic_schemas(),
                max_tokens=4096,
                thinking=True,
                effort="medium",  # бытовые команды: скорость и цена важнее глубины
            )
        except client.BudgetExceeded:
            raise
        except client.AiError:
            if not can_escalate:
                raise
            rung += 1
            continue

        if response.stop_reason != "tool_use":
            # The local model is trusted for network commands, not for open questions.
            if client.is_local_model(model) and not tool_used and can_escalate:
                rung += 1
                continue
            text = "".join(b.text for b in response.content if b.type == "text").strip()
            return Answer(text=text or "Готово.", pending=pending)

        unknown_tool = any(
            b.type == "tool_use" and b.name not in tools.REGISTRY for b in response.content
        )
        if unknown_tool and can_escalate:
            rung += 1
            continue

        calls = [
            (b.name, json.dumps(b.input or {}, sort_keys=True, ensure_ascii=False))
            for b in response.content
            if b.type == "tool_use"
        ]
        repeated = any(call in seen_calls for call in calls)
        if repeated and can_escalate:
            # A model repeating itself is stuck; the next rung sees the same results.
            rung += 1
            continue
        seen_calls.update(calls)

        tool_used = True
        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            args = dict(block.input or {})
            problem = tools.argument_problem(block.name, args)
            if problem is not None:
                # Told to the model to fix, never offered to a parent as a broken button.
                content, is_error = problem, True
            elif tools.is_mutating(block.name) and any(
                p.tool == block.name and p.args == args for p in pending
            ):
                content = "Это действие уже ожидает подтверждения. Заверши ответ."
                is_error = False
            elif tools.is_mutating(block.name):
                action = PendingAction(
                    tool=block.name,
                    args=args,
                    description=_describe_pending(db, block.name, args),
                )
                pending.append(action)
                log_event(db, "ai_proposed", action.description)
                content = (
                    "Действие отправлено пользователю на подтверждение "
                    "кнопкой. Не выполнено. Сообщи об этом и заверши ответ."
                )
                is_error = False
            else:
                content, is_error = _run_read_tool(db, block.name, args)
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": content,
                    "is_error": is_error,
                }
            )
        messages.append({"role": "user", "content": tool_results})
        if pending and client.is_local_model(model):
            return Answer(text="Подготовил действие, подтвердите его кнопкой.", pending=pending)

    return Answer(
        text="Я запутался в этом запросе (слишком много шагов) — попробуйте проще.",
        pending=pending,
    )
