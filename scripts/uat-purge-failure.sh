#!/usr/bin/env bash
# Drive a scheduled tenant purge into the `failed` state — safely — so the
# operator console's failed-sweep panel can be walked.
#
# UAT 61 ("A failed purge is reported as a failed sweep, not as scheduled") was
# the one checkpoint in phase 03 with no path: rendering that panel needs a
# `tenant_purges` row in `failed` carrying a non-null `error` and a non-zero
# `attempt_count`, and nothing produced one. The obvious route — an UPDATE
# against the dev Postgres — fabricates the state instead of observing it, and
# would have certified the panel against a row no code path can actually
# produce. This runs the REAL sweep and records the REAL failure.
#
# The safety argument, which is the whole reason this script is not a psql
# one-liner:
#
#   `execute_tenant_purge` runs its external calls FIRST (tenant_purge.py) —
#   deployment teardown and subscription cancel over /v2 — and only THEN the
#   provider identity delete, the S3 prefix deletes and the row deletes. Both
#   failure modes below land inside or before that first block, so the sweep
#   dies before its first destructive step every time. Nothing of the tenant
#   is lost; only `tenant_purges` changes.
#
#   That ordering is an invariant this script DEPENDS on, so it verifies it
#   rather than trusting it: an exception of an unexpected type aborts without
#   recording anything, and the tenant's rows are counted before and after and
#   compared. If a future refactor moves a destructive step above the failure
#   point, this script refuses instead of half-purging a tenant.
#
# Failure modes (both are real production code paths, not test doubles):
#   unreachable   DEPLOY_API_URL -> 127.0.0.1:1, which nothing can be listening
#                 on. The sweep dies in httpx with a short connect error.
#                 This is what a /v2 outage looks like in production.
#   unconfigured  DEPLOY_API_URL cleared with no opt-out, so the sweep refuses
#                 at its hosted-boundary branch (WR-04) with a ~380-character
#                 message. Use this one to see the console TRUNCATE an error:
#                 the display budget is 160 chars (PURGE_ERROR_DISPLAY_MAX).
#
# Usage:
#   bash scripts/uat-purge-failure.sh status <user_id>
#   bash scripts/uat-purge-failure.sh fail   <user_id> [attempts] [mode]
#   bash scripts/uat-purge-failure.sh reset  <user_id>
#
#   attempts  how many failed sweeps to run, default 1. The console renders
#             "Attempt {n}", so >1 is worth one run to see the count move.
#   mode      unreachable (default) | unconfigured
#
# Prerequisite: the tenant must already be deleted, so a purge row exists.
#   bash scripts/uat-admin.sh POST /v1/admin/tenants/<user_id>/delete '{"reason":"uat 61"}'
#
# `fail` backdates `purge_after`/`next_attempt_at` so the row is claimable now
# instead of after the grace window — that is what "drive it" means here, and
# `reset` puts both back. `reset` also clears the error and the attempt count
# and returns the row to `pending`, so the walk can be repeated without
# deleting another tenant. Canceling the purge from the console DELETES the
# row (WR-02), so re-walking after a cancel needs a fresh delete.
#
# HOLD — and why a failed purge is not a resting state:
#
#   `claim_tenant_purge` scans the WHOLE table for due work; it does not know
#   about tenants. A `failed` row becomes due again after its backoff (capped
#   at 15 minutes), and ANYTHING that claims — a running TenantPurgeWorker, or
#   the backend test suite, whose committed-lane worker tests call
#   `claim_tenant_purge()` with no filter — will then pick up this tenant and
#   finish the sweep for real.
#
#   That is not hypothetical: it happened while this script was being built.
#   A throwaway tenant left `failed` in the dev database was claimed and fully
#   purged by a `pytest` run minutes later.
#
#   So `fail` HOLDS the row: after recording the real failure it pushes
#   `next_attempt_at` out by the grace window, which is the one field the
#   console does not render. `status`, `error`, `attempt_count` and
#   `purge_after` are left exactly as the production recorder wrote them.
#   `status` prints whether a row is held or claimable.
#
#   Even so: do not run the backend test suite against a database holding a
#   tenant you care about mid-walk.
#
# This reads its database URL the way uvicorn does — pydantic-settings,
# in-process, straight from backend/.env — so it always writes to whatever
# database the running stack is using. There is no override: pointing this at a
# different database than the console you are about to inspect would produce a
# walk of the wrong row. It is a UAT-host tool; it has no business running
# against production data.
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ $# -lt 1 || "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
	# The whole leading comment block, however long it grows — a fixed line
	# range silently truncates the help the moment the header is edited.
	awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "${BASH_SOURCE[0]}"
	exit 0
fi

VERB="$1"
USER_ID="${2:?need a user id, e.g. bash scripts/uat-purge-failure.sh status <user_id>}"
ATTEMPTS="${3:-1}"
MODE="${4:-unreachable}"

case "$VERB" in
	status|fail|reset) ;;
	*) echo "unknown verb '$VERB' (expected status, fail or reset)" >&2; exit 2 ;;
esac
case "$MODE" in
	unreachable|unconfigured) ;;
	*) echo "unknown mode '$MODE' (expected unreachable or unconfigured)" >&2; exit 2 ;;
esac
if ! [[ "$ATTEMPTS" =~ ^[0-9]+$ ]] || (( ATTEMPTS < 1 )); then
	echo "attempts must be a positive integer, got '$ATTEMPTS'" >&2; exit 2
fi

cd "$REPO_ROOT/backend"
VERB="$VERB" USER_ID="$USER_ID" ATTEMPTS="$ATTEMPTS" MODE="$MODE" \
	PYTHONPATH=. uv run --quiet python - <<'PY'
import asyncio
import os
import sys
import uuid
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import func, select

from app.core.config import settings
from app.core.database import async_session_factory
from app.models.api_key import ApiKey
from app.models.memory import Memory
from app.models.session import Session
from app.models.skill import Skill
from app.models.tenant_purge import (
    TENANT_PURGE_STATUS_COMPLETE,
    TENANT_PURGE_STATUS_PENDING,
    TENANT_PURGE_STATUS_PURGING,
    TenantPurge,
)
from app.models.user import User
from app.models.vault import Vault
from app.services.file_store import get_file_store
from app.services.tenant_purge import execute_tenant_purge
from app.services.tenant_purge_worker import record_tenant_purge_failure

# stderr is unbuffered; without this the abort messages below print ahead of
# the progress lines they belong after.
sys.stdout.reconfigure(line_buffering=True)

VERB = os.environ["VERB"]
MODE = os.environ["MODE"]
ATTEMPTS = int(os.environ["ATTEMPTS"])

try:
    USER_ID = uuid.UUID(os.environ["USER_ID"])
except ValueError:
    sys.exit(f"not a UUID: {os.environ['USER_ID']!r}")

# Tables counted before and after the sweep. Not the whole PURGE_COVERAGE set —
# enough that a sweep which got past its first destructive step cannot go
# unnoticed, cheap enough to run on every attempt.
WITNESS_MODELS = (User, Session, Skill, Memory, Vault, ApiKey)

# Nothing can be listening here: port 1 is privileged and unassigned, so the
# connect is refused immediately rather than hanging for the 10s /v2 timeout.
UNREACHABLE_URL = "http://127.0.0.1:1"


async def _open_purge(db) -> TenantPurge | None:
    """The newest non-complete purge row for this user."""
    return (
        await db.execute(
            select(TenantPurge)
            .where(
                TenantPurge.user_id == USER_ID,
                TenantPurge.status != TENANT_PURGE_STATUS_COMPLETE,
            )
            .order_by(TenantPurge.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _witness(db) -> dict[str, int]:
    counts = {}
    for model in WITNESS_MODELS:
        column = model.id if model is User else model.user_id
        counts[model.__tablename__] = (
            await db.execute(select(func.count()).select_from(model).where(column == USER_ID))
        ).scalar_one()
    return counts


def _print_purge(purge: TenantPurge) -> None:
    print(f"  id             {purge.id}")
    print(f"  status         {purge.status}")
    print(f"  attempt_count  {purge.attempt_count}")
    print(f"  purge_after    {purge.purge_after.isoformat()}")
    print(f"  next_attempt   {purge.next_attempt_at.isoformat() if purge.next_attempt_at else '-'}")
    print(f"  claimed_at     {purge.claimed_at.isoformat() if purge.claimed_at else '-'}")
    error = purge.error
    if error is None:
        print("  error          <null>")
    else:
        print(f"  error          ({len(error)} chars) {error[:160]}")
        if len(error) > 160:
            print("                 ^ the console truncates at 160 (PURGE_ERROR_DISPLAY_MAX)")
    now = datetime.now(UTC)
    claimable = (
        purge.status in (TENANT_PURGE_STATUS_PENDING, "failed")
        and purge.purge_after <= now
        and (purge.next_attempt_at is None or purge.next_attempt_at <= now)
    )
    if claimable:
        print("  CLAIMABLE NOW  any worker — or a backend test run — will sweep this tenant")
    else:
        print("  held           not due; no worker will claim it")


async def do_status() -> int:
    async with async_session_factory() as db:
        purge = await _open_purge(db)
        if purge is None:
            print(f"no open purge row for {USER_ID}")
            print("Delete the tenant first:")
            print(
                "  bash scripts/uat-admin.sh POST "
                f"/v1/admin/tenants/{USER_ID}/delete '{{\"reason\":\"uat 61\"}}'"
            )
            return 1
        print(f"purge row for {USER_ID}:")
        _print_purge(purge)
        return 0


async def do_reset() -> int:
    async with async_session_factory() as db:
        purge = await _open_purge(db)
        if purge is None:
            return await do_status()
        now = datetime.now(UTC)
        purge.status = TENANT_PURGE_STATUS_PENDING
        purge.error = None
        purge.attempt_count = 0
        purge.claim_id = None
        purge.claimed_at = None
        purge.purge_after = now + timedelta(days=settings.tenant_purge_grace_days)
        purge.next_attempt_at = purge.purge_after
        await db.commit()
        print(f"reset to pending, due {purge.purge_after.isoformat()}")
        _print_purge(purge)
        return 0


async def _park(purge_id: uuid.UUID) -> None:
    """Release an aborted claim and move the row OUT of the due window.

    Leaving it `purging` is not the cautious choice it looks like: the claim
    lease expires after 15 minutes and a real worker then reclaims the row and
    runs the sweep this script just refused to finish. A not-yet-due `pending`
    row is the genuinely inert state, and it is the state the operator can
    inspect and re-drive.
    """
    async with async_session_factory() as db:
        purge = await db.get(TenantPurge, purge_id)
        if purge is None:
            return
        now = datetime.now(UTC)
        purge.status = TENANT_PURGE_STATUS_PENDING
        purge.claim_id = None
        purge.claimed_at = None
        purge.purge_after = now + timedelta(days=settings.tenant_purge_grace_days)
        purge.next_attempt_at = purge.purge_after
        await db.commit()
    print(
        f"  purge parked: pending, not due until {purge.purge_after.isoformat()} "
        "(no worker will claim it)",
        file=sys.stderr,
    )


async def _one_attempt(n: int) -> int:
    """Claim the target row, run the real sweep, record the real failure."""
    async with async_session_factory() as db:
        purge = await _open_purge(db)
        if purge is None:
            return await do_status()
        if purge.status == TENANT_PURGE_STATUS_PURGING and purge.claimed_at is not None:
            print(
                f"attempt {n}: purge {purge.id} is claimed by a live worker "
                f"(claimed_at {purge.claimed_at.isoformat()}). Stop the worker first.",
                file=sys.stderr,
            )
            return 1

        before = await _witness(db)
        now = datetime.now(UTC)

        # A SCOPED claim. `claim_tenant_purge` takes whatever row is due
        # globally, which on a shared UAT host could be somebody else's
        # tenant; the field mutations below are exactly its own, applied to
        # the row this invocation names. The part UAT 61 is actually about —
        # how a failure is recorded — runs through the production function
        # unmodified, further down.
        purge.purge_after = now - timedelta(hours=1)
        purge.next_attempt_at = now - timedelta(hours=1)
        purge.status = TENANT_PURGE_STATUS_PURGING
        claim_id = uuid.uuid4()
        purge.claim_id = claim_id
        purge.claimed_at = now
        purge.attempt_count += 1
        await db.commit()
        purge_id = purge.id

    # Arrange the chosen failure. Both settings are process-local: this
    # script's own Settings object, never backend/.env, so the running uvicorn
    # is unaffected.
    if MODE == "unreachable":
        settings.deploy_api_url = UNREACHABLE_URL
        settings.tenant_purge_skip_hosted_teardown = False
        tolerated = (httpx.HTTPError,)
    else:
        settings.deploy_api_url = ""
        settings.tenant_purge_skip_hosted_teardown = False
        tolerated = (RuntimeError,)

    async with async_session_factory() as db:
        purge = await db.get(TenantPurge, purge_id)
        assert purge is not None
        try:
            await execute_tenant_purge(db, get_file_store(), purge)
        except tolerated as exc:
            await db.rollback()
            error = str(exc) or exc.__class__.__name__
        except Exception as exc:  # noqa: BLE001
            # The ordering this script relies on no longer holds, or the
            # failure is not the one that was arranged. Record NOTHING: an
            # unexpected exception may have landed after a destructive step.
            await db.rollback()
            print(
                f"attempt {n}: ABORTED — expected {tolerated[0].__name__}, got "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            print(
                "Nothing was recorded. Check whether execute_tenant_purge still "
                "runs its external calls before its destructive steps.",
                file=sys.stderr,
            )
            await _park(purge_id)
            return 1
        else:
            print(
                f"attempt {n}: ABORTED — the sweep SUCCEEDED and purged the tenant. "
                "The arranged failure did not fire.",
                file=sys.stderr,
            )
            return 1

    async with async_session_factory() as db:
        after = await _witness(db)
        if after != before:
            lost = {k: (before[k], after[k]) for k in before if before[k] != after[k]}
            print(
                f"attempt {n}: ABORTED — the sweep destroyed tenant data before it "
                f"failed: {lost}",
                file=sys.stderr,
            )
            await _park(purge_id)
            return 1

        # The real failure recorder: status, capped backoff, claim release.
        recorded = await record_tenant_purge_failure(
            db, purge_id=purge_id, expected_claim_id=claim_id, error=error
        )
        await db.commit()
        if not recorded:
            print(
                f"attempt {n}: failure not recorded — the claim was lost or the "
                "purge completed elsewhere.",
                file=sys.stderr,
            )
            return 1

        # HOLD the row out of the due window (see the header). The recorder
        # just set next_attempt_at to now+backoff; anything that claims would
        # then finish this sweep for real while the operator is still looking
        # at the panel. Nothing the console renders is touched.
        held = await db.get(TenantPurge, purge_id)
        assert held is not None
        held.next_attempt_at = datetime.now(UTC) + timedelta(
            days=settings.tenant_purge_grace_days
        )
        await db.commit()

        print(f"attempt {n}: failed as arranged ({MODE}) — {error[:100]}")
        return 0


async def do_fail() -> int:
    print(f"mode={MODE} attempts={ATTEMPTS} user={USER_ID}")
    for n in range(1, ATTEMPTS + 1):
        code = await _one_attempt(n)
        if code != 0:
            return code
    async with async_session_factory() as db:
        purge = await _open_purge(db)
        if purge is not None:
            print()
            print("purge row now:")
            _print_purge(purge)
    print()
    print("Open the operator console, pick this tenant, and read the deletion panel.")
    print("Expected: a DESTRUCTIVE badge 'Purge ran and failed — data may already be")
    print("deleted', 'Attempt {n} — {error}' beside it, no future date, and a confirm")
    print("labelled 'Stop the purge'.")
    return 0


VERBS = {"status": do_status, "fail": do_fail, "reset": do_reset}
sys.exit(asyncio.run(VERBS[VERB]()))
PY
