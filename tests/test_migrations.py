"""The initial Alembic migration must produce the same schema as the ORM."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from app.repositories.orm import Base


def test_upgrade_head_matches_orm_metadata(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "migration_test.sqlite"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")

    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")

    engine = sa.create_engine(f"sqlite:///{db_path}")
    inspector = sa.inspect(engine)
    migrated_tables = set(inspector.get_table_names()) - {"alembic_version"}
    orm_tables = set(Base.metadata.tables)
    assert migrated_tables == orm_tables

    for table in sorted(orm_tables):
        migrated_columns = {col["name"] for col in inspector.get_columns(table)}
        orm_columns = {col.name for col in Base.metadata.tables[table].columns}
        assert migrated_columns == orm_columns, f"column drift in table {table}"
    engine.dispose()


def test_downgrade_removes_all_tables(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "migration_down.sqlite"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")

    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")

    engine = sa.create_engine(f"sqlite:///{db_path}")
    inspector = sa.inspect(engine)
    assert set(inspector.get_table_names()) <= {"alembic_version"}
    engine.dispose()
