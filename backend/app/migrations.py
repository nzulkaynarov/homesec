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

log = logging.getLogger("homesec.migrations")

# (версия, [SQL-выражения]); версии строго возрастают, список только растёт.
MIGRATIONS: list[tuple[int, list[str]]] = [
    # (1, ["ALTER TABLE devices ADD COLUMN ...")]),  # образец
]


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
                conn.execute(text(sql))
            # PRAGMA не принимает bind-параметры; version — int из кода, не ввод
            conn.execute(text(f"PRAGMA user_version = {int(version)}"))
            log.info("миграция %d применена", version)
            applied += 1
    return applied
