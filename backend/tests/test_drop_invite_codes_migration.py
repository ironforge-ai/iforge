"""06.3 D-01 drop-invite-codes migration coverage.

The invite-code system is gone (Clerk access mode decides sign-up), so the
`invite_codes` table is dropped by one revision chained off the merge head
1ea3feb6b7cc. These tests pin that ordering, pin the history to a single
head, and run the real revision's upgrade/downgrade inside a throwaway
schema (same harness as test_invite_grandfathering_migration.py) to prove the
round trip converges and leaves `users.invite_exempted_at` and
`users.billing_unlocked_at` untouched.
"""

from __future__ import annotations

import importlib.util
import uuid
from datetime import UTC, datetime
from pathlib import Path

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

_BACKEND_DIR = Path(__file__).resolve().parents[1]
_REVISION = "107014e81cb5"
_PREVIOUS_HEAD = "1ea3feb6b7cc"
_REVISION_FILE = _BACKEND_DIR / "alembic" / "versions" / f"{_REVISION}_drop_invite_codes.py"
_INDEXES = ("ix_invite_codes_batch_id", "ix_invite_codes_code", "ix_invite_codes_redeemed_by")


def _load_migration():
    assert _REVISION_FILE.is_file(), f"missing revision file {_REVISION_FILE.name}"
    spec = importlib.util.spec_from_file_location("drop_invite_codes_migration", _REVISION_FILE)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def _script_directory() -> ScriptDirectory:
    config = Config(str(_BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(_BACKEND_DIR / "alembic"))
    return ScriptDirectory.from_config(config)


def test_history_has_one_head_that_includes_the_drop_revision():
    script = _script_directory()
    heads = script.get_heads()
    assert len(heads) == 1
    ancestry = {revision.revision for revision in script.walk_revisions("base", heads[0])}
    assert _REVISION in ancestry


def test_drop_revision_chains_off_the_merge_head():
    migration = _load_migration()
    assert migration.revision == _REVISION
    assert migration.down_revision == _PREVIOUS_HEAD


async def test_drop_round_trip_converges_and_keeps_billing_columns(engine: AsyncEngine):
    """downgrade() recreates the empty table and its indexes; upgrade() drops
    them again; a second downgrade/upgrade cycle lands in the same state and
    the users row's two access timestamps never change."""
    migration = _load_migration()
    schema = f"drop_invite_codes_rt_{uuid.uuid4().hex}"
    exempted_at = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    unlocked_at = datetime(2026, 2, 3, 4, 5, 6, tzinfo=UTC)
    user_id = uuid.uuid4()

    def table_present(sync_conn) -> bool:
        return sync_conn.execute(
            text("SELECT to_regclass(:qualified) IS NOT NULL"),
            {"qualified": f'"{schema}".invite_codes'},
        ).scalar_one()

    def index_names(sync_conn) -> list[str]:
        rows = sync_conn.execute(
            text(
                "SELECT indexname FROM pg_indexes "
                "WHERE schemaname = :schema AND tablename = 'invite_codes' "
                "AND indexname LIKE 'ix_%' ORDER BY indexname"
            ),
            {"schema": schema},
        ).all()
        return [row.indexname for row in rows]

    def user_timestamps(sync_conn):
        return sync_conn.execute(
            text("SELECT invite_exempted_at, billing_unlocked_at FROM users WHERE id = :id"),
            {"id": user_id},
        ).one()

    def assert_dropped(sync_conn) -> None:
        assert table_present(sync_conn) is False
        assert index_names(sync_conn) == []
        row = user_timestamps(sync_conn)
        assert row.invite_exempted_at == exempted_at
        assert row.billing_unlocked_at == unlocked_at

    def run(sync_conn):
        old_op = migration.op
        sync_conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        sync_conn.execute(text(f'SET search_path TO "{schema}"'))
        try:
            # Only the table the revision's downgrade references, in minimal form.
            sync_conn.execute(
                text(
                    "CREATE TABLE users ("
                    "id uuid PRIMARY KEY, "
                    "invite_exempted_at timestamptz, "
                    "billing_unlocked_at timestamptz)"
                )
            )
            sync_conn.execute(
                text(
                    "INSERT INTO users (id, invite_exempted_at, billing_unlocked_at) "
                    "VALUES (:id, :exempted, :unlocked)"
                ),
                {"id": user_id, "exempted": exempted_at, "unlocked": unlocked_at},
            )

            migration.op = Operations(MigrationContext.configure(sync_conn))

            # The pre-drop state: downgrade() rebuilds the structure c7e2a9f4d5b8 made.
            migration.downgrade()
            assert table_present(sync_conn) is True
            assert index_names(sync_conn) == list(_INDEXES)
            sync_conn.execute(
                text(
                    "INSERT INTO invite_codes (id, code, batch_id, redeemed_by, redeemed_at) "
                    "VALUES (:id, 'code-1', :batch, :user, now())"
                ),
                {"id": uuid.uuid4(), "batch": uuid.uuid4(), "user": user_id},
            )

            migration.upgrade()
            assert_dropped(sync_conn)

            # Interrupted-and-retried history converges on the same end state.
            migration.downgrade()
            assert table_present(sync_conn) is True
            assert index_names(sync_conn) == list(_INDEXES)
            migration.upgrade()
            assert_dropped(sync_conn)
        finally:
            # Restore in `finally` so a failing assertion cannot leak a schema or
            # leave the pooled connection's search_path pointing at a dropped one.
            migration.op = old_op
            sync_conn.execute(text("SET search_path TO public"))
            sync_conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))

    async with engine.begin() as conn:
        await conn.run_sync(run)
