"""The usage_events dimensions revision, run for real against a throwaway schema (plan 06-21).

Upgrade adds the ledger's dimension and per-tenant keys WITHOUT touching the llm rows already
recorded -- the ``llm`` server default classifies them, so there is no backfill to get wrong -- and
installs the shape CHECK and the per-period unique index. Downgrade deletes the compute and
storage rows (the old shape has nowhere to put a row without a session) and restores the old
shape, including NOT NULL on ``session_id``. A second upgrade proves the round trip is repeatable.
"""

from __future__ import annotations

import importlib.util
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

MIGRATION_FILENAME = "66d5894e72d4_usage_event_dimensions.py"
PREVIOUS_HEAD = "e74e5772a551"
NEW_COLUMNS = {
    "dimension",
    "user_id",
    "environment_id",
    "installation_id",
    "companion_key",
    "period_start",
    "quantity",
}
HOUR = datetime(2026, 9, 5, 10, tzinfo=UTC)


def _migration_path() -> Path:
    return Path(__file__).parents[1] / "alembic" / "versions" / MIGRATION_FILENAME


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("usage_event_dimensions", _migration_path())
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def test_revision_chains_off_the_head_read_before_it_existed() -> None:
    source = _migration_path().read_text()
    match = re.search(r"^down_revision[^=]*=\s*[\"']([0-9a-f]+)[\"']", source, re.M)
    assert match is not None
    assert match.group(1) == PREVIOUS_HEAD


def _create_previous_schema(connection: sa.Connection) -> None:
    """The parents the new foreign keys point at, and usage_events as the prior head left it."""
    for table in ("users", "sessions", "agent_environments", "companion_installations"):
        connection.execute(sa.text(f"CREATE TABLE {table} (id uuid PRIMARY KEY)"))
    connection.execute(
        sa.text(
            """
            CREATE TABLE usage_events (
                id uuid PRIMARY KEY,
                session_id uuid NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
                ts timestamptz NOT NULL,
                provider varchar(100),
                model varchar(200) NOT NULL,
                cost_usd_micros bigint DEFAULT 0
            )
            """
        )
    )


def _insert(connection: sa.Connection, **values: Any) -> uuid.UUID:
    row_id = uuid.uuid4()
    values = {"id": row_id, "ts": HOUR, "model": "m", **values}
    columns = ", ".join(values)
    params = ", ".join(f":{name}" for name in values)
    connection.execute(sa.text(f"INSERT INTO usage_events ({columns}) VALUES ({params})"), values)
    return row_id


def _columns(connection: sa.Connection) -> dict[str, dict[str, Any]]:
    return {column["name"]: column for column in sa.inspect(connection).get_columns("usage_events")}


def _check_names(connection: sa.Connection) -> set[str]:
    return {check["name"] for check in sa.inspect(connection).get_check_constraints("usage_events")}


def _index_names(connection: sa.Connection) -> set[str]:
    return {index["name"] for index in sa.inspect(connection).get_indexes("usage_events")}


def test_upgrade_adds_the_dimensions_and_downgrade_restores_the_old_shape(
    engine: AsyncEngine,
) -> None:
    migration = _load_migration()
    schema = f"usage_event_dimensions_{uuid.uuid4().hex}"
    sync_engine = create_engine(engine.url.set(drivername="postgresql+psycopg2"))
    old_op = migration.op
    user_id, session_id, installation_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    try:
        with sync_engine.begin() as connection:
            connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(sa.text(f'SET search_path TO "{schema}"'))
            _create_previous_schema(connection)
            for table, row_id in (
                ("users", user_id),
                ("sessions", session_id),
                ("companion_installations", installation_id),
            ):
                connection.execute(
                    sa.text(f"INSERT INTO {table} (id) VALUES (:id)"), {"id": row_id}
                )
            llm_id = _insert(connection, session_id=session_id, cost_usd_micros=1_234)
            migration.op = Operations(MigrationContext.configure(connection))

            migration.upgrade()

            columns = _columns(connection)
            assert NEW_COLUMNS <= set(columns)
            assert columns["session_id"]["nullable"] is True
            assert {"ck_usage_events_dimension", "ck_usage_events_dimension_shape"} <= (
                _check_names(connection)
            )
            assert "uq_usage_events_dimension_period" in _index_names(connection)
            # The existing llm row is intact and classified by the server default.
            assert connection.execute(
                sa.text("SELECT dimension, session_id, cost_usd_micros FROM usage_events")
            ).one() == ("llm", session_id, 1_234)

            compute = {
                "dimension": "compute",
                "user_id": user_id,
                "installation_id": installation_id,
                "period_start": HOUR,
                "quantity": 1,
                "cost_usd_micros": 41_666,
            }
            _insert(connection, **compute)
            for mis_shaped in (
                compute,  # the same period again: the unique index
                {**compute, "period_start": None},
                {**compute, "session_id": session_id},
                {"dimension": "llm"},
            ):
                with pytest.raises(IntegrityError):
                    with connection.begin_nested():
                        _insert(connection, **mis_shaped)

            migration.downgrade()

            columns = _columns(connection)
            assert not NEW_COLUMNS & set(columns)
            assert columns["session_id"]["nullable"] is False
            assert not {"ck_usage_events_dimension", "ck_usage_events_dimension_shape"} & (
                _check_names(connection)
            )
            assert "uq_usage_events_dimension_period" not in _index_names(connection)
            # The compute row is gone; the llm row survived both directions.
            assert list(connection.execute(sa.text("SELECT id FROM usage_events")).scalars()) == [
                llm_id
            ]

            migration.upgrade()
            assert NEW_COLUMNS <= set(_columns(connection))
            assert "uq_usage_events_dimension_period" in _index_names(connection)
    finally:
        migration.op = old_op
        with sync_engine.begin() as connection:
            connection.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        sync_engine.dispose()
