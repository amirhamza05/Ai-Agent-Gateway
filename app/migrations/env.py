"""Alembic environment script — async edition.

Reads ``DATABASE_URL`` from the environment (loaded by docker-compose's
``env_file: .env``) and runs migrations against an async engine. We import
the SQLAlchemy ``Base`` from the gateway package so that future
``--autogenerate`` runs see the model metadata.
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from gateway.db.models import Base

# Alembic config object — values pulled from alembic.ini.
config = context.config

# Inject DATABASE_URL from the environment so we never write it to disk.
# Required even for `alembic upgrade head` against an empty migrations dir.
database_url = os.environ.get("DATABASE_URL")
if not database_url:
    raise RuntimeError(
        "DATABASE_URL is not set. Alembic needs it to connect — see .env.example."
    )
config.set_main_option("sqlalchemy.url", database_url)

# Configure Python logging from alembic.ini's [loggers] section.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Metadata target for `--autogenerate`. Models are added in P2; until then
# this is empty and migrations just no-op.
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting to the DB.

    Useful for code review of generated DDL. Uses the URL from alembic.ini.
    """
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Open an async engine, then hand a sync-style connection to Alembic."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
