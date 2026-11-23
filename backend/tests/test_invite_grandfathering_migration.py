"""D-07 grandfathering migration coverage.

The invite-gate revision must backfill `invite_exempted_at` and
`billing_unlocked_at` for every User row that exists at migration time,
keyed on ROW EXISTENCE ONLY — admin-created rows carry email=NULL and must
be grandfathered too (Pitfall 3) — and emit one invite.grandfathered
control-plane audit event per row.

Follows the live upgrade/downgrade round-trip pattern from
test_usage_events.py: the real revision module runs against the real
database inside a throwaway schema, so it never touches `public`.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

_VERSIONS_DIR = Path(__file__).resolve().parents[1] / "alembic" / "versions"

# The head that existed immediately before this plan's revision. The revision
# added here must chain off exactly this, or the history branches.
_PREVIOUS_HEAD = "b3d7f1a2c845"


def _revision_file() -> Path:
    """The single revision carrying the invite gate schema + backfill."""
    matches = [p for p in _VERSIONS_DIR.glob("*.py") if "invite.grandfathered" in p.read_text()]
    assert len(matches) == 1, [p.name for p in matches]
    return matches[0]


def _load_migration():
    spec = importlib.util.spec_from_file_location("invite_gate_migration", _revision_file())
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def test_revision_chains_off_the_previous_head():
    migration = _load_migration()
    assert migration.down_revision == _PREVIOUS_HEAD


async def test_grandfathering_backfills_by_row_existence_and_audits(engine: AsyncEngine):
    """Seed pre-migration users — one WITH email, one email=NULL — run the
    real upgrade, and prove both are backfilled with one audit row each.
    Then a real downgrade removes everything it added, and a re-upgrade
    converges."""
    migration = _load_migration()
    schema = f"invite_gate_rt_{uuid.uuid4().hex}"

    def run(sync_conn):
        old_op = migration.op
        sync_conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        sync_conn.execute(text(f'SET search_path TO "{schema}"'))
        try:
            # Only the tables the revision touches are needed, in minimal form.
            sync_conn.execute(text("CREATE TABLE users (id uuid PRIMARY KEY, email varchar(320))"))
            sync_conn.execute(
                text(
                    """
                    CREATE TABLE control_plane_audit_events (
                        id uuid PRIMARY KEY,
                        actor_type varchar(32) NOT NULL,
                        actor_user_id uuid,
                        target_user_id uuid,
                        source varchar(64) NOT NULL,
                        action varchar(100) NOT NULL,
                        resource_type varchar(80) NOT NULL,
                        resource_id varchar(200),
                        environment_id uuid,
                        channel_account_id uuid,
                        channel_agent_link_id uuid,
                        details jsonb NOT NULL,
                        created_at timestamptz NOT NULL,
                        updated_at timestamptz NOT NULL
                    )
                    """
                )
            )
            emailed_id = uuid.uuid4()
            emailless_id = uuid.uuid4()
            sync_conn.execute(
                text("INSERT INTO users (id, email) VALUES (:id, 'pre@clawdi.local')"),
                {"id": emailed_id},
            )
            # Admin-created row: email IS NULL — must still be grandfathered.
            sync_conn.execute(
                text("INSERT INTO users (id, email) VALUES (:id, NULL)"),
                {"id": emailless_id},
            )

            migration.op = Operations(MigrationContext.configure(sync_conn))
            migration.upgrade()

            backfilled = sync_conn.execute(
                text("SELECT id, invite_exempted_at, billing_unlocked_at FROM users ORDER BY id")
            ).all()
            assert len(backfilled) == 2
            for row in backfilled:
                assert row.invite_exempted_at is not None, row
                assert row.billing_unlocked_at is not None, row

            audits = sync_conn.execute(
                text(
                    "SELECT target_user_id FROM control_plane_audit_events "
                    "WHERE action = 'invite.grandfathered' AND source = 'migration'"
                )
            ).all()
            assert sorted(a.target_user_id for a in audits) == sorted([emailed_id, emailless_id])

            invite_codes_exists = sync_conn.execute(
                text("SELECT to_regclass(:qualified) IS NOT NULL AS present"),
                {"qualified": f'"{schema}".invite_codes'},
            ).scalar_one()
            assert invite_codes_exists is True

            migration.downgrade()

            reverted_cols = sync_conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = :schema AND table_name = 'users' "
                    "AND column_name IN ('invite_exempted_at', 'billing_unlocked_at')"
                ),
                {"schema": schema},
            ).all()
            assert reverted_cols == []
            invite_codes_gone = sync_conn.execute(
                text("SELECT to_regclass(:qualified) IS NULL AS gone"),
                {"qualified": f'"{schema}".invite_codes'},
            ).scalar_one()
            assert invite_codes_gone is True
            remaining_audits = sync_conn.execute(
                text(
                    "SELECT count(*) FROM control_plane_audit_events "
                    "WHERE action = 'invite.grandfathered'"
                )
            ).scalar_one()
            assert remaining_audits == 0

            # Re-upgrade converges: rows are grandfathered again.
            migration.upgrade()
            rebackfilled = sync_conn.execute(
                text("SELECT count(*) FROM users WHERE invite_exempted_at IS NOT NULL")
            ).scalar_one()
            assert rebackfilled == 2
        finally:
            # Restore in `finally` so a failing assertion cannot leak a schema or
            # leave the pooled connection's search_path pointing at a dropped one.
            migration.op = old_op
            sync_conn.execute(text("SET search_path TO public"))
            sync_conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))

    async with engine.begin() as conn:
        await conn.run_sync(run)
