"""Агент-оркестратор: свободный текст из Telegram -> tool calls из реестра
app/ai/tools.py. Цикл tool use написан вручную, потому что мутирующие
инструменты НЕ исполняются моделью: они откладываются в PendingAction и ждут
кнопку подтверждения в Telegram (принцип №1 из ТЗ). Читающие инструменты
исполняются сразу.

Модель физически не может сделать больше, чем есть в реестре: firewall, NAT,
mangle и DNS-конфиг для неё не существуют."""

import logging
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session

from ..models import log_event
from . import client, tools

log = logging.getLogger("homesec.ai.orchestrator")

MAX_TOOL_ROUNDS = 8  # предохранитель от зацикливания

SYSTEM = """Ты — ИИ-помощник домашней сети HomeSec (панель родительского
контроля на Raspberry Pi + роутер MikroTik + фильтр AdGuard Home). Ты
общаешься с владельцем дома в Telegram и управляешь сетью через инструменты.

Правила:
- отвечай по-русски, коротко, без markdown;
- прежде чем менять что-то, посмотри текущее состояние (list_devices/get_status);
- мутирующие действия (блокировки, паузы, лимиты) не выполняются сразу:
  они уходят пользователю на подтверждение кнопкой. Когда инструмент ответил
  «ожидает подтверждения» — просто скажи, что предложил действие и ждёшь
  подтверждения. Не обещай, что действие уже выполнено;
- если запрос неоднозначен (несколько похожих устройств) — уточни, не гадай;
- ты не можешь менять настройки firewall/NAT/DNS и самого роутера — только
  блокировки, паузы, лимиты и владельцев устройств. Если просят большее,
  честно скажи, что это делается руками в панели или на роутере."""


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
        import json

        return json.dumps(result, ensure_ascii=False, default=str), False
    except tools.ToolError as e:
        return str(e), True
    except Exception:
        log.exception("инструмент %s упал", name)
        return "Внутренняя ошибка инструмента", True


def _describe_pending(name: str, args: dict) -> str:
    spec = tools.REGISTRY[name]
    first_line = spec.description.splitlines()[0]
    arg_str = ", ".join(f"{k}={v}" for k, v in args.items())
    return f"{first_line} ({arg_str})"


def handle(db: Session, user_text: str, history: list[dict] | None = None) -> Answer:
    """Обрабатывает сообщение пользователя. Возвращает ответ + отложенные
    мутации (бот навешивает на них кнопки подтверждения)."""
    now = datetime.now()
    messages: list[dict] = list(history or [])
    messages.append({"role": "user", "content": f"[{now:%Y-%m-%d %H:%M}] {user_text}"})
    pending: list[PendingAction] = []

    for _ in range(MAX_TOOL_ROUNDS):
        response = client.ask(
            db,
            system=SYSTEM,
            messages=messages,
            tools=tools.anthropic_schemas(),
            max_tokens=4096,
            thinking=True,
            effort="medium",  # бытовые команды: скорость и цена важнее глубины
        )
        if response.stop_reason != "tool_use":
            text = "".join(b.text for b in response.content if b.type == "text").strip()
            return Answer(text=text or "Готово.", pending=pending)

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            args = dict(block.input or {})
            if tools.is_mutating(block.name):
                action = PendingAction(
                    tool=block.name,
                    args=args,
                    description=_describe_pending(block.name, args),
                )
                pending.append(action)
                log_event(db, "ai_proposed", action.description)
                content = ("Действие отправлено пользователю на подтверждение "
                           "кнопкой. Не выполнено. Сообщи об этом и заверши ответ.")
                is_error = False
            else:
                content, is_error = _run_read_tool(db, block.name, args)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": content,
                "is_error": is_error,
            })
        messages.append({"role": "user", "content": tool_results})

    return Answer(
        text="Я запутался в этом запросе (слишком много шагов) — попробуйте проще.",
        pending=pending,
    )
