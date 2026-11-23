"""The companion-scope back-fill onto stored managed runtime keys (06-02, WINDOWS #199).

``api_keys.scopes`` is a per-row array, so widening ``RUNTIME_MCP_SCOPES`` reaches only keys minted
after the change. This revision appends ``companions:read`` and ``companions:invoke`` to the keys
already issued -- and ONLY to managed, unrevoked keys whose stored scopes carry both
``connectors:read`` and ``connectors:invoke``, which is the runtime bundle's signature
(T-06-02-02). Every other key is asserted unchanged, because a data migration that widens the
authority of issued credentials is exactly where an over-broad WHERE clause does the most damage.

Shape follows ``test_hosted_runtime_key_scope_migration.py``: load the revision module, run its
real Alembic operations through ``Operations(MigrationContext.configure(...))``, against a scratch
``api_keys`` table in a throwaway schema.
"""

from __future__ import annotations

import importlib.util
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncEngine

MIGRATION_FILENAME = "cdadb5203b54_companion_runtime_scopes.py"
COMPANION_SCOPES = ["companions:read", "companions:invoke"]
# The runtime bundle as stored BEFORE this revision: the order a key minted then carries.
PRE_06_02_RUNTIME_SCOPES = [
    "connectors:read",
    "connectors:invoke",
    "memories:read",
    "memories:write",
    "projects:read",
    "sessions:read",
    "sessions:write",
    "skills:read",
    "skills:write",
    "vault:read",
]


def _load_migration():
    migration_path = Path(__file__).parents[1] / "alembic" / "versions" / MIGRATION_FILENAME
    spec = importlib.util.spec_from_file_location("companion_runtime_scopes", migration_path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


@pytest.fixture
def scratch(engine: AsyncEngine) -> Iterator[tuple[sa.Connection, object]]:
    """A throwaway schema holding the columns this revision reads, and the revision bound to it."""
    migration = _load_migration()
    schema = f"companion_runtime_scopes_{uuid.uuid4().hex}"
    sync_engine = create_engine(engine.url.set(drivername="postgresql+psycopg2"))
    old_op = migration.op
    try:
        with sync_engine.begin() as connection:
            connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(sa.text(f'SET search_path TO "{schema}"'))
            connection.execute(
                sa.text(
                    """
                    CREATE TABLE api_keys (
                        id uuid PRIMARY KEY,
                        managed boolean NOT NULL DEFAULT false,
                        revoked_at timestamptz,
                        scopes varchar(64)[]
                    )
                    """
                )
            )
            migration.op = Operations(MigrationContext.configure(connection))
            yield connection, migration
    finally:
        migration.op = old_op
        with sync_engine.begin() as connection:
            connection.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        sync_engine.dispose()


def _insert_key(
    connection: sa.Connection,
    *,
    scopes: list[str] | None,
    managed: bool = True,
    revoked: bool = False,
) -> uuid.UUID:
    key_id = uuid.uuid4()
    connection.execute(
        sa.text(
            "INSERT INTO api_keys (id, managed, revoked_at, scopes) "
            "VALUES (CAST(:id AS uuid), :managed, :revoked_at, :scopes)"
        ),
        {
            "id": str(key_id),
            "managed": managed,
            "revoked_at": datetime.now(UTC) if revoked else None,
            "scopes": scopes,
        },
    )
    return key_id


def _scopes(connection: sa.Connection, key_id: uuid.UUID) -> list[str] | None:
    return connection.execute(
        sa.text("SELECT scopes FROM api_keys WHERE id = CAST(:id AS uuid)"),
        {"id": str(key_id)},
    ).scalar_one()


def test_upgrade_appends_both_companion_scopes_to_a_managed_runtime_key(scratch) -> None:
    connection, migration = scratch
    runtime_key = _insert_key(connection, scopes=list(PRE_06_02_RUNTIME_SCOPES))

    migration.upgrade()

    # Appended exactly once each, after every scope the key already held, in their stored order.
    assert _scopes(connection, runtime_key) == [*PRE_06_02_RUNTIME_SCOPES, *COMPANION_SCOPES]


def test_upgrade_leaves_every_other_key_unchanged(scratch) -> None:
    connection, migration = scratch
    unmanaged = _insert_key(connection, scopes=list(PRE_06_02_RUNTIME_SCOPES), managed=False)
    null_scopes = _insert_key(connection, scopes=None)
    revoked = _insert_key(connection, scopes=list(PRE_06_02_RUNTIME_SCOPES), revoked=True)
    # Managed and live, but not the runtime bundle: only one of the two connectors scopes.
    narrow = ["connectors:read", "sessions:write"]
    not_runtime = _insert_key(connection, scopes=list(narrow))
    # The positive control in the same run, so "nothing changed" cannot be a no-op revision.
    runtime_key = _insert_key(connection, scopes=list(PRE_06_02_RUNTIME_SCOPES))

    migration.upgrade()

    assert _scopes(connection, unmanaged) == PRE_06_02_RUNTIME_SCOPES
    assert _scopes(connection, null_scopes) is None
    assert _scopes(connection, revoked) == PRE_06_02_RUNTIME_SCOPES
    assert _scopes(connection, not_runtime) == narrow
    assert _scopes(connection, runtime_key) == [*PRE_06_02_RUNTIME_SCOPES, *COMPANION_SCOPES]


def test_upgrade_twice_leaves_one_copy_of_each_scope(scratch) -> None:
    connection, migration = scratch
    runtime_key = _insert_key(connection, scopes=list(PRE_06_02_RUNTIME_SCOPES))
    # A key minted AFTER the tuple widened already carries both, in the tuple's own order.
    minted_after = _insert_key(connection, scopes=[*COMPANION_SCOPES, *PRE_06_02_RUNTIME_SCOPES])
    # A key holding one of the two gains only the other.
    half = _insert_key(connection, scopes=[*PRE_06_02_RUNTIME_SCOPES, "companions:read"])

    migration.upgrade()
    migration.upgrade()

    assert _scopes(connection, runtime_key) == [*PRE_06_02_RUNTIME_SCOPES, *COMPANION_SCOPES]
    assert _scopes(connection, minted_after) == [*COMPANION_SCOPES, *PRE_06_02_RUNTIME_SCOPES]
    assert _scopes(connection, half) == [
        *PRE_06_02_RUNTIME_SCOPES,
        "companions:read",
        "companions:invoke",
    ]


def test_downgrade_removes_exactly_the_two_scopes_and_keeps_the_rest_in_order(scratch) -> None:
    connection, migration = scratch
    runtime_key = _insert_key(connection, scopes=list(PRE_06_02_RUNTIME_SCOPES))
    minted_after = _insert_key(connection, scopes=[*COMPANION_SCOPES, *PRE_06_02_RUNTIME_SCOPES])
    unmanaged = _insert_key(
        connection, scopes=[*PRE_06_02_RUNTIME_SCOPES, *COMPANION_SCOPES], managed=False
    )

    migration.upgrade()
    migration.downgrade()

    assert _scopes(connection, runtime_key) == PRE_06_02_RUNTIME_SCOPES
    assert _scopes(connection, minted_after) == PRE_06_02_RUNTIME_SCOPES
    # Outside the row set in both directions: downgrade reverses only what upgrade could touch.
    assert _scopes(connection, unmanaged) == [*PRE_06_02_RUNTIME_SCOPES, *COMPANION_SCOPES]
