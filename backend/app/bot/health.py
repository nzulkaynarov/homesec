"""Health-мониторинг с антидребезгом: алерт после N подряд неудачных проверок,
сообщение о восстановлении — после M подряд удачных. Одиночный таймаут
(роутер занят, панель перезапускается деплоем) тревогу не поднимает."""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

import httpx

from .. import db as dbmod
from ..config import settings
from ..services import adguard, enforcement, mikrotik

log = logging.getLogger("homesec.bot.health")


def probe_router() -> bool:
    return mikrotik.api_reachable()


def probe_adguard() -> bool:
    try:
        adguard.get_stats()
        return True
    except adguard.AdGuardError:
        return False


def probe_panel() -> bool:
    try:
        r = httpx.get(f"{settings.panel_url}/login", timeout=5)
        return r.status_code < 500
    except httpx.HTTPError:
        return False


def _enforcement() -> tuple[bool, str]:
    session = dbmod.session()
    try:
        return enforcement.enforcement_status(session)
    finally:
        session.close()


def probe_enforcement() -> bool:
    """Rules really reach the router and AdGuard, not just that the router port answers."""
    return _enforcement()[0]


# What each reason for stopped rules means for the family and what a parent can do about it.
CAUSE_TEXTS = {
    "mikrotik": (
        "Причина: роутер не отвечает. Если дома пропал интернет, выключите роутер из розетки "
        "на 10 секунд и включите снова."
    ),
    "adguard": (
        "Причина: не отвечает фильтр сайтов. Если через 15 минут не наладится, выключите "
        "Raspberry Pi из розетки на 10 секунд и включите снова."
    ),
    "queues": "Причина: не получается менять скорость устройств, остальное должно работать.",
    "self_ip": (
        "HomeSec не может узнать свой адрес в сети и ничего не меняет, чтобы не оставить дом "
        "без интернета. Нужен тот, кто настраивал систему."
    ),
}
UNKNOWN_CAUSE = (
    "Причину я записал в журнал. Если через 15 минут не придёт «снова применяются», "
    "выключите Raspberry Pi из розетки на 10 секунд и включите снова."
)


def enforcement_detail() -> str:
    """Why the rules stopped, in words for parents; the technical error stays in the log."""
    session = dbmod.session()
    try:
        causes = enforcement.last_causes(session)
    finally:
        session.close()
    known = [CAUSE_TEXTS[cause] for cause in causes if cause in CAUSE_TEXTS]
    return "\n".join(known) if known else UNKNOWN_CAUSE


ENFORCEMENT_CHECK = "Правила дома"
ROUTER_CHECK = "Роутер"
FILTER_CHECK = "Фильтр сайтов"
PANEL_CHECK = "HomeSec на Raspberry Pi"
SINCE = "{since}"  # replaced with the time of the first failed check

FAILURE_TEXTS = {
    ROUTER_CHECK: (
        "🔴 Роутер не отвечает с {since}. Пока он не отвечает, паузы и лимиты не меняются.\n"
        "Если дома пропал интернет, выключите роутер из розетки на 10 секунд и включите снова."
    ),
    FILTER_CHECK: (
        "🔴 Фильтр сайтов не отвечает с {since}: сайты дома могут не открываться, "
        "запреты сайтов для детей не действуют.\n"
        "Если через 15 минут не придёт «снова работает», выключите Raspberry Pi из розетки "
        "на 10 секунд и включите снова."
    ),
    PANEL_CHECK: (
        "🔴 HomeSec на Raspberry Pi не отвечает с {since}: паузы, лимиты и отбой сейчас "
        "не включаются и не выключаются. Кнопки бота отвечают, но до роутера не доходят.\n"
        "Обычно он поднимается сам за пару минут. Если через 15 минут не придёт «снова "
        "работает», выключите Raspberry Pi из розетки на 10 секунд и включите снова."
    ),
    ENFORCEMENT_CHECK: (
        "🔴 Правила дома не применяются (заметил в {since}): новые паузы, лимиты и "
        "блокировки могут не сработать."
    ),
}
RECOVERY_TEXTS = {
    ROUTER_CHECK: "✅ Роутер снова работает.",
    FILTER_CHECK: "✅ Фильтр сайтов снова работает.",
    PANEL_CHECK: "✅ HomeSec на Raspberry Pi снова работает.",
    ENFORCEMENT_CHECK: "✅ Правила дома снова применяются.",
}


@dataclass
class _CheckState:
    fails: int = 0
    oks: int = 0
    alerted: bool = False
    since: datetime | None = None  # the first failed check of this run of failures


@dataclass
class HealthMonitor:
    checks: dict[str, Callable[[], bool]]
    fail_after: int = 3
    ok_after: int = 2
    state: dict[str, _CheckState] = field(default_factory=dict)
    failure_texts: dict[str, str] = field(default_factory=dict)
    details: dict[str, Callable[[], str]] = field(default_factory=dict)
    recovery_texts: dict[str, str] = field(default_factory=dict)

    def _failure(self, name: str, since: datetime) -> str:
        """What stopped, since when, what it means for the family and what to do."""
        text = self.failure_texts.get(name, f"🔴 {name}: не отвечает с {SINCE}.")
        text = text.replace(SINCE, f"{since:%H:%M}")
        detail = self.details.get(name)
        if detail is not None:
            try:
                text = f"{text}\n{detail()}"
            except Exception:
                log.exception("подробности %s не получены", name)
        return text

    def tick(self, now: datetime | None = None) -> list[str]:
        """Прогоняет все проверки, возвращает сообщения для отправки."""
        now = now or datetime.now()
        messages = []
        for name, probe in self.checks.items():
            st = self.state.setdefault(name, _CheckState())
            try:
                ok = probe()
            except Exception:
                log.exception("проверка %s упала", name)
                ok = False
            if ok:
                st.fails = 0
                st.since = None
                st.oks += 1
                if st.alerted and st.oks >= self.ok_after:
                    st.alerted = False
                    messages.append(self.recovery_texts.get(name, f"✅ {name}: снова работает"))
            else:
                st.oks = 0
                st.fails += 1
                st.since = st.since or now
                if not st.alerted and st.fails >= self.fail_after:
                    st.alerted = True
                    messages.append(self._failure(name, st.since))
        return messages


def default_monitor() -> HealthMonitor:
    return HealthMonitor(
        checks={
            ROUTER_CHECK: probe_router,
            FILTER_CHECK: probe_adguard,
            PANEL_CHECK: probe_panel,
            ENFORCEMENT_CHECK: probe_enforcement,
        },
        failure_texts=FAILURE_TEXTS,
        details={ENFORCEMENT_CHECK: enforcement_detail},
        recovery_texts=RECOVERY_TEXTS,
    )
