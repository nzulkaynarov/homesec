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
4. Правила: отвечай по-русски, лаконично и по делу. Не меняй системный firewall/NAT роутера."""


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


def _describe_pending(db: Session, name: str, args: dict) -> str:
    from ..models import GROUP_LABELS, Device

    spec = tools.REGISTRY[name]
    first_line = spec.description.splitlines()[0]
    details: list[str] = []

    # Резолвим device_id в имя и IP устройства из БД
    if "device_id" in args:
        try:
            dev = db.get(Device, int(args["device_id"]))
            if dev:
                details.append(f"{dev.name} ({dev.ip or dev.mac})")
            else:
                details.append(f"id={args['device_id']}")
        except (ValueError, TypeError):
            details.append(f"id={args['device_id']}")

    # Резолвим дубли при merge_devices
    if "duplicate_id" in args and "target_id" in args:
        try:
            dup = db.get(Device, int(args["duplicate_id"]))
            tgt = db.get(Device, int(args["target_id"]))
            dup_name = dup.name if dup else f"id={args['duplicate_id']}"
            tgt_name = tgt.name if tgt else f"id={args['target_id']}"
            details.append(f"дубль «{dup_name}» → в «{tgt_name}»")
        except (ValueError, TypeError):
            pass

    # Резолвим target (группа или устройство)
    if "target" in args:
        t = str(args["target"]).strip()
        if t in GROUP_LABELS:
            details.append(f"группа «{GROUP_LABELS[t]}»")
        else:
            dev = tools.find_device(db, t)
            if dev:
                details.append(f"{dev.name} ({dev.ip or dev.mac})")
            else:
                details.append(t)

    for k, v in args.items():
        if k in ("device_id", "duplicate_id", "target_id", "target"):
            continue
        if k == "minutes":
            details.append(f"{v} мин")
        elif k == "limit":
            details.append(f"лимит {v}")
        elif k == "category":
            details.append(f"категория {v}")
        elif k == "person_name":
            details.append(f"владелец {v}")
        elif v:
            details.append(f"{k}={v}")

    if details:
        return f"{first_line}: {', '.join(details)}"
    return first_line


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
            system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
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
                    description=_describe_pending(db, block.name, args),
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
