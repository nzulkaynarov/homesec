#!/usr/bin/env python3
"""Обрезает хранение журнала запросов и статистики AdGuard Home.

Зачем: на Raspberry Pi 3 (1 ГБ RAM, SD-карта) журнал запросов за 90 дней —
главный подозреваемый в переполнении карты и разрастании памяти (ЧП 2026-07-27,
малинка зависла). 24 часа журнала хватает и панели (квоты считаются по
последним минутам), и разбору инцидентов; статистики хватает недели.

Запуск на малинке (по умолчанию — только показать, что будет сделано):
    python3 /opt/homesec/deploy/adguard_retention.py
    python3 /opt/homesec/deploy/adguard_retention.py --apply

Креды берутся из /opt/homesec/backend/.env (HS_ADGUARD_URL/USERNAME/PASSWORD).
Только стандартная библиотека: скрипт должен работать и без venv панели.

API AdGuard менялся между версиями, поэтому пробуем сначала современные
эндпоинты (/control/querylog/config, интервалы в миллисекундах), затем старые
(/control/querylog_config, интервалы в сутках). Единицы определяем по величине
текущего значения, а не по угадыванию версии.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_ENV = "/opt/homesec/backend/.env"
QUERYLOG_HOURS = 24
STATS_DAYS = 7
# Значение в сутках не бывает больше 90; всё, что крупнее, — миллисекунды.
MS_THRESHOLD = 1000


def load_env(path: str) -> dict[str, str]:
    env: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                env[key.strip()] = value.strip().strip("'\"")
    except OSError:
        pass
    # Переменные окружения важнее файла — удобно для проверки на стенде.
    for key in ("HS_ADGUARD_URL", "HS_ADGUARD_USERNAME", "HS_ADGUARD_PASSWORD"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    return env


class Api:
    def __init__(self, base: str, user: str, password: str) -> None:
        self.base = base.rstrip("/")
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        self.auth = f"Basic {token}"

    def call(self, method: str, path: str, payload: dict | None = None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Authorization", self.auth)
        if data:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=10) as r:
            body = r.read()
        if not body:
            return None
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return None


def to_target(current: int | float, hours: int) -> int:
    """Целевое значение в тех же единицах, в каких AdGuard отдал текущее."""
    if current and current >= MS_THRESHOLD:
        return hours * 3600 * 1000
    # Старый формат — целые сутки, меньше суток он не умеет.
    return max(1, round(hours / 24))


def describe(value: int) -> str:
    if value >= MS_THRESHOLD:
        return f"{value / 3600000:g} ч"
    return f"{value:g} сут"


def tune(api: Api, name: str, variants: list[tuple[str, str, str]], hours: int,
         apply: bool) -> str:
    """variants: (GET-путь, метод записи, путь записи) в порядке предпочтения."""
    last_error = "эндпоинт не найден"
    for get_path, write_method, write_path in variants:
        try:
            config = api.call("GET", get_path)
        except urllib.error.HTTPError as e:
            last_error = f"{get_path}: HTTP {e.code}"
            continue
        except urllib.error.URLError as e:
            return f"{name}: AdGuard недоступен ({e.reason})"
        if not isinstance(config, dict) or "interval" not in config:
            last_error = f"{get_path}: неожиданный ответ"
            continue
        current = config["interval"]
        target = to_target(current, hours)
        if current == target:
            return f"{name}: уже {describe(target)} — менять нечего"
        if not apply:
            return (f"{name}: сейчас {describe(current)} → станет {describe(target)}"
                    f" (запусти с --apply)")
        payload = dict(config)
        payload["interval"] = target
        try:
            api.call(write_method, write_path, payload)
        except urllib.error.HTTPError as e:
            return f"{name}: не удалось записать ({write_path}: HTTP {e.code})"
        return f"{name}: было {describe(current)} → стало {describe(target)}"
    return f"{name}: пропущено ({last_error})"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="действительно записать настройки (без флага — только показать)")
    parser.add_argument("--env", default=DEFAULT_ENV, help=f"путь к .env (по умолчанию {DEFAULT_ENV})")
    args = parser.parse_args()

    env = load_env(args.env)
    url = env.get("HS_ADGUARD_URL")
    user = env.get("HS_ADGUARD_USERNAME")
    password = env.get("HS_ADGUARD_PASSWORD")
    if not (url and user and password):
        print(f"Не нашёл HS_ADGUARD_URL/USERNAME/PASSWORD в {args.env} — "
              "задай их или пропиши retention в веб-интерфейсе AdGuard вручную.")
        return 1

    api = Api(url, user, password)
    print(tune(api, "Журнал запросов",
               [("/control/querylog/config", "PUT", "/control/querylog/config/update"),
                ("/control/querylog_config", "POST", "/control/querylog_config")],
               QUERYLOG_HOURS, args.apply))
    print(tune(api, "Статистика",
               [("/control/stats/config", "PUT", "/control/stats/config/update"),
                ("/control/stats_config", "POST", "/control/stats_config"),
                ("/control/stats_info", "POST", "/control/stats_config")],
               STATS_DAYS * 24, args.apply))
    return 0


if __name__ == "__main__":
    sys.exit(main())
