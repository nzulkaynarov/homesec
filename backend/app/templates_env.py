from pathlib import Path

from fastapi.templating import Jinja2Templates

from .models import GROUP_LABELS
from .services.adguard import SERVICE_CATEGORIES

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.globals["GROUP_LABELS"] = GROUP_LABELS
templates.env.globals["SERVICE_CATEGORIES"] = SERVICE_CATEGORIES

DAY_NAMES = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
templates.env.globals["DAY_NAMES"] = DAY_NAMES

EVENT_KIND_LABELS = {
    "device_new": "Новое устройство",
    "device_maybe_same": "Возможный дубль",
    "register_request": "Запрос доступа",
    "bonus_request": "Просьба о времени",
    "quota_block": "Квота исчерпана",
    "quota_warn": "Квота 80%",
    "block": "Блокировка",
    "ip_conflict": "Смена IP",
    "error": "Ошибка",
}
templates.env.globals["EVENT_KIND_LABELS"] = EVENT_KIND_LABELS
