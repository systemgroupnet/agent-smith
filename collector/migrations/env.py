"""Alembic migration environment for the collector.

The database URL comes from COLLECTOR_DATABASE_URL (or is injected by
``storage.run_migrations`` via the Alembic config), never hard-coded.
``target_metadata`` points at the storage table definitions so ``alembic
revision --autogenerate`` can diff future schema changes.
"""

import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Make the collector package importable whether run via the alembic CLI or via
# storage.run_migrations().
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import storage  # noqa: E402

config = context.config

if config.config_file_name is not None:
    try:
        fileConfig(config.config_file_name)
    except Exception:
        pass

_url = config.get_main_option("sqlalchemy.url") or os.getenv("COLLECTOR_DATABASE_URL")
if not _url:
    raise RuntimeError("COLLECTOR_DATABASE_URL not set; cannot run migrations")
config.set_main_option("sqlalchemy.url", _url)

target_metadata = storage.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=_url,
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = _url
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
