"""Мини-миграции схемы. `Base.metadata.create_all` создаёт только НОВЫЕ
таблицы и не трогает существующие, поэтому изменение уже развёрнутых таблиц
(колонки, индексы) описывается здесь простым SQL. Версия хранится в
PRAGMA user_version; применяются только миграции новее текущей версии.

Прод обновляется автоматически (pull-деплой раз в минуту), так что каждая
миграция обязана быть безопасной на живой базе: только аддитивные изменения,
никаких DROP/переименований без явного плана отката.
"""

import logging

from sqlalchemy import Engine, text
from sqlalchemy.exc import OperationalError

log = logging.getLogger("homesec.migrations")

# (версия, [SQL-выражения]); версии строго возрастают, список только растёт.
MIGRATIONS: list[tuple[int, list[str]]] = [
    # hostname из DHCP + таблица дополнительных MAC (анти-рандомизация).
    # На свежей базе не выполняется (stamp_fresh) — create_all уже создаёт
    # актуальную схему; здесь — только доводка СУЩЕСТВУЮЩИХ таблиц.
    (1, [
        "ALTER TABLE devices ADD COLUMN hostname VARCHAR(64) NOT NULL DEFAULT ''",
        "INSERT INTO device_macs (device_id, mac) "
        "SELECT id, mac FROM devices "
        "WHERE mac NOT IN (SELECT mac FROM device_macs)",
    ]),
]


def latest_version(migrations: list[tuple[int, list[str]]] | None = None) -> int:
    migrations = MIGRATIONS if migrations is None else migrations
    return max((v for v, _ in migrations), default=0)


def stamp_fresh(engine: Engine, migrations: list[tuple[int, list[str]]] | None = None) -> None:
    """Для СВЕЖЕЙ базы: create_all уже создал актуальную схему, применять
    миграции не к чему — просто помечаем версию."""
    with engine.begin() as conn:
        conn.execute(text(f"PRAGMA user_version = {latest_version(migrations)}"))


def ensure_schema(engine: Engine, metadata) -> None:
    """Единая точка подготовки схемы для панели и бота: create_all + миграции.
    Свежая база просто помечается последней версией (см. stamp_fresh)."""
    from sqlalchemy import inspect

    fresh = not inspect(engine).has_table("devices")
    metadata.create_all(engine)
    if fresh:
        stamp_fresh(engine)
    else:
        run_migrations(engine)


def run_migrations(engine: Engine, migrations: list[tuple[int, list[str]]] | None = None) -> int:
    """Применяет недостающие миграции, возвращает число применённых."""
    migrations = MIGRATIONS if migrations is None else migrations
    applied = 0
    with engine.begin() as conn:
        current = conn.execute(text("PRAGMA user_version")).scalar() or 0
        for version, statements in sorted(migrations):
            if version <= current:
                continue
            for sql in statements:
                try:
                    conn.execute(text(sql))
                except OperationalError as e:
                    # Колонка уже есть (схему успел создать create_all или
                    # миграция применялась частично) — аддитивные миграции
                    # обязаны это переживать, остальные ошибки — наверх.
                    if "duplicate column" not in str(e).lower():
                        raise
                    log.info("миграция %d: пропуск (%s)", version, e.orig)
            # PRAGMA не принимает bind-параметры; version — int из кода, не ввод
            conn.execute(text(f"PRAGMA user_version = {int(version)}"))
            log.info("миграция %d применена", version)
            applied += 1
    return applied
