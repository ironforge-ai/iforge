"""IronHermes usage telemetry — the `usage_events` child table, the one new
session-level scalar (`sessions.tool_call_count`), and the replace-by-session
ingest endpoint.

Every storage decision this phase locked is pinned here rather than left to
review:

* D-15 — cost and cache-creation live on the EVENT row at per-model grain.
  `SessionCreate` must carry neither. Asserted structurally over
  `model_fields`, not by reading the file.
* D-16 — `tool_call_count` is the one recorded IronHermes value with no
  per-event analogue, so it is the ONE new session-level scalar. It must
  survive an UPDATE as well as an INSERT, which is the half that a
  create-then-read test cannot see.
* D-17 — cost is an integer micros column stored verbatim. No `Numeric`, no
  decimal conversion at any boundary.
* D-18 — `error_kind` and `provider` are free-text and blank-tolerant. Failed
  calls and blank-provider calls still burned tokens.
* D-22 — ingest is replace-by-session keyed on a per-session event-set hash,
  never append. The source table's id is a local autoincrement a store rebuild
  can renumber, so it is never used as a dedupe key.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NamedTuple

import httpx
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from pydantic import ValidationError
from sqlalchemy import CheckConstraint, delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.models.companion_installation import CompanionInstallation
from app.models.session import Session
from app.models.usage_event import UsageEvent
from app.schemas.session import SessionCreate
from app.schemas.usage_event import (
    USAGE_EVENT_BATCH_MAX,
    UsageEventBatchRequest,
    UsageEventCreate,
)

_VERSIONS_DIR = Path(__file__).resolve().parents[1] / "alembic" / "versions"

# The head that existed immediately before this plan's revision. The revision
# added here must chain off exactly this, or the history branches.
_PREVIOUS_HEAD = "a4f9c2e7d1b6"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


async def _register_env(client: httpx.AsyncClient, machine_id: str) -> str:
    r = await client.post(
        "/v1/environments",
        json={
            "machine_id": machine_id,
            "machine_name": f"mac-{machine_id}",
            "agent_type": "ironhermes",
            "agent_version": "0.1.0",
            "os": "darwin",
        },
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _post_session(
    client: httpx.AsyncClient,
    env_id: str,
    local_session_id: str,
    **fields: object,
) -> httpx.Response:
    payload: dict[str, object] = {
        "environment_id": env_id,
        "local_session_id": local_session_id,
        "started_at": datetime.now(UTC).isoformat(),
        "message_count": 1,
    }
    payload.update(fields)
    return await client.post("/v1/sessions/batch", json={"sessions": [payload]})


async def _read_session_item(client: httpx.AsyncClient, local_session_id: str) -> dict:
    listing = (await client.get("/v1/sessions")).json()
    matches = [i for i in listing["items"] if i["local_session_id"] == local_session_id]
    assert matches, listing
    return matches[0]


async def _seed_session(
    db_session: AsyncSession,
    user_id: uuid.UUID,
    *,
    local_session_id: str,
) -> Session:
    """A committed `sessions` row usable as a usage_events FK target."""
    session = Session(
        user_id=user_id,
        environment_id=None,
        local_session_id=local_session_id,
        started_at=datetime.now(UTC),
    )
    db_session.add(session)
    await db_session.commit()
    await db_session.refresh(session)
    return session


def _event(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "ts": datetime.now(UTC).isoformat(),
        "provider": "anthropic",
        "model": "claude-opus-4",
        "in_tok": 100,
        "out_tok": 50,
        "cache_read": 10,
        "cache_create": 5,
        "cost_usd_micros": 1245129,
    }
    body.update(overrides)
    return body


def _revision_file() -> Path:
    """The single revision carrying BOTH schema changes."""
    matches = [p for p in _VERSIONS_DIR.glob("*.py") if "tool_call_count" in p.read_text()]
    assert len(matches) == 1, [p.name for p in matches]
    return matches[0]


# --------------------------------------------------------------------------
# Test 1 — the migration: one revision, both changes, real downgrade
# --------------------------------------------------------------------------


def test_one_revision_carries_both_schema_changes():
    """`usage_events` and `sessions.tool_call_count` are one decision split
    across two grains (D-15/D-16). A reviewer reading the history should see
    one move, not two unrelated ones."""
    source = _revision_file().read_text()
    assert "usage_events" in source
    assert "tool_call_count" in source


def test_revision_chains_off_the_head_that_preceded_it():
    source = _revision_file().read_text()
    match = re.search(r"^down_revision[^=]*=\s*[\"']([0-9a-zA-Z_]+)[\"']", source, re.M)
    assert match is not None, source[:400]
    assert match.group(1) == _PREVIOUS_HEAD


def test_revision_downgrade_drops_both_the_table_and_the_column():
    """A migration whose downgrade is `pass` is not reversible, and the
    round-trip assertion below would silently pass on an empty function."""
    source = _revision_file().read_text()
    downgrade = source.split("def downgrade()", 1)[1]
    assert "drop_table" in downgrade
    assert "usage_events" in downgrade
    assert "drop_column" in downgrade
    assert "tool_call_count" in downgrade


class _Fingerprint(NamedTuple):
    """The four catalog shapes the revision is responsible for.

    A tuple rather than a dict so the whole schema compares with one `==`, and
    so a field added here cannot be silently dropped from that comparison.
    """

    columns: list[tuple[str, str, str, str | None]]
    indexes: list[tuple[str, str]]
    delete_rule: str | None
    tool_call_count: tuple[str, str, str | None] | None


def _schema_fingerprint(sync_conn, schema: str) -> _Fingerprint:
    """Every object the revision creates, read back out of the catalog.

    Captured on both sides of the round trip, so an object the downgrade drops
    but the re-upgrade rebuilds *differently* surfaces as an inequality. A test
    that only re-ran `upgrade()` and checked it did not raise would miss that.
    """
    columns = sync_conn.execute(
        text(
            """
            SELECT column_name, data_type, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_schema = :schema AND table_name = 'usage_events'
            ORDER BY column_name
            """
        ),
        {"schema": schema},
    ).all()
    indexes = sync_conn.execute(
        text(
            """
            SELECT indexname, indexdef FROM pg_indexes
            WHERE schemaname = :schema AND tablename = 'usage_events'
            ORDER BY indexname
            """
        ),
        {"schema": schema},
    ).all()
    delete_rule = sync_conn.execute(
        text(
            """
            SELECT rc.delete_rule
            FROM information_schema.referential_constraints rc
            JOIN information_schema.key_column_usage kcu
              ON kcu.constraint_name = rc.constraint_name
             AND kcu.constraint_schema = rc.constraint_schema
            WHERE kcu.table_schema = :schema
              AND kcu.table_name = 'usage_events'
              AND kcu.column_name = 'session_id'
            """
        ),
        {"schema": schema},
    ).scalar()
    tool_call_count = sync_conn.execute(
        text(
            """
            SELECT data_type, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_schema = :schema
              AND table_name = 'sessions'
              AND column_name = 'tool_call_count'
            """
        ),
        {"schema": schema},
    ).one_or_none()
    return _Fingerprint(
        columns=[tuple(row) for row in columns],
        indexes=[tuple(row) for row in indexes],
        delete_rule=delete_rule,
        tool_call_count=tuple(tool_call_count) if tool_call_count is not None else None,
    )


async def test_migration_round_trip_leaves_the_schema_unchanged(engine: AsyncEngine):
    """`upgrade` → `downgrade` → `upgrade` restores an identical schema.

    The source-text assertion above proves `downgrade()` is not `pass`. It
    cannot prove those statements execute, that the objects actually disappear,
    or that re-upgrading rebuilds them the same way — the half
    `02-VERIFICATION.md` recorded as behaviourally unverified. This runs the
    real revision against the real database inside a throwaway schema, so it
    never touches `public`.
    """
    spec = importlib.util.spec_from_file_location("usage_events_migration", _revision_file())
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    schema = f"usage_events_rt_{uuid.uuid4().hex}"

    def run(sync_conn):
        old_op = migration.op
        sync_conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        sync_conn.execute(text(f'SET search_path TO "{schema}"'))
        try:
            # Only the foreign-key target is needed; the revision builds the rest.
            sync_conn.execute(text("CREATE TABLE sessions (id uuid PRIMARY KEY)"))
            migration.op = Operations(MigrationContext.configure(sync_conn))

            migration.upgrade()
            applied = _schema_fingerprint(sync_conn, schema)
            assert applied.columns, "upgrade() did not create usage_events"
            assert applied.delete_rule == "CASCADE"
            assert [name for name, _ in applied.indexes] == [
                "ix_usage_events_session_id",
                "ix_usage_events_ts",
                "usage_events_pkey",
            ]
            assert applied.tool_call_count is not None

            migration.downgrade()
            reverted = _schema_fingerprint(sync_conn, schema)
            # Asserted positively — each object is gone, not merely "nothing raised".
            assert reverted.columns == []
            assert reverted.indexes == []
            assert reverted.delete_rule is None
            assert reverted.tool_call_count is None

            migration.upgrade()
            assert _schema_fingerprint(sync_conn, schema) == applied
        finally:
            # Restore in `finally` so a failing assertion cannot leak a schema or
            # leave the pooled connection's search_path pointing at a dropped one.
            migration.op = old_op
            sync_conn.execute(text("SET search_path TO public"))
            sync_conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))

    async with engine.begin() as conn:
        await conn.run_sync(run)


async def test_usage_events_table_is_live_with_a_cascade_foreign_key(db_session: AsyncSession):
    """The applied schema, read back from the live database — not from the
    model file, which compiles whether or not the migration ever ran."""
    rule = await db_session.scalar(
        text(
            """
            SELECT rc.delete_rule
            FROM information_schema.referential_constraints rc
            JOIN information_schema.key_column_usage kcu
              ON kcu.constraint_name = rc.constraint_name
            WHERE kcu.table_name = 'usage_events'
              AND kcu.column_name = 'session_id'
            """
        )
    )
    assert rule == "CASCADE"


async def test_sessions_tool_call_count_column_is_live_with_a_zero_default(
    db_session: AsyncSession,
):
    default = await db_session.scalar(
        text(
            "SELECT column_default FROM information_schema.columns "
            "WHERE table_name = 'sessions' AND column_name = 'tool_call_count'"
        )
    )
    assert default is not None
    assert "0" in default


# --------------------------------------------------------------------------
# Tests 2-3 — foreign key and cascade
# --------------------------------------------------------------------------


async def test_event_for_an_unknown_session_is_rejected_by_the_foreign_key(
    db_session: AsyncSession,
):
    """The source table declares no foreign key and real orphan rows exist
    locally. Clawdi's copy has a real one; orphans are filtered client-side
    (plan 02-05) precisely so this constraint can stay real."""
    db_session.add(
        UsageEvent(
            session_id=uuid.uuid4(),
            ts=datetime.now(UTC),
            provider="anthropic",
            model="claude-opus-4",
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.committed_db
async def test_deleting_a_session_cascades_away_its_events(db_session: AsyncSession, seed_user):
    session = await _seed_session(db_session, seed_user.id, local_session_id="cascade-1")
    db_session.add(
        UsageEvent(
            session_id=session.id,
            ts=datetime.now(UTC),
            provider="anthropic",
            model="claude-opus-4",
        )
    )
    await db_session.commit()

    assert (
        await db_session.scalar(
            select(func.count()).select_from(UsageEvent).where(UsageEvent.session_id == session.id)
        )
        == 1
    )

    await db_session.execute(delete(Session).where(Session.id == session.id))
    await db_session.commit()

    assert (
        await db_session.scalar(
            select(func.count()).select_from(UsageEvent).where(UsageEvent.session_id == session.id)
        )
        == 0
    )


# --------------------------------------------------------------------------
# Tests 4-7 — blank-tolerant provider, open error vocabulary, verbatim cost
# --------------------------------------------------------------------------


async def test_blank_provider_is_stored_not_rejected(db_session: AsyncSession, seed_user):
    """The source column is `provider TEXT NOT NULL` yet 56 live rows hold the
    empty string. Those calls burned real tokens and real money; rejecting
    them would make the totals wrong (D-18)."""
    session = await _seed_session(db_session, seed_user.id, local_session_id="blank-provider")
    event = UsageEvent(
        session_id=session.id,
        ts=datetime.now(UTC),
        provider="",
        model="claude-opus-4",
    )
    db_session.add(event)
    await db_session.commit()
    await db_session.refresh(event)
    assert event.provider == ""


async def test_null_provider_is_stored(db_session: AsyncSession, seed_user):
    session = await _seed_session(db_session, seed_user.id, local_session_id="null-provider")
    event = UsageEvent(
        session_id=session.id,
        ts=datetime.now(UTC),
        provider=None,
        model="claude-opus-4",
    )
    db_session.add(event)
    await db_session.commit()
    await db_session.refresh(event)
    assert event.provider is None


async def test_unknown_error_kind_is_stored_unchanged(db_session: AsyncSession, seed_user):
    """The recorded design named 4 error kinds; the live store already carries
    9, and IronHermes owns and extends that vocabulary. An enum or CHECK here
    would silently reject rows for real usage (D-18, Pitfall 10)."""
    session = await _seed_session(db_session, seed_user.id, local_session_id="error-kind")
    novel = "quota_exhausted_upstream_regional_failover"
    db_session.add(
        UsageEvent(
            session_id=session.id,
            ts=datetime.now(UTC),
            provider="anthropic",
            model="claude-opus-4",
            error_kind=None,
        )
    )
    with_kind = UsageEvent(
        session_id=session.id,
        ts=datetime.now(UTC),
        provider="anthropic",
        model="claude-opus-4",
        error_kind=novel,
    )
    db_session.add(with_kind)
    await db_session.commit()
    await db_session.refresh(with_kind)
    assert with_kind.error_kind == novel


def test_usage_event_model_declares_no_enum_shaped_constraint():
    """The repo convention for enum-shaped columns is `String` + CHECK. `error_kind`
    deliberately breaks it, so the absence must be asserted rather than left to a
    reader assuming the constraint was forgotten.

    Plan 06-21 added the table's first CHECKs -- the ledger `dimension` and its row
    shape, a vocabulary iforge owns -- so this asserts that none of them names
    `error_kind`, rather than that the table has no CHECK at all."""
    source = (Path(__file__).resolve().parents[1] / "app/models/usage_event.py").read_text()
    checks = [
        constraint
        for constraint in UsageEvent.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    ]
    assert {constraint.name for constraint in checks} == {
        "ck_usage_events_dimension",
        "ck_usage_events_dimension_shape",
    }
    assert all("error_kind" not in str(constraint.sqltext) for constraint in checks)
    assert "Literal[" not in source
    assert "Numeric" not in source


async def test_cost_micros_round_trips_as_an_exact_integer(db_session: AsyncSession, seed_user):
    """Integer micros, verbatim. IronHermes computed the figure at request
    time under the pricing then in effect; a later recompute cannot reproduce
    it, and a float/decimal boundary would not reproduce it either (D-17)."""
    session = await _seed_session(db_session, seed_user.id, local_session_id="cost-micros")
    big = 9_876_543_210_123
    event = UsageEvent(
        session_id=session.id,
        ts=datetime.now(UTC),
        provider="anthropic",
        model="claude-opus-4",
        cost_usd_micros=big,
    )
    db_session.add(event)
    await db_session.commit()
    await db_session.refresh(event)
    assert event.cost_usd_micros == big
    assert isinstance(event.cost_usd_micros, int)

    small = await db_session.scalar(
        select(UsageEvent.cost_usd_micros).where(UsageEvent.id == event.id)
    )
    assert small == big


# --------------------------------------------------------------------------
# Tests 8-10 — request-schema bounds
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field",
    ["in_tok", "out_tok", "cache_read", "cache_create", "cost_usd_micros"],
)
def test_negative_observables_are_rejected(field: str):
    """A negative cost corrupts a future aggregate as surely as a negative
    token count, and is the one that is easy to forget."""
    with pytest.raises(ValidationError):
        UsageEventCreate(**{**_event(), field: -1})


def test_batch_reuses_the_session_id_constraint_verbatim():
    """A colon-bearing id must 422. `local_session_id` flows into an
    object-store key path; relaxing the pattern server-side is how a client
    escapes its own tenant prefix. The IronHermes transform belongs on the
    client (plan 02-01)."""
    with pytest.raises(ValidationError):
        UsageEventBatchRequest(
            environment_id=uuid.uuid4(),
            local_session_id="agent:main:web:dm:abc",
            event_set_hash="a" * 64,
            events=[UsageEventCreate(**_event())],
        )

    ok = UsageEventBatchRequest(
        environment_id=uuid.uuid4(),
        local_session_id="agent.main.web.dm.abc",
        event_set_hash="a" * 64,
        events=[UsageEventCreate(**_event())],
    )
    assert ok.local_session_id == "agent.main.web.dm.abc"


def test_batch_rejects_a_list_longer_than_the_declared_cap():
    events = [UsageEventCreate(**_event()) for _ in range(2)]
    with pytest.raises(ValidationError):
        UsageEventBatchRequest(
            environment_id=uuid.uuid4(),
            local_session_id="ok-id",
            event_set_hash="a" * 64,
            events=events * (USAGE_EVENT_BATCH_MAX // 2 + 1),
        )


def test_batch_cap_leaves_headroom_under_the_wire_parameter_ceiling():
    """~12 bound parameters per usage_events row against PostgreSQL's 32767
    parameter ceiling. Not the 500 from `SessionBatchRequest`, which was
    calibrated for a ~17-column session row."""
    assert USAGE_EVENT_BATCH_MAX * 12 < 32767


def test_free_text_fields_are_length_capped():
    with pytest.raises(ValidationError):
        UsageEventCreate(**_event(provider="p" * 500))
    with pytest.raises(ValidationError):
        UsageEventCreate(**_event(model="m" * 500))
    with pytest.raises(ValidationError):
        UsageEventCreate(**_event(error_kind="e" * 500))


# --------------------------------------------------------------------------
# Tests 11-13 — the session-level scalar, and the scalars that must NOT exist
# --------------------------------------------------------------------------


def test_session_create_carries_no_cost_and_no_cache_creation_field():
    """D-15 / Pitfall 9, machine-checked. 38 live sessions are genuinely
    multi-model; a session-level cost scalar would flatten them wrong, and
    ingesting both grains would give one question two answers."""
    names = set(SessionCreate.model_fields)
    assert not [n for n in names if "cost" in n]
    assert not [n for n in names if "creat" in n]


def test_session_create_gains_exactly_one_new_scalar():
    assert "tool_call_count" in SessionCreate.model_fields


def test_tool_call_count_rejects_a_negative_value():
    with pytest.raises(ValidationError):
        SessionCreate(
            environment_id=uuid.uuid4(),
            local_session_id="neg-tool-calls",
            started_at=datetime.now(UTC),
            tool_call_count=-1,
        )


def test_tool_call_count_defaults_to_zero_when_omitted():
    """Claude Code, Codex, OpenClaw and Hermes have no such counter and will
    never send the field. A required field here would 422 every push from
    every other runtime."""
    model = SessionCreate(
        environment_id=uuid.uuid4(),
        local_session_id="no-tool-calls",
        started_at=datetime.now(UTC),
    )
    assert model.tool_call_count == 0


# --------------------------------------------------------------------------
# Tests 14-15 — the round trip through BOTH halves of the batch upsert
# --------------------------------------------------------------------------


async def test_tool_call_count_round_trips_on_insert(client: httpx.AsyncClient):
    env = await _register_env(client, f"tcc-insert-{uuid.uuid4().hex[:8]}")
    r = await _post_session(client, env, "tcc-insert", tool_call_count=137)
    assert r.status_code == 200, r.text

    item = await _read_session_item(client, "tcc-insert")
    assert item["tool_call_count"] == 137


async def test_tool_call_count_round_trips_on_update(client: httpx.AsyncClient):
    """The two-halves catch. `batch_create_sessions` names every persisted
    column twice — the `rows` values dict and the `on_conflict_do_update`
    `set_`. A column added only to the first is written on INSERT and then
    silently frozen on every subsequent push. Only this assertion sees it."""
    env = await _register_env(client, f"tcc-update-{uuid.uuid4().hex[:8]}")
    assert (await _post_session(client, env, "tcc-update", tool_call_count=137)).status_code == 200
    assert (await _read_session_item(client, "tcc-update"))["tool_call_count"] == 137

    assert (await _post_session(client, env, "tcc-update", tool_call_count=204)).status_code == 200
    item = await _read_session_item(client, "tcc-update")
    assert item["tool_call_count"] == 204


async def test_session_batch_still_accepts_a_body_omitting_tool_call_count(
    client: httpx.AsyncClient,
):
    env = await _register_env(client, f"tcc-absent-{uuid.uuid4().hex[:8]}")
    r = await _post_session(client, env, "tcc-absent")
    assert r.status_code == 200, r.text
    assert (await _read_session_item(client, "tcc-absent"))["tool_call_count"] == 0


# --------------------------------------------------------------------------
# Task 2 — the ingest endpoint
# --------------------------------------------------------------------------

_INGEST_PATH = "/v1/sessions/usage-events"


def _batch(env_id: str, local_session_id: str, event_set_hash: str, events: list) -> dict:
    return {
        "environment_id": env_id,
        "local_session_id": local_session_id,
        "event_set_hash": event_set_hash,
        "events": events,
    }


async def _stored_events(db_session: AsyncSession, local_session_id: str) -> list[UsageEvent]:
    rows = await db_session.execute(
        select(UsageEvent)
        .join(Session, Session.id == UsageEvent.session_id)
        .where(Session.local_session_id == local_session_id)
        .order_by(UsageEvent.ts)
    )
    return list(rows.scalars().all())


async def test_ingest_persists_every_row_for_an_owned_session(
    client: httpx.AsyncClient, db_session: AsyncSession
):
    env = await _register_env(client, f"ingest-{uuid.uuid4().hex[:8]}")
    assert (await _post_session(client, env, "ingest-1")).status_code == 200

    base = datetime.now(UTC)
    events = [
        _event(ts=(base + timedelta(seconds=i)).isoformat(), in_tok=i, cost_usd_micros=i * 1000)
        for i in range(3)
    ]
    r = await client.post(_INGEST_PATH, json=_batch(env, "ingest-1", "h" * 64, events))
    assert r.status_code == 200, r.text
    assert r.json()["inserted"] == 3

    stored = await _stored_events(db_session, "ingest-1")
    assert len(stored) == 3
    assert [e.in_tok for e in stored] == [0, 1, 2]
    assert [e.cost_usd_micros for e in stored] == [0, 1000, 2000]


async def test_reposting_the_identical_set_is_a_no_op(
    client: httpx.AsyncClient, db_session: AsyncSession
):
    """Never append. The source event id is a local autoincrement a store
    rebuild or vacuum can renumber, so appending would silently double-count
    recorded spend (D-22)."""
    env = await _register_env(client, f"idem-{uuid.uuid4().hex[:8]}")
    assert (await _post_session(client, env, "idem-1")).status_code == 200

    body = _batch(env, "idem-1", "h" * 64, [_event(), _event(model="gpt-5")])
    first = await client.post(_INGEST_PATH, json=body)
    assert first.status_code == 200, first.text
    assert first.json()["inserted"] == 2
    assert first.json()["unchanged"] is False

    second = await client.post(_INGEST_PATH, json=body)
    assert second.status_code == 200, second.text
    assert second.json()["unchanged"] is True
    assert second.json()["inserted"] == 0

    assert len(await _stored_events(db_session, "idem-1")) == 2


async def test_a_changed_set_replaces_the_sessions_rows_entirely(
    client: httpx.AsyncClient, db_session: AsyncSession
):
    env = await _register_env(client, f"replace-{uuid.uuid4().hex[:8]}")
    assert (await _post_session(client, env, "replace-1")).status_code == 200

    await client.post(
        _INGEST_PATH,
        json=_batch(env, "replace-1", "a" * 64, [_event(model="old-a"), _event(model="old-b")]),
    )
    r = await client.post(
        _INGEST_PATH,
        json=_batch(env, "replace-1", "b" * 64, [_event(model="new-only")]),
    )
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] == 2
    assert r.json()["inserted"] == 1

    stored = await _stored_events(db_session, "replace-1")
    assert [e.model for e in stored] == ["new-only"]


async def test_ingest_for_one_session_never_touches_another(
    client: httpx.AsyncClient, db_session: AsyncSession
):
    env = await _register_env(client, f"isolate-{uuid.uuid4().hex[:8]}")
    assert (await _post_session(client, env, "iso-a")).status_code == 200
    assert (await _post_session(client, env, "iso-b")).status_code == 200

    await client.post(_INGEST_PATH, json=_batch(env, "iso-a", "a" * 64, [_event(model="a-only")]))
    await client.post(_INGEST_PATH, json=_batch(env, "iso-b", "b" * 64, [_event(model="b-only")]))
    # Replacing A's set wholesale must not disturb B.
    await client.post(_INGEST_PATH, json=_batch(env, "iso-a", "c" * 64, [_event(model="a-new")]))

    assert [e.model for e in await _stored_events(db_session, "iso-a")] == ["a-new"]
    assert [e.model for e in await _stored_events(db_session, "iso-b")] == ["b-only"]


async def test_session_ingest_never_deletes_dimension_rows(
    client: httpx.AsyncClient, db_session: AsyncSession, seed_user
):
    """Replace-by-session ingest deletes one session's llm rows and nothing else (Pitfall 8).

    A companion's compute and storage rows live in the same table (plan 06-21) but carry no
    session, so the ingest's `WHERE session_id = :session` delete can never match one -- asserted
    here against a real replace, on the same agent the companion is installed on."""
    env = await _register_env(client, f"dims-{uuid.uuid4().hex[:8]}")
    assert (await _post_session(client, env, "dims-1")).status_code == 200
    first = await client.post(
        _INGEST_PATH, json=_batch(env, "dims-1", "a" * 64, [_event(model="old")])
    )
    assert first.status_code == 200, first.text

    label = f"postiz-{uuid.uuid4().hex[:12]}"
    installation = CompanionInstallation(
        user_id=seed_user.id,
        environment_id=uuid.UUID(env),
        companion_key="postiz",
        isolation_tier="standard",
        status="complete",
        version="v2.24.0",
        pinned_digest=hashlib.sha256(label.encode()).hexdigest(),
        hostname_label=label,
        hostname=f"{label}.companions.example",
        owner_email="owner@companions.example",
    )
    db_session.add(installation)
    await db_session.flush()
    period = datetime(2026, 9, 3, tzinfo=UTC)
    for dimension, quantity in (("compute", 1), ("storage", 2**30)):
        db_session.add(
            UsageEvent(
                dimension=dimension,
                user_id=seed_user.id,
                environment_id=installation.environment_id,
                installation_id=installation.id,
                companion_key="postiz",
                period_start=period,
                ts=period,
                model="postiz/standard",
                quantity=quantity,
                cost_usd_micros=41_666,
            )
        )
    await db_session.commit()

    replaced = await client.post(
        _INGEST_PATH, json=_batch(env, "dims-1", "b" * 64, [_event(model="new")])
    )
    assert replaced.status_code == 200, replaced.text
    assert replaced.json()["deleted"] == 1
    assert replaced.json()["inserted"] == 1

    assert [e.model for e in await _stored_events(db_session, "dims-1")] == ["new"]
    survivors = (
        await db_session.scalars(
            select(UsageEvent.dimension)
            .where(UsageEvent.installation_id == installation.id)
            .order_by(UsageEvent.dimension)
        )
    ).all()
    assert list(survivors) == ["compute", "storage"]


async def test_empty_event_list_short_circuits_to_a_well_formed_response(
    client: httpx.AsyncClient,
):
    env = await _register_env(client, f"empty-{uuid.uuid4().hex[:8]}")
    r = await client.post(_INGEST_PATH, json=_batch(env, "never-created", "h" * 64, []))
    assert r.status_code == 200, r.text
    assert r.json() == {"inserted": 0, "deleted": 0, "unchanged": True}


async def test_ingest_for_a_nonexistent_session_is_a_client_error(client: httpx.AsyncClient):
    env = await _register_env(client, f"missing-{uuid.uuid4().hex[:8]}")
    r = await client.post(_INGEST_PATH, json=_batch(env, "no-such-session", "h" * 64, [_event()]))
    assert 400 <= r.status_code < 500, r.text
    assert r.json()["detail"]["code"] == "unknown_session"


async def test_a_mixed_batch_of_awkward_real_rows_is_accepted_in_full(
    client: httpx.AsyncClient, db_session: AsyncSession
):
    """Blank provider, an error kind in no enumeration anywhere, and a large
    integer cost — all three are real shapes in the live store."""
    env = await _register_env(client, f"mixed-{uuid.uuid4().hex[:8]}")
    assert (await _post_session(client, env, "mixed-1")).status_code == 200

    base = datetime.now(UTC)
    events = [
        _event(ts=base.isoformat(), provider="", model="claude-opus-4"),
        _event(
            ts=(base + timedelta(seconds=1)).isoformat(),
            error_kind="regional_failover_after_quota",
        ),
        _event(ts=(base + timedelta(seconds=2)).isoformat(), cost_usd_micros=9_876_543_210_123),
    ]
    r = await client.post(_INGEST_PATH, json=_batch(env, "mixed-1", "h" * 64, events))
    assert r.status_code == 200, r.text
    assert r.json()["inserted"] == 3

    stored = await _stored_events(db_session, "mixed-1")
    assert stored[0].provider == ""
    assert stored[1].error_kind == "regional_failover_after_quota"
    assert stored[2].cost_usd_micros == 9_876_543_210_123


async def test_lone_surrogates_are_stripped_not_500(
    client: httpx.AsyncClient, db_session: AsyncSession
):
    """Lone UTF-16 surrogates must be stripped on ingest (T-02-20).

    `schemas/session.py:24-33` already carries `_LONE_SURROGATE_RE`, and its
    comment records why: a lone surrogate is a valid Python `str` but cannot be
    encoded as UTF-8, so asyncpg rejects the bound parameter and 500s the whole
    batch — and "a stuck daemon retried the bad payload ~34k times in prod
    before we shipped the fix".

    The lesson that comment records is that client-side codepoint truncation is
    NOT sufficient; the server-side strip is what ended the incident. This
    endpoint has the identical permanent-retry shape (push.ts records no
    event-set hash on failure), so it needs the identical control.
    """
    env = await _register_env(client, f"surrogate-{uuid.uuid4().hex[:8]}")
    assert (await _post_session(client, env, "surrogate-1")).status_code == 200

    # Posted as RAW JSON TEXT, not via `json=`. httpx would refuse to encode a
    # lone surrogate into the request body and the test would pass vacuously on
    # a client-side error. The real producer is the Bun/TypeScript CLI, whose
    # UTF-16 strings serialise a lone surrogate to the perfectly valid JSON
    # escape `\ud800` — the server then decodes it back into a lone surrogate.
    # Sending the escape sequence literally reproduces that exact path.
    events = [
        _event(
            ts=datetime.now(UTC).isoformat(),
            provider="anthro__HI__pic",
            model="claude-opus__LO__-4",
            error_kind="quota__HI___exceeded",
        )
    ]
    raw = json.dumps(_batch(env, "surrogate-1", "s" * 64, events))
    raw = raw.replace("__HI__", r"\ud800").replace("__LO__", r"\udfff")
    r = await client.post(
        _INGEST_PATH, content=raw.encode("utf-8"), headers={"content-type": "application/json"}
    )
    assert r.status_code == 200, r.text

    stored = await _stored_events(db_session, "surrogate-1")
    assert len(stored) == 1
    # Stripped, not escaped and not rejected — and the surrounding text survives.
    assert stored[0].provider == "anthropic"
    assert stored[0].model == "claude-opus-4"
    assert stored[0].error_kind == "quota_exceeded"


async def test_blank_model_is_ingested_not_rejected(
    client: httpx.AsyncClient, db_session: AsyncSession
):
    """`model` must fail open exactly as `provider` does (D-18, 02-REVIEW CR-01).

    The IronHermes source column is `model TEXT NOT NULL`, and NOT NULL does not
    forbid the empty string — the same shape that leaves 56 live rows with a blank
    `provider`. The adapter emits `model: e.model ?? ""` and the whole batch is one
    request, so a single blank model rejecting the request loses every OTHER event
    in it too. Worse, the CLI never records the event-set hash on failure, so the
    push retries and fails identically forever.

    A call with an unrecorded model still burned tokens and still cost money.
    Dropping it makes the totals wrong in the user's favour, which is the harder
    error to notice — the exact reasoning D-18 applies to `provider`.
    """
    env = await _register_env(client, f"blankmodel-{uuid.uuid4().hex[:8]}")
    assert (await _post_session(client, env, "blank-model-1")).status_code == 200

    base = datetime.now(UTC)
    events = [
        _event(ts=base.isoformat(), model=""),
        _event(ts=(base + timedelta(seconds=1)).isoformat(), model="claude-opus-4"),
    ]
    r = await client.post(_INGEST_PATH, json=_batch(env, "blank-model-1", "m" * 64, events))
    assert r.status_code == 200, r.text
    assert r.json()["inserted"] == 2, "a blank model must not reject its batch-mates"

    stored = await _stored_events(db_session, "blank-model-1")
    assert stored[0].model == ""
    assert stored[1].model == "claude-opus-4"


# --- access control -------------------------------------------------------


@pytest.fixture
def bound_cli_client(db_session: AsyncSession, seed_user):
    """A CLI principal bound to exactly one environment, mirroring a deployed
    Agent API key. Yields a factory so the test picks the binding."""
    import httpx as _httpx
    from httpx import ASGITransport

    from app.core.auth import AuthContext, get_auth, get_auth_short_session
    from app.core.database import get_session
    from app.main import app
    from app.models.api_key import ApiKey
    from tests.conftest import _dependency_overrides

    def _make(*, environment_id=None, scopes=None):
        async def _override_get_session():
            yield db_session

        async def _override_get_auth() -> AuthContext:
            return AuthContext(
                user=seed_user,
                api_key=ApiKey(
                    user_id=seed_user.id,
                    environment_id=environment_id,
                    scopes=scopes,
                ),
            )

        overrides = {
            get_session: _override_get_session,
            get_auth: _override_get_auth,
            get_auth_short_session: _override_get_auth,
        }
        ctx = _dependency_overrides(overrides)
        ctx.__enter__()
        transport = ASGITransport(app=app)
        return ctx, _httpx.AsyncClient(transport=transport, base_url="http://test")

    return _make


async def test_bound_principal_cannot_write_events_under_another_environment(
    client: httpx.AsyncClient, db_session: AsyncSession, bound_cli_client
):
    """Matches the shipped session-batch behaviour: the WHOLE batch is
    refused, never partially accepted."""
    env_a = await _register_env(client, f"bind-a-{uuid.uuid4().hex[:8]}")
    env_b = await _register_env(client, f"bind-b-{uuid.uuid4().hex[:8]}")
    assert (await _post_session(client, env_b, "bound-1")).status_code == 200

    ctx, bound = bound_cli_client(environment_id=uuid.UUID(env_a))
    try:
        async with bound:
            r = await bound.post(
                _INGEST_PATH, json=_batch(env_b, "bound-1", "h" * 64, [_event(), _event()])
            )
    finally:
        ctx.__exit__(None, None, None)

    assert r.status_code == 403, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "env_binding_violation"
    assert detail["bound_environment_id"] == env_a
    assert env_b in detail["offending_environment_ids"]

    # Nothing was persisted — refusal is whole-batch, not partial.
    assert await _stored_events(db_session, "bound-1") == []


async def test_principal_without_sessions_write_scope_is_refused(
    client: httpx.AsyncClient, bound_cli_client
):
    env = await _register_env(client, f"scope-{uuid.uuid4().hex[:8]}")
    assert (await _post_session(client, env, "scope-1")).status_code == 200

    ctx, scoped = bound_cli_client(scopes=["sessions:read"])
    try:
        async with scoped:
            r = await scoped.post(_INGEST_PATH, json=_batch(env, "scope-1", "h" * 64, [_event()]))
    finally:
        ctx.__exit__(None, None, None)

    assert r.status_code == 403, r.text


def test_ingest_route_is_published_on_the_canonical_v1_surface():
    """Asserted over the OpenAPI schema rather than `app.routes`: this
    FastAPI version stores includes as lazy `_IncludedRouter` wrappers with
    no `.path`, and the schema is the contract clients actually generate
    from anyway."""
    from app.main import app

    paths = app.openapi()["paths"]
    assert _INGEST_PATH in paths
    assert "post" in paths[_INGEST_PATH]


async def test_structured_model_blob_is_cleaned_not_stored_verbatim(
    client: httpx.AsyncClient, db_session: AsyncSession
):
    """The event grain must clean a structured model blob, as the session grain does (T-02-26).

    T-02-26's declared mitigation is TWO controls — "model-field parser + server
    model-value cleaner". The adapter-side parser shipped first; this is the
    server half, and it is not redundant with it.

    The source column can hold IronHermes's whole model config, which carries
    provider headers including API keys, and the dashboard renders this field.
    `schemas/session.py` has cleaned the session-grain `model` since before this
    phase (`_clean_model_value`); until this test, the event grain stored the blob
    verbatim. Any principal with `sessions:write` could reach it — including an
    older CLI build that predates the adapter-side parse entirely, which is
    precisely the case a server-side control exists to cover.
    """
    env = await _register_env(client, f"blob-{uuid.uuid4().hex[:8]}")
    assert (await _post_session(client, env, "blob-1")).status_code == 200

    secret = "sk-live-DO-NOT-STORE-8f3a2b1c"
    blob = (
        '{"default": "gpt-5.3-codex", "provider": "openai-codex", '
        '"base_url": "https://example.invalid/v1", '
        f'"headers": {{"x-api-key": "{secret}"}}}}'
    )

    r = await client.post(
        _INGEST_PATH,
        json=_batch(env, "blob-1", "b" * 64, [_event(model=blob)]),
    )
    assert r.status_code == 200, r.text

    stored = await _stored_events(db_session, "blob-1")
    assert len(stored) == 1
    assert stored[0].model == "gpt-5.3-codex"
    # The assertion that actually matters: the secret is nowhere in the row.
    assert secret not in stored[0].model


async def test_whitespace_prefixed_model_blob_is_still_cleaned(
    client: httpx.AsyncClient, db_session: AsyncSession
):
    """A leading space must not smuggle the blob past the cleaner (T-02-26).

    This is the exact shape the two layers disagree on, and the reason they were
    never redundant. The CLI's `parseModelField` tests `raw.startsWith("{")` with
    NO `.strip()`, so a leading space, newline or tab returns the raw blob
    untouched. The server's `_clean_model_value` strips FIRST and only then tests
    `startswith("{")`. Remove the server layer and this input leaks; remove the
    `.strip()` from the server layer and it leaks too.
    """
    env = await _register_env(client, f"wsblob-{uuid.uuid4().hex[:8]}")
    assert (await _post_session(client, env, "wsblob-1")).status_code == 200

    secret = "sk-live-WHITESPACE-BYPASS-4d9e"
    blob = f'  \n\t{{"default": "claude-opus-4", "headers": {{"x-api-key": "{secret}"}}}}'

    r = await client.post(
        _INGEST_PATH,
        json=_batch(env, "wsblob-1", "w" * 64, [_event(model=blob)]),
    )
    assert r.status_code == 200, r.text

    stored = await _stored_events(db_session, "wsblob-1")
    assert stored[0].model == "claude-opus-4"
    assert secret not in stored[0].model


async def test_oversized_model_blob_is_cleaned_rather_than_422d(
    client: httpx.AsyncClient, db_session: AsyncSession
):
    """A blob longer than the 200-char cap must clean down, not reject the batch.

    This is why the cleaner runs `mode="before"`. `model` is
    `Annotated[str, StringConstraints(max_length=200)]`, and pydantic-core enforces
    that cap BEFORE any after-validator runs — so an after-validator would never
    see a long blob, the whole batch would 422, and `push.ts` records no event-set
    hash on failure, so the daemon would re-send it forever. Same permanent-retry
    shape the surrogate incident had.

    A genuinely long model NAME must still be rejected: the cleaner extracts, it
    does not truncate. Length enforcement stays with StringConstraints so
    T-02-19/T-02-25's verified caps are unchanged.
    """
    env = await _register_env(client, f"bigblob-{uuid.uuid4().hex[:8]}")
    assert (await _post_session(client, env, "bigblob-1")).status_code == 200

    secret = "sk-live-OVERSIZED-" + ("z" * 300)
    blob = f'{{"default": "gpt-5.3-codex", "headers": {{"x-api-key": "{secret}"}}}}'
    assert len(blob) > 200

    r = await client.post(
        _INGEST_PATH,
        json=_batch(env, "bigblob-1", "z" * 64, [_event(model=blob)]),
    )
    assert r.status_code == 200, r.text
    stored = await _stored_events(db_session, "bigblob-1")
    assert stored[0].model == "gpt-5.3-codex"

    # The length cap itself is untouched: a long model NAME still 422s.
    assert (await _post_session(client, env, "bigblob-2")).status_code == 200
    r = await client.post(
        _INGEST_PATH,
        json=_batch(env, "bigblob-2", "y" * 64, [_event(model="custom-" + "x" * 200)]),
    )
    assert r.status_code == 422, r.text


async def test_unparseable_model_blob_fails_open_to_blank(
    client: httpx.AsyncClient, db_session: AsyncSession
):
    """A blob with no recoverable model name becomes "", never a rejection (D-18).

    The session grain may return None here because its column is nullable. The
    event column is `model TEXT NOT NULL`, and D-18 / 02-REVIEW CR-01 established
    that a blank model must fail OPEN: the batch is one request, so a single
    unrecoverable model rejecting it would discard every valid event beside it,
    and the call still burned tokens and still cost money.
    """
    env = await _register_env(client, f"noname-{uuid.uuid4().hex[:8]}")
    assert (await _post_session(client, env, "noname-1")).status_code == 200

    r = await client.post(
        _INGEST_PATH,
        json=_batch(
            env,
            "noname-1",
            "n" * 64,
            [_event(model='{"provider": "openai-codex", "api_key": "sk-live-NOMODEL-77"}')],
        ),
    )
    assert r.status_code == 200, r.text

    stored = await _stored_events(db_session, "noname-1")
    assert stored[0].model == ""
