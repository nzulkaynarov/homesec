"""Тестовое окружение задаётся ДО импорта приложения: настройки читаются
один раз при импорте app.config, поэтому переменные ставим здесь, на самом
верху conftest (pytest загружает его первым)."""

import os
import pathlib
import tempfile

_db = pathlib.Path(tempfile.gettempdir()) / "homesec_pytest.db"
if _db.exists():
    _db.unlink()

os.environ["HS_SCHEDULER_ENABLED"] = "false"
os.environ["HS_DATABASE_PATH"] = str(_db)
os.environ["HS_ADMIN_USERNAME"] = "admin"
os.environ["HS_ADMIN_PASSWORD"] = "testpass"
os.environ["HS_SECRET_KEY"] = "test-secret-key"
# Порты, на которых заведомо никто не слушает — интеграции быстро падают в
# connection refused и обрабатываются как «сервис недоступен».
os.environ["HS_MIKROTIK_HOST"] = "127.0.0.1"
os.environ["HS_ADGUARD_URL"] = "http://127.0.0.1:9"
# TestClient шлёт Host: testserver — он должен считаться «своим» адресом панели,
# иначе middleware примет тестовые запросы за NAT-перехваченные.
os.environ["HS_PANEL_LAN_URL"] = "http://testserver:8000"

import logging  # noqa: E402

logging.disable(logging.WARNING)
