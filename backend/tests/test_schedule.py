from datetime import datetime

import pytest

from app.models import Rule
from app.services.enforcement import rule_is_active


def _night_rule():
    # Будни 22:00–07:00 (окно через полночь)
    return Rule(name="night", target_type="group", target="kid",
                days="0,1,2,3,4", start_time="22:00", end_time="07:00", enabled=True)


def _day_rule():
    # Выходные 09:00–13:00 (обычное окно)
    return Rule(name="day", target_type="group", target="kid",
                days="5,6", start_time="09:00", end_time="13:00", enabled=True)


@pytest.mark.parametrize("now,expected", [
    (datetime(2026, 7, 13, 23, 0), True),   # Пн 23:00 — активно
    (datetime(2026, 7, 14, 3, 0), True),    # Вт 03:00 — окно, начатое в Пн
    (datetime(2026, 7, 14, 8, 0), False),   # Вт 08:00 — вне окна
    (datetime(2026, 7, 18, 23, 0), False),  # Сб 23:00 — суббота не в днях
    (datetime(2026, 7, 18, 3, 0), True),    # Сб 03:00 — окно, начатое в Пт
    (datetime(2026, 7, 13, 21, 59), False), # Пн 21:59 — ещё рано
])
def test_overnight_window(now, expected):
    assert rule_is_active(_night_rule(), now) is expected


@pytest.mark.parametrize("now,expected", [
    (datetime(2026, 7, 18, 10, 0), True),   # Сб 10:00 — активно
    (datetime(2026, 7, 17, 10, 0), False),  # Пт 10:00 — пятница не в днях
    (datetime(2026, 7, 18, 13, 0), False),  # Сб 13:00 — конец исключительный
])
def test_daytime_window(now, expected):
    assert rule_is_active(_day_rule(), now) is expected


def test_disabled_rule_never_active():
    r = _night_rule()
    r.enabled = False
    assert rule_is_active(r, datetime(2026, 7, 13, 23, 0)) is False


def test_malformed_rule_is_inactive():
    r = Rule(name="bad", target_type="group", target="kid",
             days="0", start_time="oops", end_time="07:00", enabled=True)
    assert rule_is_active(r, datetime(2026, 7, 13, 23, 0)) is False
