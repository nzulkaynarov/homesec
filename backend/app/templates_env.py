from pathlib import Path

from fastapi.templating import Jinja2Templates

from .models import GROUP_LABELS
from .services.adguard import SERVICE_CATEGORIES

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.globals["GROUP_LABELS"] = GROUP_LABELS
templates.env.globals["SERVICE_CATEGORIES"] = SERVICE_CATEGORIES

DAY_NAMES = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
templates.env.globals["DAY_NAMES"] = DAY_NAMES
