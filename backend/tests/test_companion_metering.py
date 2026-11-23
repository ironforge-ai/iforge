"""Per-tenant companion cost attribution across compute, storage and nested LLM spend (OPS-05).

``usage_events`` is extended into the ledger (D-23) rather than a second metering path standing
up beside it. Compute accrues per whole hour at the tier price the tenant accepted (D-07); storage
accrues per UTC day from measured bytes; every recorded cost is an integer of micro-dollars fixed
when it was accrued. The read-out is honest about what nobody measured: a dimension without a
recorded row is null, never zero, and companion-originated LLM spend is not yet attributed
(PLAT-03, research A13).
"""

from __future__ import annotations

import hashlib
import inspect
import io
import json
import tarfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.companion_installation import CompanionInstallation
from app.models.project import PROJECT_KIND_PERSONAL, Project
from app.models.session import Session
from app.models.usage_event import UsageEvent
from app.models.user import User
from app.services.companion_metering import (
    CompanionMeteringWorker,
    accrue_compute,
    accrue_storage,
    measure_media_bytes,
)
from app.services.file_store import LocalFileStore
from app.services.file_store_s3 import _Boto3S3ObjectStoreClient
from app.services.tenant_export import assemble_tenant_export
from tests.conftest import create_env_with_project
from tests.test_companion_install_job import _sessionmaker
from tests.test_file_store_s3_ops import _OK_METADATA, _FakeBoto3S3Client

SPEND_PATH = "/v1/companions/spend"

# September 2026 has 30 days: 720 hours.
MONTH = "2026-09"
MONTH_START = datetime(2026, 9, 1, tzinfo=UTC)
QUOTED_MONTHLY = 30_000_000  # $30.00 a month at the accepted tier
QUOTED_STORAGE_PER_GIB_MONTH = 150_000  # $0.15 per GiB-month
HOURLY_COST = QUOTED_MONTHLY // (30 * 24)  # floor; the remainder is unbilled rounding
GIB = 2**30
MEDIA_BUCKET = "iforge-media-abcdefghij12"


async def _billed_installation(
    db_session: AsyncSession,
    user: User,
    *,
    billing_started_at: datetime = MONTH_START,
    db_bytes: int | None = None,
) -> CompanionInstallation:
    """A complete installation whose line item started, priced at the quote the tenant accepted."""
    env = await create_env_with_project(
        db_session,
        user_id=user.id,
        machine_id=f"metering-{uuid.uuid4().hex[:8]}",
        machine_name="Metering host",
    )
    label = f"postiz-{uuid.uuid4().hex[:12]}"
    installation = CompanionInstallation(
        user_id=user.id,
        environment_id=env.id,
        companion_key="postiz",
        isolation_tier="standard",
        status="complete",
        version="v2.24.0",
        pinned_digest=hashlib.sha256(label.encode()).hexdigest(),
        hostname_label=label,
        hostname=f"{label}.companions.example",
        owner_email="owner@companions.example",
        quote_id="quote-metering",
        quoted_monthly_usd_micros=QUOTED_MONTHLY,
        quoted_storage_usd_micros_per_gib_month=QUOTED_STORAGE_PER_GIB_MONTH,
        billing_line_item_id="line-item-metering",
        billing_started_at=billing_started_at,
        completed_at=billing_started_at,
        backup_state={} if db_bytes is None else {"db_bytes": db_bytes},
    )
    db_session.add(installation)
    await db_session.commit()
    await db_session.refresh(installation)
    return installation


def _line_for(body: dict, installation: CompanionInstallation) -> dict:
    (line,) = [c for c in body["companions"] if c["installation_id"] == str(installation.id)]
    return line


async def test_compute_accrues_at_the_accepted_price_and_is_read_back(
    client: httpx.AsyncClient, db_session: AsyncSession, seed_user: User
) -> None:
    installation = await _billed_installation(db_session, seed_user)

    # Before anything accrues, the installation is listed and its compute is NOT a measured zero.
    before = await client.get(SPEND_PATH, params={"month": MONTH})
    assert before.status_code == 200, before.text
    assert _line_for(before.json(), installation)["compute_usd_micros"] is None

    first_hour = datetime(2026, 9, 5, 10, tzinfo=UTC)
    for offset in range(3):
        assert await accrue_compute(db_session, installation, first_hour + timedelta(hours=offset))
    await db_session.commit()

    rows = (
        await db_session.scalars(
            select(UsageEvent)
            .where(UsageEvent.installation_id == installation.id)
            .order_by(UsageEvent.period_start)
        )
    ).all()
    assert [row.dimension for row in rows] == ["compute"] * 3
    assert [row.cost_usd_micros for row in rows] == [HOURLY_COST] * 3
    assert [row.period_start for row in rows] == [
        first_hour + timedelta(hours=offset) for offset in range(3)
    ]
    assert all(row.session_id is None and row.user_id == seed_user.id for row in rows)
    assert all(row.quantity == 1 and row.model == "postiz/standard" for row in rows)

    response = await client.get(SPEND_PATH, params={"month": MONTH})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["month"] == MONTH
    assert body["currency"] == "usd"
    line = _line_for(body, installation)
    assert line["agent_id"] == str(installation.environment_id)
    assert line["companion_key"] == "postiz"
    assert line["isolation_tier"] == "standard"
    assert line["compute_usd_micros"] == 3 * HOURLY_COST
    assert isinstance(line["compute_usd_micros"], int)
    assert line["storage_usd_micros"] is None
    assert line["ai_usage_usd_micros"] is None
    assert line["total_usd_micros"] == 3 * HOURLY_COST
    assert body["total_usd_micros"] == 3 * HOURLY_COST

    # Nothing else in the ledger: exactly the three accrued hours.
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(UsageEvent)
            .where(UsageEvent.installation_id == installation.id)
        )
        == 3
    )


async def test_compute_accrual_keeps_working_past_the_prepared_statement_plan_switch(
    db_session: AsyncSession, seed_user: User
) -> None:
    """A day of hours from one session: the worker accrues many periods in a row.

    asyncpg re-uses one prepared statement, which PostgreSQL plans generically from its sixth
    execution. A partial-index ON CONFLICT whose predicate is a bound parameter cannot infer its
    arbiter index under a generic plan, so every accrual after the fifth failed.
    """
    installation = await _billed_installation(db_session, seed_user)

    day = datetime(2026, 9, 6, tzinfo=UTC)
    for hour in range(24):
        assert await accrue_compute(db_session, installation, day + timedelta(hours=hour))
    for hour in range(24):
        assert not await accrue_compute(db_session, installation, day + timedelta(hours=hour))
    await db_session.commit()

    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(UsageEvent)
            .where(UsageEvent.installation_id == installation.id)
        )
        == 24
    )


# ---------------------------------------------------------------------------
# Storage, idempotency, the database's shape guard, nested LLM, honesty, export (Task 2).
# ---------------------------------------------------------------------------


class _MediaStub:
    """The installation's media bucket as the admin client lists it: keys with their sizes."""

    def __init__(self, sizes: list[tuple[str, int]]) -> None:
        self.sizes = sizes
        self.buckets: list[str] = []
        self.closed = 0

    def factory(self, bucket: str) -> _MediaStub:
        self.buckets.append(bucket)
        return self

    def list_object_sizes(self, prefix: str) -> list[tuple[str, int]]:
        assert prefix == ""
        return list(self.sizes)

    def close(self) -> None:
        self.closed += 1


async def _give_media_bucket(db_session: AsyncSession, installation: CompanionInstallation) -> None:
    """Record the install job's ``media`` step, as plan 06-13's provisioning step does."""
    installation.external_resources = {
        "steps": [{"name": "media", "resources": {"bucket": MEDIA_BUCKET}}]
    }
    await db_session.commit()


async def _other_tenant(db_session: AsyncSession) -> User:
    other = User(
        clerk_id=f"test_other_{uuid.uuid4().hex[:12]}",
        email=f"other-{uuid.uuid4().hex[:12]}@iforge.local",
        name="Other Tenant",
    )
    db_session.add(other)
    await db_session.flush()
    db_session.add(
        Project(user_id=other.id, name="Personal", slug="personal", kind=PROJECT_KIND_PERSONAL)
    )
    await db_session.commit()
    return other


async def _session_with_llm_spend(
    db_session: AsyncSession, user: User, costs: list[tuple[datetime, int]]
) -> Session:
    """One of the tenant's agent sessions with recorded model calls (the nested LLM spend)."""
    session = Session(
        user_id=user.id,
        environment_id=None,
        local_session_id=f"metering-{uuid.uuid4().hex[:8]}",
        started_at=MONTH_START,
    )
    db_session.add(session)
    await db_session.flush()
    for ts, cost in costs:
        db_session.add(
            UsageEvent(
                session_id=session.id, ts=ts, provider="anthropic", model="m", cost_usd_micros=cost
            )
        )
    await db_session.commit()
    return session


async def _rows(
    db_session: AsyncSession, installation: CompanionInstallation, dimension: str
) -> list[UsageEvent]:
    return list(
        (
            await db_session.scalars(
                select(UsageEvent)
                .where(
                    UsageEvent.installation_id == installation.id,
                    UsageEvent.dimension == dimension,
                )
                .order_by(UsageEvent.period_start)
            )
        ).all()
    )


async def test_storage_accrues_from_measured_bytes(
    client: httpx.AsyncClient, db_session: AsyncSession, seed_user: User
) -> None:
    installation = await _billed_installation(db_session, seed_user, db_bytes=3 * GIB)
    await _give_media_bucket(db_session, installation)
    media = _MediaStub([("uploads/a.png", GIB), ("uploads/b.mp4", GIB // 2)])

    day = datetime(2026, 9, 7, tzinfo=UTC)
    assert await accrue_storage(db_session, installation, day, media_client_factory=media.factory)
    await db_session.commit()

    measured = 3 * GIB + GIB + GIB // 2
    day_cost = measured * QUOTED_STORAGE_PER_GIB_MONTH // (GIB * 30)
    assert day_cost == 22_500  # 4.5 GiB at $0.15 per GiB-month, for one of September's 30 days
    (row,) = await _rows(db_session, installation, "storage")
    assert row.quantity == measured
    assert row.cost_usd_micros == day_cost
    assert row.period_start == day and row.ts == day
    assert row.session_id is None and row.user_id == seed_user.id
    assert media.buckets == [MEDIA_BUCKET]
    assert media.closed == 1

    # One row per UTC day: the next day is its own period.
    assert await accrue_storage(
        db_session, installation, day + timedelta(days=1), media_client_factory=media.factory
    )
    await db_session.commit()
    assert len(await _rows(db_session, installation, "storage")) == 2

    response = await client.get(SPEND_PATH, params={"month": MONTH})
    assert response.status_code == 200, response.text
    line = _line_for(response.json(), installation)
    assert line["storage_usd_micros"] == 2 * day_cost
    assert line["compute_usd_micros"] is None
    assert line["total_usd_micros"] == 2 * day_cost


async def test_accrual_is_idempotent_per_period(db_session: AsyncSession, seed_user: User) -> None:
    installation = await _billed_installation(db_session, seed_user, db_bytes=GIB)
    media = _MediaStub([])

    hour = datetime(2026, 9, 8, 3, tzinfo=UTC)
    day = datetime(2026, 9, 8, tzinfo=UTC)
    assert await accrue_compute(db_session, installation, hour) is True
    assert await accrue_compute(db_session, installation, hour) is False
    assert await accrue_storage(db_session, installation, day, media_client_factory=media.factory)
    assert (
        await accrue_storage(db_session, installation, day, media_client_factory=media.factory)
        is False
    )
    await db_session.commit()

    assert len(await _rows(db_session, installation, "compute")) == 1
    assert len(await _rows(db_session, installation, "storage")) == 1


async def test_database_rejects_mis_shaped_rows(db_session: AsyncSession, seed_user: User) -> None:
    """The shape CHECK, not the service, is what keeps an llm row session-bound and a compute or
    storage row session-free with its tenant, installation and period."""
    installation = await _billed_installation(db_session, seed_user)
    session = await _session_with_llm_spend(db_session, seed_user, [])
    hour = datetime(2026, 9, 9, 1, tzinfo=UTC)
    compute: dict[str, Any] = {
        "dimension": "compute",
        "user_id": seed_user.id,
        "installation_id": installation.id,
        "period_start": hour,
        "ts": hour,
        "model": "postiz/standard",
        "cost_usd_micros": HOURLY_COST,
        "quantity": 1,
    }
    mis_shaped: dict[str, dict[str, Any]] = {
        "llm without a session": {"dimension": "llm", "ts": hour, "model": "m"},
        "compute with a session": {**compute, "session_id": session.id},
        "compute without an installation": {**compute, "installation_id": None},
        "compute without a tenant": {**compute, "user_id": None},
        "compute without a period": {**compute, "period_start": None},
        "an unknown dimension": {**compute, "dimension": "bandwidth"},
    }
    rejected: list[str] = []
    for label, values in mis_shaped.items():
        with pytest.raises(IntegrityError, match="ck_usage_events_dimension"):
            async with db_session.begin_nested():
                db_session.add(UsageEvent(**values))
                await db_session.flush()
        db_session.expunge_all()
        rejected.append(label)
    assert rejected == list(mis_shaped)

    # The well-shaped row is accepted, which is what makes the rejections above meaningful.
    db_session.add(UsageEvent(**compute))
    await db_session.commit()


async def test_nested_llm_total_is_the_tenants_session_spend(
    client: httpx.AsyncClient, db_session: AsyncSession, seed_user: User
) -> None:
    in_month = datetime(2026, 9, 12, 8, tzinfo=UTC)
    await _session_with_llm_spend(
        db_session,
        seed_user,
        [
            (in_month, 1_000),
            (in_month + timedelta(minutes=5), 2_500),
            (MONTH_START - timedelta(seconds=1), 9_999),  # August: not this month
        ],
    )
    other = await _other_tenant(db_session)
    await _session_with_llm_spend(db_session, other, [(in_month, 77_777)])

    response = await client.get(SPEND_PATH, params={"month": MONTH})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["nested_llm_usd_micros"] == 3_500
    assert body["companions"] == []
    assert body["total_usd_micros"] == 3_500

    installation = await _billed_installation(db_session, seed_user)

    assert await accrue_compute(db_session, installation, in_month)
    await db_session.commit()
    body = (await client.get(SPEND_PATH, params={"month": MONTH})).json()
    assert body["nested_llm_usd_micros"] == 3_500
    assert body["total_usd_micros"] == 3_500 + HOURLY_COST
    # Companion-originated LLM spend is not attributed to the companion (PLAT-03).
    assert _line_for(body, installation)["ai_usage_usd_micros"] is None


async def test_unmeasured_dimension_is_null_not_zero(
    client: httpx.AsyncClient, db_session: AsyncSession, seed_user: User
) -> None:
    # No database size was ever reported, so storage cannot be measured -- and must not be zero.
    installation = await _billed_installation(db_session, seed_user, db_bytes=None)
    media = _MediaStub([("a.png", GIB)])

    day = datetime(2026, 9, 10, tzinfo=UTC)
    assert (
        await accrue_storage(db_session, installation, day, media_client_factory=media.factory)
        is False
    )
    await db_session.commit()
    assert await _rows(db_session, installation, "storage") == []

    body = (await client.get(SPEND_PATH, params={"month": MONTH})).json()
    line = _line_for(body, installation)
    assert line["compute_usd_micros"] is None
    assert line["storage_usd_micros"] is None
    assert line["ai_usage_usd_micros"] is None
    assert line["total_usd_micros"] is None
    assert body["nested_llm_usd_micros"] is None
    assert body["total_usd_micros"] is None

    assert await accrue_compute(db_session, installation, day)
    await db_session.commit()
    line = _line_for((await client.get(SPEND_PATH, params={"month": MONTH})).json(), installation)
    assert line["compute_usd_micros"] == HOURLY_COST
    assert line["storage_usd_micros"] is None
    assert line["total_usd_micros"] == HOURLY_COST


async def test_spend_read_out_is_scoped_to_the_caller(
    client: httpx.AsyncClient, db_session: AsyncSession, seed_user: User
) -> None:
    """T-06-21-04: another tenant's installation and its accrued spend never appear."""

    hour = datetime(2026, 9, 11, 4, tzinfo=UTC)
    other = await _other_tenant(db_session)
    theirs = await _billed_installation(db_session, other)
    assert await accrue_compute(db_session, theirs, hour)
    mine = await _billed_installation(db_session, seed_user)
    assert await accrue_compute(db_session, mine, hour)
    await db_session.commit()

    body = (await client.get(SPEND_PATH, params={"month": MONTH})).json()
    assert [line["installation_id"] for line in body["companions"]] == [str(mine.id)]
    assert body["total_usd_micros"] == HOURLY_COST


async def test_spend_month_defaults_to_the_current_utc_month_and_rejects_a_malformed_one(
    client: httpx.AsyncClient,
) -> None:
    now = datetime.now(UTC)
    body = (await client.get(SPEND_PATH)).json()
    assert body["month"] == f"{now.year:04d}-{now.month:02d}"
    assert (await client.get(SPEND_PATH, params={"month": "2026-13"})).status_code == 422
    assert (await client.get(SPEND_PATH, params={"month": "2026-9"})).status_code == 422


def _jsonl(archive: bytes, member: str) -> list[dict[str, Any]]:
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        extracted = tar.extractfile(member)
        assert extracted is not None
        return [json.loads(line) for line in extracted.read().decode().splitlines() if line]


async def test_export_includes_dimension_rows(
    db_session: AsyncSession, seed_user: User, tmp_path: Path
) -> None:
    """The tenant export's usage domain carries the compute and storage rows as well as the
    session rows -- the ledger a tenant is shown is the ledger a tenant can take away."""

    session = await _session_with_llm_spend(
        db_session, seed_user, [(datetime(2026, 9, 12, tzinfo=UTC), 4_242)]
    )
    installation = await _billed_installation(db_session, seed_user)
    hour = datetime(2026, 9, 12, 5, tzinfo=UTC)
    assert await accrue_compute(db_session, installation, hour)
    other = await _other_tenant(db_session)
    assert await accrue_compute(db_session, await _billed_installation(db_session, other), hour)
    await db_session.commit()

    archive, manifest = await assemble_tenant_export(
        db_session,
        LocalFileStore(str(tmp_path / "files")),
        seed_user.id,
        include_vault_values=False,
    )
    rows = {entry["name"]: entry["rows"] for entry in manifest["domains"]}
    assert rows["usage_events"] == 2
    records = _jsonl(archive, "usage_events.jsonl")
    by_dimension = {record.get("dimension"): record for record in records}
    assert sorted(by_dimension, key=str) == ["compute", "llm"]

    llm = by_dimension["llm"]
    assert llm["session_id"] == str(session.id)
    assert llm["cost_usd_micros"] == 4_242
    assert llm["installation_id"] is None

    compute = by_dimension["compute"]
    assert compute["session_id"] is None
    assert compute["installation_id"] == str(installation.id)
    assert compute["environment_id"] == str(installation.environment_id)
    assert compute["companion_key"] == "postiz"
    assert datetime.fromisoformat(compute["period_start"]) == hour
    assert compute["quantity"] == 1
    assert compute["cost_usd_micros"] == HOURLY_COST


async def test_list_objects_returns_keys_and_sizes(
    db_session: AsyncSession, seed_user: User
) -> None:
    """The media measurement reads the adapter's listing -- keys with sizes, across pages -- and
    sums the sizes; it never reads an object. boto3 stays behind ``file_store_s3``."""
    installation = await _billed_installation(db_session, seed_user, db_bytes=0)
    await _give_media_bucket(db_session, installation)
    pages = [
        {
            "Contents": [{"Key": "a.png", "Size": 10}, {"Key": "b.mp4", "Size": 2048}],
            **_OK_METADATA,
        },
        {"Contents": [{"Key": "c.gif", "Size": 7}], **_OK_METADATA},
    ]

    class _ClosableFake(_FakeBoto3S3Client):
        def close(self) -> None:
            self.calls.append(("close",))

    fake = _ClosableFake(pages=pages)
    opened: list[str] = []

    def factory(bucket: str) -> _Boto3S3ObjectStoreClient:
        opened.append(bucket)
        adapter = _Boto3S3ObjectStoreClient(
            bucket=bucket,
            region="auto",
            endpoint_url="http://127.0.0.1:1",
            access_key_id="fake-access-key",
            secret_access_key="fake-secret-key",
            force_path_style=True,
        )
        # Inject the fake at the same seam the real boto3 client occupies.
        adapter._client = fake  # type: ignore[assignment]
        return adapter

    assert await measure_media_bytes(installation, media_client_factory=factory) == 10 + 2048 + 7
    assert opened == [MEDIA_BUCKET]
    assert ("paginate", {"Bucket": MEDIA_BUCKET, "Prefix": ""}) in fake.calls
    assert ("close",) in fake.calls


# ---------------------------------------------------------------------------
# The metering worker (Task 3): it runs on its own, from an injectable clock.
# ---------------------------------------------------------------------------


def test_metering_worker_is_registered_in_the_worker_process() -> None:
    from app.workers import channels as channel_workers

    workers = channel_workers.build_channel_workers()
    assert "CompanionMeteringWorker" in {type(worker).__name__ for worker in workers}
    source = inspect.getsource(channel_workers.build_channel_workers)
    assert "CompanionMeteringWorker(async_session_factory)" in source


async def test_worker_accrues_every_missing_hour_and_yesterdays_storage(
    db_session: AsyncSession, seed_user: User
) -> None:

    started = datetime(2026, 9, 4, 22, 30, tzinfo=UTC)
    installation = await _billed_installation(
        db_session, seed_user, billing_started_at=started, db_bytes=GIB
    )
    media = _MediaStub([])
    worker = CompanionMeteringWorker(_sessionmaker(db_session), media_client_factory=media.factory)

    # 02:10 on the 5th: 23:00, 00:00 and 01:00 are whole billed hours; 22:00 began before billing
    # did and 02:00 has not ended. Yesterday (the 4th) was not billed for the whole day.
    await worker.run_once(datetime(2026, 9, 5, 2, 10, tzinfo=UTC))
    compute = await _rows(db_session, installation, "compute")
    assert [row.period_start for row in compute] == [
        datetime(2026, 9, 4, 23, tzinfo=UTC),
        datetime(2026, 9, 5, 0, tzinfo=UTC),
        datetime(2026, 9, 5, 1, tzinfo=UTC),
    ]
    assert await _rows(db_session, installation, "storage") == []

    # Just after midnight on the 6th: every missing hour since the last accrued one, and the 5th,
    # the first whole billed day, as storage.
    await worker.run_once(datetime(2026, 9, 6, 0, 30, tzinfo=UTC))
    compute = await _rows(db_session, installation, "compute")
    assert len(compute) == 3 + 22  # 02:00 .. 23:00 on the 5th
    assert compute[-1].period_start == datetime(2026, 9, 5, 23, tzinfo=UTC)
    hours = [row.period_start for row in compute]
    assert hours == sorted(set(hours))
    assert all(b - a == timedelta(hours=1) for a, b in zip(hours, hours[1:], strict=False))
    (storage,) = await _rows(db_session, installation, "storage")
    assert storage.period_start == datetime(2026, 9, 5, tzinfo=UTC)
    assert storage.quantity == GIB

    # Running again at the same moment records nothing new.
    await worker.run_once(datetime(2026, 9, 6, 0, 30, tzinfo=UTC))
    assert len(await _rows(db_session, installation, "compute")) == 25
    assert len(await _rows(db_session, installation, "storage")) == 1


async def test_worker_accrues_nothing_for_a_removed_installation_after_billing_stopped(
    db_session: AsyncSession, seed_user: User
) -> None:

    installation = await _billed_installation(
        db_session,
        seed_user,
        billing_started_at=datetime(2026, 9, 5, 0, tzinfo=UTC),
        db_bytes=GIB,
    )
    stopped = datetime(2026, 9, 5, 3, 30, tzinfo=UTC)
    installation.billing_stopped_at = stopped
    installation.removed_at = stopped
    await db_session.commit()
    worker = CompanionMeteringWorker(
        _sessionmaker(db_session), media_client_factory=_MediaStub([]).factory
    )

    await worker.run_once(datetime(2026, 9, 7, 12, tzinfo=UTC))
    await worker.run_once(datetime(2026, 9, 8, 12, tzinfo=UTC))

    compute = await _rows(db_session, installation, "compute")
    # 00:00, 01:00 and 02:00; 03:00 did not end before billing stopped, and nothing after it.
    assert [row.period_start.hour for row in compute] == [0, 1, 2]
    assert all(row.period_start < stopped for row in compute)
    assert await _rows(db_session, installation, "storage") == []


async def test_worker_keeps_metering_other_installations_when_one_media_listing_fails(
    db_session: AsyncSession, seed_user: User
) -> None:
    """A media listing failure costs that installation its storage day, not everyone's."""

    started = datetime(2026, 9, 1, tzinfo=UTC)
    broken = await _billed_installation(
        db_session, seed_user, billing_started_at=started, db_bytes=1
    )
    await _give_media_bucket(db_session, broken)
    healthy = await _billed_installation(
        db_session, seed_user, billing_started_at=started, db_bytes=2
    )

    def factory(bucket: str) -> _MediaStub:
        raise RuntimeError(f"listing {bucket} failed")

    worker = CompanionMeteringWorker(_sessionmaker(db_session), media_client_factory=factory)
    await worker.run_once(datetime(2026, 9, 2, 1, 5, tzinfo=UTC))

    assert len(await _rows(db_session, broken, "compute")) == 25
    assert await _rows(db_session, broken, "storage") == []
    assert len(await _rows(db_session, healthy, "compute")) == 25
    (storage,) = await _rows(db_session, healthy, "storage")
    assert storage.quantity == 2
