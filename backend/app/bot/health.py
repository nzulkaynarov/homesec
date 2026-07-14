"""Health-мониторинг с антидребезгом: алерт после N подряд неудачных проверок,
сообщение о восстановлении — после M подряд удачных. Одиночный таймаут
(роутер занят, панель перезапускается деплоем) тревогу не поднимает."""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from ..config import settings
from ..services import adguard, mikrotik

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


@dataclass
class _CheckState:
    fails: int = 0
    oks: int = 0
    alerted: bool = False


@dataclass
class HealthMonitor:
    checks: dict[str, Callable[[], bool]]
    fail_after: int = 3
    ok_after: int = 2
    state: dict[str, _CheckState] = field(default_factory=dict)

    def tick(self) -> list[str]:
        """Прогоняет все проверки, возвращает сообщения для отправки."""
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
                st.oks += 1
                if st.alerted and st.oks >= self.ok_after:
                    st.alerted = False
                    messages.append(f"✅ {name}: снова работает")
            else:
                st.oks = 0
                st.fails += 1
                if not st.alerted and st.fails >= self.fail_after:
                    st.alerted = True
                    messages.append(f"🔴 {name}: не отвечает (проверок подряд: {st.fails})")
        return messages


def default_monitor() -> HealthMonitor:
    return HealthMonitor(checks={
        "Роутер MikroTik": probe_router,
        "AdGuard Home (DNS-фильтр)": probe_adguard,
        "Панель HomeSec": probe_panel,
    })
