"""Alembic environment — async migrations via SQLAlchemy, SQLite+PG compatible."""

from __future__ import annotations

import asyncio
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

# Ensure src/ is on the path so ``astra`` is importable
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from astra.config import settings  # noqa: E402
from astra.db.models import Base  # noqa: E402

config = context.config
config.set_main_option("sqlalchemy.url", settings.database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    is_sqlite = "sqlite" in settings.database_url.lower()
    cfg = config.get_section(config.config_ini_section, {}).copy()
    # Ensure sqlite uses correct connect args
    connectable = async_engine_from_config(
        cfg,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        if not is_sqlite:
            try:
                from sqlalchemy import text

                await connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            except Exception:
                pass
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
