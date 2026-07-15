"""Мини-миграции: порядок, идемпотентность, версия в PRAGMA user_version."""

from sqlalchemy import create_engine, text

from app.db import Base
from app.db import engine as app_engine
from app.migrations import MIGRATIONS, run_migrations


def test_migrations_apply_once_in_order(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    migs = [
        (2, ["CREATE TABLE b (x INTEGER)"]),
        (1, ["CREATE TABLE a (x INTEGER)", "CREATE INDEX ix_a ON a (x)"]),
    ]
    assert run_migrations(eng, migs) == 2
    assert run_migrations(eng, migs) == 0  # повторный запуск ничего не делает
    with eng.connect() as c:
        assert c.execute(text("PRAGMA user_version")).scalar() == 2
        c.execute(text("INSERT INTO a VALUES (1)"))
        c.execute(text("INSERT INTO b VALUES (1)"))


def test_migration_2_survives_precreated_table(tmp_path):
    """Как на проде: ensure_schema сначала зовёт create_all (тот уже создаёт
    pending_actions), потом миграции — №2 обязана пережить готовую таблицу."""
    eng = create_engine(f"sqlite:///{tmp_path / 'p.db'}")
    Base.metadata.create_all(eng)
    migs = [m for m in MIGRATIONS if m[0] == 2]
    assert run_migrations(eng, migs) == 1  # не упала, версия проставлена
    with eng.begin() as c:
        assert c.execute(text("PRAGMA user_version")).scalar() == 2
        c.execute(text(
            "INSERT INTO pending_actions (tool, args, description, created) "
            "VALUES ('block_device', '{}', 'тест', '2026-07-15 12:00:00')"
        ))


def test_app_db_uses_wal():
    with app_engine.connect() as c:
        assert c.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        assert c.execute(text("PRAGMA busy_timeout")).scalar() == 5000
