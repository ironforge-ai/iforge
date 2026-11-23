"""Durable companion lifecycle worker: the install job's state machine (APP-01, APP-04, D-26).

Cloned from ``TenantExportWorker``: claim one due row in its own short transaction with
``FOR UPDATE SKIP LOCKED`` and a lease, do the work in fresh sessions, fence every write on the
claim id, and poll in ``run_forever``. A crashed worker's lease goes stale after
``COMPANION_LIFECYCLE_CLAIM_LEASE_SECONDS`` and the next worker resumes the row where it stopped.

**The state machine is forward-only** (D-26): ``queued -> running -> complete``, or ``failed``.

* ``queued``: the worker runs the ordered ``PROVISIONING_STEPS`` and only THEN marks the job
  ``running`` -- the status at which plan 06-10's projection starts emitting the companion to its
  agent's runtime, so the runtime never sees an installation whose platform-side resources do not
  exist yet.
* ``running``: the runtime reports through ``POST /v1/runtime/companion-installations/{id}/report``
  (``companion_install.record_install_report``). A data-asserted ``ready`` report leaves the job at
  ``READY_STEP`` and due; the worker then runs ``COMPLETION_STEPS`` and marks it ``complete``.
* ``complete``: the moment the job completes, the tenant's per-process tool cache is reset, so
  the companion's verbs are listed for the agent in the same action (APP-04). Nothing registers
  tools separately: the façade lists a completed installation's kind as deployed.
* ``complete`` and reachable: every five minutes at most, the worker probes a complete
  installation's hostname through the edge (``companion_edge.probe_edge_reachability``) and records
  ``external_resources["edge"]["reachability"] = {state, checked_at}``. Reachability is measured,
  never assumed (APP-02).
* ``failed``: terminal for the operation. The worker tears down every step that operation
  completed, in reverse order, and records each in ``external_resources["torn_down"]``. A job
  fails by a runtime ``failed`` report, by a pipeline step that raises, or by the reporting
  deadline: a running job that hears nothing for ``REPORT_DEADLINE_SECONDS`` fails with
  ``RUNTIME_DID_NOT_REPORT`` instead of running forever (T-06-11-05). Every stored reason is
  redacted against the installation's own credentials.

**Pipelines.** A ``ProvisioningStep`` is a name, a ``provision`` that returns the resources it
created (JSON, stored with the step), and a ``teardown`` that receives those resources back. Each
completed step is recorded in ``external_resources["steps"]`` together with the operation it ran
for, so a reclaimed job resumes after its last completed step and a teardown knows exactly what
this operation created. The step contract itself lives in ``companion_lifecycle_steps`` (so a step
module never imports this worker) and is re-exported here. Plan 06-13 provides the first two
provisioning steps (``media``, ``temporal``); plan 06-14 appends the unrouted tunnel (``edge``) and
provides the first completion step (``edge-publish``); plan 06-15 appends the no-delete backup key
(``backup``); plan 06-17 appends the billing line item (``billing-start``) as the last completion
step.

**Updates (plan 06-18).** An accepted update starts ``running`` directly -- it provisions nothing
-- and a ready report completes it WITHOUT the completion steps, which belong to the install: the
hostname is already published and the line item already bills, so re-running them would publish
twice and bill twice. A failed update owes no teardown, and the deadline fails it the same way it
fails an install, additionally returning the pin to the digest it was accepted from.

**The hostname goes live only after bootstrap (research Pitfall 3).** Completion steps run only
for a job the runtime reported ready, so ``EDGE_PUBLISH_STEP`` -- the ingress rule and the DNS
record -- can never precede the data-asserted ready report, whichever worker runs it and however
often a worker restarts.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

from pydantic import JsonValue
from sqlalchemy import DateTime, and_, or_, select
from sqlalchemy import cast as sql_cast
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm.attributes import flag_modified

from app.models.companion_installation import (
    COMPANION_INSTALLATION_STATUS_COMPLETE,
    COMPANION_INSTALLATION_STATUS_FAILED,
    COMPANION_INSTALLATION_STATUS_QUEUED,
    COMPANION_INSTALLATION_STATUS_RUNNING,
    COMPANION_OPERATION_INSTALL,
    COMPANION_OPERATION_UNINSTALL,
    COMPANION_OPERATION_UPDATE,
    HOST_REMOVAL_UNCONFIRMED_KEY,
    REMOVED_REPORTED_AT_KEY,
    CompanionInstallation,
    installation_is_serving,
    serving_installation_filter,
)
from app.models.user import User
from app.schemas.companions import MAX_INSTALL_REPORT_ERROR_CHARS
from app.services.companion_backup_repo import BACKUP_REPO_STEP
from app.services.companion_billing import BILLING_START_STEP
from app.services.companion_edge import (
    EDGE_PUBLISH_STEP,
    EDGE_TUNNEL_STEP,
    REACHABILITY_PROBE_INTERVAL_SECONDS,
    probe_edge_reachability,
)
from app.services.companion_facade import reset_companion_tools_cache_for
from app.services.companion_install import (
    READY_STEP,
    REPORT_DEADLINE_SECONDS,
    RUNTIME_DID_NOT_REPORT,
    UNINSTALL_RECORD_KEY,
    UNINSTALL_REMOVAL_DEADLINE_SECONDS,
    UPDATE_ERROR_RUNTIME_DID_NOT_REPORT,
    installation_redactor,
    revert_update_pin,
)
from app.services.companion_lifecycle_steps import (
    CompanionStepRetryableError,
    JsonObject,
    LifecycleStepContext,
    ProvisionAction,
    ProvisioningStep,
    TeardownAction,
)
from app.services.companion_media_store import MEDIA_BUCKET_STEP
from app.services.companion_temporal import TEMPORAL_NAMESPACE_STEP

log = logging.getLogger(__name__)

# How long a claim may sit before another worker may take the row over (a crashed worker).
COMPANION_LIFECYCLE_CLAIM_LEASE_SECONDS = 5 * 60
COMPANION_LIFECYCLE_BACKOFF_CAP_SECONDS = 15 * 60
# The first retry of a step that failed transiently waits this long; each later one twice as long.
STEP_RETRY_BASE_SECONDS = 30
# A reachability probe reserved by a worker that crashed mid-probe is re-probed after this long.
REACHABILITY_PROBE_LEASE_SECONDS = 60


# Order is execution order; teardown is reverse. Plan 06-13 provides the media bucket and the
# Temporal namespace -- media first, so Postiz's two external dependencies exist before anything
# can boot against them. Plan 06-14's tunnel follows, created with NO ingress and NO DNS record,
# then plan 06-15's backup key.
PROVISIONING_STEPS: tuple[ProvisioningStep, ...] = (
    MEDIA_BUCKET_STEP,
    TEMPORAL_NAMESPACE_STEP,
    EDGE_TUNNEL_STEP,
    # Plan 06-15: the installation's no-delete backup key and its repository cipher pass, so the
    # database archives from its very first start.
    BACKUP_REPO_STEP,
)
# Run only after a data-asserted ready report. Publishing the hostname comes FIRST (Pitfall 3):
# the platform is already Postiz's first registrant, and every later completion step starts only
# once the tenant can actually reach what they pay for. Plan 06-17's line item is that later step:
# a failed or unreachable install never reaches it, so it is never billed (APP-08).
COMPLETION_STEPS: tuple[ProvisioningStep, ...] = (EDGE_PUBLISH_STEP, BILLING_START_STEP)


class CompanionLifecycleClaimLostError(RuntimeError):
    pass


class _StepFailedError(Exception):
    """A pipeline step raised.

    The operation fails, unless the step raised ``CompanionStepRetryableError`` and has attempts
    left under its ``max_attempts``: then the job keeps its status and is due again after a backoff.
    """

    def __init__(
        self, step_name: str, cause: Exception, *, retryable: bool = False, max_attempts: int = 1
    ) -> None:
        super().__init__(step_name)
        self.step_name = step_name
        self.cause = cause
        self.retryable = retryable
        self.max_attempts = max_attempts

    def reason(self) -> str:
        # Unredacted here on purpose; ``_redacted`` runs before anything is stored.
        detail = str(self.cause) or self.cause.__class__.__name__
        return f"step {self.step_name} failed: {self.cause.__class__.__name__}: {detail}"


@dataclass(frozen=True, slots=True)
class ClaimedCompanionLifecycle:
    installation_id: UUID
    claim_id: UUID


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _recorded_steps(installation: CompanionInstallation) -> list[JsonObject]:
    steps = (installation.external_resources or {}).get("steps")
    if not isinstance(steps, list):
        return []
    return [cast(JsonObject, entry) for entry in steps if isinstance(entry, dict)]


def _newest_step_resources(installation: CompanionInstallation, name: str) -> JsonObject | None:
    """What the newest recorded ``name`` step created, of any operation, or ``None``."""
    for entry in reversed(_recorded_steps(installation)):
        if entry.get("name") == name:
            resources = entry.get("resources")
            return dict(resources) if isinstance(resources, dict) else {}
    return None


def _uninstall_record(installation: CompanionInstallation) -> JsonObject:
    recorded = (installation.external_resources or {}).get(UNINSTALL_RECORD_KEY)
    return dict(recorded) if isinstance(recorded, dict) else {}


def _set_resource(row: CompanionInstallation, key: str, value: JsonValue) -> None:
    row.external_resources = {**(row.external_resources or {}), key: value}
    flag_modified(row, "external_resources")


def _step_context(installation: CompanionInstallation, sessionmaker) -> LifecycleStepContext:
    return LifecycleStepContext(
        installation_id=installation.id,
        user_id=installation.user_id,
        environment_id=installation.environment_id,
        companion_key=installation.companion_key,
        operation=installation.operation,
        hostname=installation.hostname,
        sessionmaker=sessionmaker,
    )


async def teardown_installation_resources(
    db: AsyncSession,
    installation: CompanionInstallation,
    *,
    steps: tuple[ProvisioningStep, ...] | None = None,
    sessionmaker: async_sessionmaker[AsyncSession] | None = None,
    clock: Callable[[], datetime] = _utcnow,
) -> list[str]:
    """Tear down every external resource the installation's recorded steps created, newest first.

    Every recorded step counts, whichever operation created it -- an uninstall or a tenant purge
    removes what the install provisioned and completed: the billing line (stopped, idempotently),
    the hostname's DNS record, the backup key (revoked; no backup file is deleted, so retained
    backups age out on their normal retention and the cipher pass stays readable), the tunnel,
    the Temporal namespace and the media bucket with its domain and token.

    One durable step at a time: each undone step moves from ``steps`` to ``torn_down`` in its own
    commit, so an interrupted teardown resumes with exactly what is left, and a second call once
    nothing is left does nothing (safe to run twice). A teardown that raises stops here with the
    rest still recorded; the caller retries. No row lock is held while a teardown runs -- a step
    may call an external API for a while -- so every step's own session can read and write the
    installation meanwhile.

    ``db`` supplies the bind the step sessions are opened on unless ``sessionmaker`` is given.
    Returns the names torn down by this call, in order.
    """
    by_name = {step.name: step for step in (steps or (*PROVISIONING_STEPS, *COMPLETION_STEPS))}
    maker = sessionmaker or async_sessionmaker(
        bind=db.bind, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    context = _step_context(installation, maker)
    installation_id = context.installation_id
    torn: list[str] = []
    while True:
        async with maker() as session:
            row = await session.scalar(
                select(CompanionInstallation)
                .where(CompanionInstallation.id == installation_id)
                .execution_options(populate_existing=True)
            )
            recorded = _recorded_steps(row) if row is not None else []
            await session.rollback()
        if not recorded:
            break
        entry = recorded[-1]
        name = str(entry.get("name"))
        step = by_name.get(name)
        resources = entry.get("resources")
        if step is None:
            # The code that created it is gone; record the gap loudly rather than loop on it.
            log.warning(
                "companion teardown has no step named %s installation_id=%s", name, installation_id
            )
        else:
            await step.teardown(context, dict(resources) if isinstance(resources, dict) else {})
        async with maker() as session:
            row = (
                await session.execute(
                    select(CompanionInstallation)
                    .where(CompanionInstallation.id == installation_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            if row is None:
                await session.rollback()
                break
            current = _recorded_steps(row)
            positions = [index for index, candidate in enumerate(current) if candidate == entry]
            if positions:
                del current[positions[-1]]
                torn_down = (row.external_resources or {}).get("torn_down")
                history = list(torn_down) if isinstance(torn_down, list) else []
                history.append(
                    {
                        "name": name,
                        "operation": entry.get("operation"),
                        "torn_down_at": clock().isoformat(),
                        "teardown": "done" if step is not None else "missing_step",
                    }
                )
                row.external_resources = {
                    **(row.external_resources or {}),
                    "steps": cast(list[JsonValue], current),
                    "torn_down": history,
                }
                flag_modified(row, "external_resources")
                torn.append(name)
            await session.commit()
    return torn


def _completed_names(installation: CompanionInstallation) -> frozenset[str]:
    """The steps already completed for the operation in flight."""
    return frozenset(
        str(entry.get("name"))
        for entry in _recorded_steps(installation)
        if entry.get("operation") == installation.operation
    )


async def claim_companion_lifecycle(
    db: AsyncSession,
    *,
    now: datetime,
    lease_seconds: int = COMPANION_LIFECYCLE_CLAIM_LEASE_SECONDS,
) -> ClaimedCompanionLifecycle | None:
    """Claim one due installation without blocking another worker's row.

    Due work is a queued installation whose backoff has elapsed, a running one the runtime
    reported ready or that passed its reporting deadline, or a failed one whose teardown is still
    owed. A row whose claim is still fresh belongs to another worker and is skipped; a stale claim
    (a crashed worker) is taken over.
    """
    stale_before = now - timedelta(seconds=lease_seconds)
    row = (
        await db.execute(
            select(CompanionInstallation)
            .where(
                CompanionInstallation.removed_at.is_(None),
                or_(
                    CompanionInstallation.claim_id.is_(None),
                    CompanionInstallation.claimed_at <= stale_before,
                ),
                or_(
                    and_(
                        CompanionInstallation.status == COMPANION_INSTALLATION_STATUS_QUEUED,
                        or_(
                            CompanionInstallation.next_attempt_at.is_(None),
                            CompanionInstallation.next_attempt_at <= now,
                        ),
                    ),
                    # Running rows are due when the runtime reported ready (due at once) and when
                    # the reporting deadline passed with no report (every report pushes it out).
                    and_(
                        CompanionInstallation.status == COMPANION_INSTALLATION_STATUS_RUNNING,
                        CompanionInstallation.next_attempt_at <= now,
                    ),
                    # A failed job whose teardown has not finished; NULL means nothing is left.
                    and_(
                        CompanionInstallation.status == COMPANION_INSTALLATION_STATUS_FAILED,
                        CompanionInstallation.next_attempt_at <= now,
                    ),
                ),
            )
            .order_by(
                CompanionInstallation.next_attempt_at.asc().nulls_first(),
                CompanionInstallation.created_at,
                CompanionInstallation.id,
            )
            .limit(1)
            .with_for_update(skip_locked=True)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    claim_id = uuid.uuid4()
    row.claim_id = claim_id
    row.claimed_at = now
    row.attempt_count += 1
    await db.flush()
    return ClaimedCompanionLifecycle(installation_id=row.id, claim_id=claim_id)


async def _locked(db: AsyncSession, claim: ClaimedCompanionLifecycle) -> CompanionInstallation:
    """The claimed row, locked and re-read, or ``CompanionLifecycleClaimLostError``."""
    row = (
        await db.execute(
            select(CompanionInstallation)
            .where(CompanionInstallation.id == claim.installation_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if row is None or row.claim_id != claim.claim_id:
        raise CompanionLifecycleClaimLostError("companion lifecycle claim changed")
    return row


def _set_edge(row: CompanionInstallation, **values: JsonValue) -> None:
    """Merge ``values`` into ``external_resources["edge"]``; a ``None`` value removes its key."""
    resources = dict(row.external_resources or {})
    current = resources.get("edge")
    edge: JsonObject = dict(current) if isinstance(current, dict) else {}
    for key, value in values.items():
        if value is None:
            edge.pop(key, None)
        else:
            edge[key] = value
    resources["edge"] = edge
    row.external_resources = resources
    flag_modified(row, "external_resources")


def _release(row: CompanionInstallation) -> None:
    row.claim_id = None
    row.claimed_at = None


class CompanionLifecycleWorker:
    """Advance companion installations through their lifecycle, durably."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], datetime] = _utcnow,
        poll_interval_seconds: float = 1.0,
        lease_seconds: int = COMPANION_LIFECYCLE_CLAIM_LEASE_SECONDS,
        provisioning_steps: tuple[ProvisioningStep, ...] | None = None,
        completion_steps: tuple[ProvisioningStep, ...] | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._clock = clock
        self._poll_interval_seconds = poll_interval_seconds
        self._lease_seconds = lease_seconds
        self._provisioning_steps = (
            PROVISIONING_STEPS if provisioning_steps is None else provisioning_steps
        )
        self._completion_steps = COMPLETION_STEPS if completion_steps is None else completion_steps

    async def run_once(self) -> UUID | None:
        """Claim and advance one due installation; ``None`` when nothing is due."""
        async with self._sessionmaker() as db:
            claim = await claim_companion_lifecycle(
                db, now=self._clock(), lease_seconds=self._lease_seconds
            )
            await db.commit()
        if claim is None:
            return None
        try:
            await self._advance(claim)
        except asyncio.CancelledError:
            raise
        except CompanionLifecycleClaimLostError:
            log.info("companion lifecycle claim lost installation_id=%s", claim.installation_id)
        except Exception:  # noqa: BLE001 - an unexpected error stays durable retry work.
            log.exception(
                "companion lifecycle pass failed installation_id=%s", claim.installation_id
            )
            await self._release_with_backoff(claim)
        return claim.installation_id

    async def run_forever(self, stop: asyncio.Event | None = None) -> None:
        stop_event = stop or asyncio.Event()
        while not stop_event.is_set():
            try:
                installation_id = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one row cannot stop durable recovery.
                log.exception("companion lifecycle worker iteration failed")
                installation_id = None
            if installation_id is None:
                try:
                    installation_id = await self.probe_reachability_once()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - a probe failure is re-probed later.
                    log.exception("companion reachability probe iteration failed")
            if installation_id is None:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=self._poll_interval_seconds)
                except TimeoutError:
                    pass

    # -- reachability --------------------------------------------------------------------------

    async def probe_reachability_once(self) -> UUID | None:
        """Probe one complete installation whose last check is five minutes old or missing.

        The row is reserved in its own short transaction (``edge.probe_started_at``) and the
        probe runs with no lock held, so a slow edge never blocks another worker or a request;
        a reservation older than ``REACHABILITY_PROBE_LEASE_SECONDS`` is a crashed worker's and
        is taken over. ``None`` when nothing is due.
        """
        now = self._clock()
        edge = CompanionInstallation.external_resources["edge"]
        checked_at = sql_cast(edge[("reachability", "checked_at")].astext, DateTime(timezone=True))
        probe_started_at = sql_cast(edge["probe_started_at"].astext, DateTime(timezone=True))
        async with self._sessionmaker() as db:
            row = (
                await db.execute(
                    select(CompanionInstallation)
                    .where(
                        serving_installation_filter(),
                        CompanionInstallation.removed_at.is_(None),
                        or_(
                            checked_at.is_(None),
                            checked_at
                            <= now - timedelta(seconds=REACHABILITY_PROBE_INTERVAL_SECONDS),
                        ),
                        or_(
                            probe_started_at.is_(None),
                            probe_started_at
                            <= now - timedelta(seconds=REACHABILITY_PROBE_LEASE_SECONDS),
                        ),
                    )
                    .order_by(checked_at.asc().nulls_first(), CompanionInstallation.id)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
            ).scalar_one_or_none()
            if row is None:
                await db.commit()
                return None
            installation_id, hostname = row.id, row.hostname
            _set_edge(row, probe_started_at=now.isoformat())
            await db.commit()

        # Only the hostname is needed, so the probe gets a detached stand-in, not the session's row.
        reachability = await probe_edge_reachability(
            CompanionInstallation(id=installation_id, hostname=hostname), now=now
        )
        async with self._sessionmaker() as db:
            row = (
                await db.execute(
                    select(CompanionInstallation)
                    .where(CompanionInstallation.id == installation_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            if row is not None and installation_is_serving(row) and row.removed_at is None:
                _set_edge(row, reachability=reachability, probe_started_at=None)
            await db.commit()
        return installation_id

    # -- one pass -------------------------------------------------------------------------------

    async def _advance(self, claim: ClaimedCompanionLifecycle) -> None:
        """Re-derive what is due from the LOCKED row, then do it.

        Re-derived rather than trusted from claim time: a report can land between the claim and
        this pass, and the row as it is now is the only thing worth acting on.
        """
        now = self._clock()
        async with self._sessionmaker() as db:
            row = await _locked(db, claim)
            context = self._context(row)
            due = row.next_attempt_at is not None and row.next_attempt_at <= now
            if (
                row.status == COMPANION_INSTALLATION_STATUS_RUNNING
                and row.operation == COMPANION_OPERATION_UNINSTALL
                and due
            ):
                action = "uninstall"
            elif row.status == COMPANION_INSTALLATION_STATUS_QUEUED:
                action = "provision"
            elif row.status == COMPANION_INSTALLATION_STATUS_RUNNING and row.step == READY_STEP:
                action = "complete"
            elif row.status == COMPANION_INSTALLATION_STATUS_RUNNING and due:
                # Checked against the row as it is NOW: a report since the claim moved it out.
                action = "deadline"
            elif (
                row.status == COMPANION_INSTALLATION_STATUS_FAILED
                and row.next_attempt_at is not None
            ):
                action = "teardown"
            else:
                _release(row)
                await db.commit()
                return
            await db.commit()

        if action == "uninstall":
            # Never "fails": a step that raises leaves the uninstall running and due again after a
            # backoff (``run_once``), so billing and resources can never be left half-handled.
            await self._run_uninstall(claim, context)
            return
        try:
            if action == "provision":
                await self._run_pipeline(claim, context, self._provisioning_steps)
                await self._mark_running(claim)
                return
            if action == "complete":
                # The completion steps publish the hostname and start billing: the install's, once.
                steps = (
                    self._completion_steps
                    if context.operation == COMPANION_OPERATION_INSTALL
                    else ()
                )
                await self._run_pipeline(claim, context, steps)
                await self._mark_complete(claim)
                return
        except _StepFailedError as failure:
            # A step that raises fails the operation: the reason is redacted and everything this
            # operation already created is torn down, in this same pass. The one exception is a
            # transient failure of a step that allows retries -- it is retried with backoff until
            # its ``max_attempts`` are spent, and only then fails the operation.
            reason = await self._redacted(claim, failure.reason())
            if failure.retryable and await self._defer_step_retry(claim, failure, reason):
                return
            if not await self._fail(claim, reason):
                return
        else:
            if action == "deadline" and not await self._fail(
                claim, RUNTIME_DID_NOT_REPORT, update_error_code=UPDATE_ERROR_RUNTIME_DID_NOT_REPORT
            ):
                return
        await self._teardown(claim, context)

    async def _run_uninstall(
        self, claim: ClaimedCompanionLifecycle, context: LifecycleStepContext
    ) -> None:
        """One pass of the uninstall pipeline (plan 06-20, APP-07, APP-08).

        1. **Billing stops first**, once: the line item is stopped through the billing step's own
           teardown (idempotent; /v2's 404 is already stopped) and the stop is recorded in
           ``external_resources["uninstall"]["billing_stopped_at"]`` before anything else runs.
        2. **The runtime removes.** While this waits, the projection carries the installation with
           ``lifecycle: "remove"``. The pass ends here, due again at the removal deadline, until
           the runtime's ``removed`` report (which makes the row due at once) -- or until
           ``UNINSTALL_REMOVAL_DEADLINE_SECONDS`` pass without one, which records
           ``host_removal_unconfirmed`` and continues rather than leave resources and billing
           behind.
        3. **Everything the installation's steps created is torn down**, newest first
           (``teardown_installation_resources``).
        4. **The installation is removed**: ``removed_at`` set, the uninstall ``complete``; the
           projection no longer carries it and a reinstall is possible.
        """
        async with self._sessionmaker() as db:
            row = await _locked(db, claim)
            billing_stopped = isinstance(_uninstall_record(row).get("billing_stopped_at"), str)
            billing_resources = _newest_step_resources(row, BILLING_START_STEP.name)
            await db.commit()
        if not billing_stopped:
            await BILLING_START_STEP.teardown(context, billing_resources or {})
            async with self._sessionmaker() as db:
                row = await _locked(db, claim)
                record = _uninstall_record(row)
                record["billing_stopped_at"] = self._clock().isoformat()
                _set_resource(row, UNINSTALL_RECORD_KEY, record)
                await db.commit()

        now = self._clock()
        async with self._sessionmaker() as db:
            row = await _locked(db, claim)
            resources = row.external_resources or {}
            reported = isinstance(resources.get(REMOVED_REPORTED_AT_KEY), str)
            deadline = row.operation_started_at + timedelta(
                seconds=UNINSTALL_REMOVAL_DEADLINE_SECONDS
            )
            if not reported and now < deadline:
                row.next_attempt_at = deadline
                _release(row)
                await db.commit()
                return
            if not reported and resources.get(HOST_REMOVAL_UNCONFIRMED_KEY) is not True:
                # The control plane finishes without the runtime: its data may still be on the
                # host (withdrawn, never destroyed), which this marker says for the operator.
                _set_resource(row, HOST_REMOVAL_UNCONFIRMED_KEY, True)
                log.warning(
                    "companion uninstall removal unconfirmed after %ss installation_id=%s",
                    UNINSTALL_REMOVAL_DEADLINE_SECONDS,
                    claim.installation_id,
                )
            await db.commit()

        async with self._sessionmaker() as db:
            torn = await teardown_installation_resources(
                db,
                CompanionInstallation(
                    id=context.installation_id,
                    user_id=context.user_id,
                    environment_id=context.environment_id,
                    companion_key=context.companion_key,
                    operation=context.operation,
                    hostname=context.hostname,
                ),
                steps=(*self._provisioning_steps, *self._completion_steps),
                sessionmaker=self._sessionmaker,
                clock=self._clock,
            )

        async with self._sessionmaker() as db:
            row = await _locked(db, claim)
            moment = self._clock()
            record = _uninstall_record(row)
            record["removed_at"] = moment.isoformat()
            record["torn_down"] = cast(list[JsonValue], torn)
            _set_resource(row, UNINSTALL_RECORD_KEY, record)
            row.status = COMPANION_INSTALLATION_STATUS_COMPLETE
            row.completed_at = moment
            row.removed_at = moment
            row.error = None
            row.next_attempt_at = None
            _release(row)
            clerk_id = await db.scalar(select(User.clerk_id).where(User.id == row.user_id))
            await db.commit()
        if clerk_id:
            reset_companion_tools_cache_for(clerk_id)

    async def _fail(
        self,
        claim: ClaimedCompanionLifecycle,
        reason: str,
        *,
        update_error_code: str | None = None,
    ) -> bool:
        """Fail the in-flight operation (forward-only) and leave its teardown owed; keep the claim.

        A failed UPDATE also returns its pin to the digest it was accepted from, so the manifest
        never keeps naming a digest the runtime did not prove (T-06-18-03).

        Returns ``False`` -- and releases the claim -- when the job already left the states this
        worker may fail (a report completed or failed it meanwhile).
        """
        async with self._sessionmaker() as db:
            row = await _locked(db, claim)
            if row.status not in (
                COMPANION_INSTALLATION_STATUS_QUEUED,
                COMPANION_INSTALLATION_STATUS_RUNNING,
            ):
                _release(row)
                await db.commit()
                return False
            row.status = COMPANION_INSTALLATION_STATUS_FAILED
            row.error = reason[:MAX_INSTALL_REPORT_ERROR_CHARS]
            row.next_attempt_at = self._clock()
            if row.operation == COMPANION_OPERATION_UPDATE:
                revert_update_pin(
                    row,
                    error_code=update_error_code or UPDATE_ERROR_RUNTIME_DID_NOT_REPORT,
                    moment=self._clock(),
                )
                flag_modified(row, "external_resources")
            await db.commit()
        return True

    async def _defer_step_retry(
        self, claim: ClaimedCompanionLifecycle, failure: _StepFailedError, reason: str
    ) -> bool:
        """Count one failed attempt of a retryable step; ``True`` when the job waits for another.

        The count lives in ``external_resources["step_attempts"]["<operation>:<step>"]`` with the
        last (redacted) reason, so it survives a worker restart and never leaks across operations.
        While attempts remain, the job KEEPS its status -- a ready job stays ``running`` at
        ``READY_STEP`` -- and is due again after ``STEP_RETRY_BASE_SECONDS`` doubling per attempt
        (capped). ``False`` means the bound is spent or the job moved on; the caller fails it.
        """
        async with self._sessionmaker() as db:
            row = await _locked(db, claim)
            if row.status not in (
                COMPANION_INSTALLATION_STATUS_QUEUED,
                COMPANION_INSTALLATION_STATUS_RUNNING,
            ):
                return False
            now = self._clock()
            key = f"{row.operation}:{failure.step_name}"
            resources = dict(row.external_resources or {})
            recorded = resources.get("step_attempts")
            attempts_by_step: JsonObject = dict(recorded) if isinstance(recorded, dict) else {}
            previous = attempts_by_step.get(key)
            previous_count = previous.get("attempts") if isinstance(previous, dict) else None
            count = (previous_count if isinstance(previous_count, int) else 0) + 1
            attempts_by_step[key] = {
                "attempts": count,
                "last_error": reason[:MAX_INSTALL_REPORT_ERROR_CHARS],
                "failed_at": now.isoformat(),
            }
            resources["step_attempts"] = attempts_by_step
            row.external_resources = resources
            flag_modified(row, "external_resources")
            if count >= failure.max_attempts:
                await db.commit()
                return False
            backoff_seconds = min(
                STEP_RETRY_BASE_SECONDS * 2 ** (count - 1), COMPANION_LIFECYCLE_BACKOFF_CAP_SECONDS
            )
            row.next_attempt_at = now + timedelta(seconds=backoff_seconds)
            _release(row)
            await db.commit()
        log.warning(
            "companion lifecycle step %s will retry (attempt %s of %s) installation_id=%s",
            failure.step_name,
            count,
            failure.max_attempts,
            claim.installation_id,
        )
        return True

    async def _redacted(self, claim: ClaimedCompanionLifecycle, text: str) -> str:
        """``text`` redacted against the installation's own credentials, or a safe stand-in."""
        try:
            async with self._sessionmaker() as db:
                redact = await installation_redactor(db, installation_id=claim.installation_id)
                await db.rollback()
        except Exception:  # noqa: BLE001 - unreadable credentials: keep only the non-secret prefix.
            return text.split(":", 1)[0] + ": details withheld (credentials unreadable)"
        return redact(text)

    def _context(self, row: CompanionInstallation) -> LifecycleStepContext:
        return LifecycleStepContext(
            installation_id=row.id,
            user_id=row.user_id,
            environment_id=row.environment_id,
            companion_key=row.companion_key,
            operation=row.operation,
            hostname=row.hostname,
            sessionmaker=self._sessionmaker,
        )

    async def _run_pipeline(
        self,
        claim: ClaimedCompanionLifecycle,
        context: LifecycleStepContext,
        steps: tuple[ProvisioningStep, ...],
    ) -> None:
        """Run each step not yet completed for this operation, recording each as it completes.

        The row lock is NOT held while a step runs -- a step may call an external API for a while,
        and the runtime's reports must not queue behind it. The claim id fences the recording.
        """
        for step in steps:
            async with self._sessionmaker() as db:
                row = await _locked(db, claim)
                done = _completed_names(row)
                await db.commit()
            if step.name in done:
                continue
            try:
                resources = await step.provision(context)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - any step failure fails the operation.
                log.warning(
                    "companion lifecycle step %s failed installation_id=%s",
                    step.name,
                    claim.installation_id,
                )
                raise _StepFailedError(
                    step.name,
                    exc,
                    retryable=isinstance(exc, CompanionStepRetryableError),
                    max_attempts=step.max_attempts,
                ) from exc
            async with self._sessionmaker() as db:
                row = await _locked(db, claim)
                entry: JsonObject = {
                    "name": step.name,
                    "operation": row.operation,
                    "resources": resources or {},
                    "completed_at": self._clock().isoformat(),
                }
                row.external_resources = {
                    **(row.external_resources or {}),
                    "steps": [*_recorded_steps(row), entry],
                }
                flag_modified(row, "external_resources")
                await db.commit()

    async def _mark_running(self, claim: ClaimedCompanionLifecycle) -> None:
        """Provisioning is done: hand the installation to the projection, start the deadline."""
        async with self._sessionmaker() as db:
            row = await _locked(db, claim)
            if row.status != COMPANION_INSTALLATION_STATUS_QUEUED:
                _release(row)
                await db.commit()
                return
            now = self._clock()
            row.status = COMPANION_INSTALLATION_STATUS_RUNNING
            row.step = None
            row.error = None
            row.next_attempt_at = now + timedelta(seconds=REPORT_DEADLINE_SECONDS)
            _release(row)
            await db.commit()

    async def _mark_complete(self, claim: ClaimedCompanionLifecycle) -> None:
        """Complete the job and, in the same action, make its tools listable (APP-04)."""
        async with self._sessionmaker() as db:
            row = await _locked(db, claim)
            if row.status != COMPANION_INSTALLATION_STATUS_RUNNING or row.step != READY_STEP:
                # A report changed the job while the completion steps ran; the row as it is now
                # decides what happens next, on a later pass.
                _release(row)
                await db.commit()
                return
            row.status = COMPANION_INSTALLATION_STATUS_COMPLETE
            row.completed_at = self._clock()
            row.error = None
            row.next_attempt_at = None
            _release(row)
            clerk_id = await db.scalar(select(User.clerk_id).where(User.id == row.user_id))
            await db.commit()
        if clerk_id:
            reset_companion_tools_cache_for(clerk_id)

    def _step_named(self, name: str) -> ProvisioningStep | None:
        for step in (*self._provisioning_steps, *self._completion_steps):
            if step.name == name:
                return step
        return None

    async def _teardown(
        self, claim: ClaimedCompanionLifecycle, context: LifecycleStepContext
    ) -> None:
        """Undo every step the failed operation completed, newest first, one durable step at a time.

        Each undone step moves from ``steps`` to ``torn_down`` in its own commit, so a teardown
        interrupted halfway resumes with exactly what is left. Steps recorded for an EARLIER
        operation (an install's resources, under a later update) are not this operation's to undo.
        A teardown that raises leaves the job failed and due again after a backoff.
        """
        while True:
            async with self._sessionmaker() as db:
                row = await _locked(db, claim)
                owed = [
                    entry
                    for entry in _recorded_steps(row)
                    if entry.get("operation") == row.operation
                ]
                await db.commit()
            if not owed:
                break
            entry = owed[-1]
            name = str(entry.get("name"))
            step = self._step_named(name)
            resources = entry.get("resources")
            if step is None:
                # The code that created it is gone; record the gap loudly rather than loop on it.
                log.warning(
                    "companion lifecycle teardown has no step named %s installation_id=%s",
                    name,
                    claim.installation_id,
                )
            else:
                await step.teardown(context, resources if isinstance(resources, dict) else {})
            async with self._sessionmaker() as db:
                row = await _locked(db, claim)
                steps = _recorded_steps(row)
                index = max(
                    position
                    for position, candidate in enumerate(steps)
                    if candidate.get("name") == name and candidate.get("operation") == row.operation
                )
                del steps[index]
                torn_down = (row.external_resources or {}).get("torn_down")
                history = list(torn_down) if isinstance(torn_down, list) else []
                history.append(
                    {
                        "name": name,
                        "operation": row.operation,
                        "torn_down_at": self._clock().isoformat(),
                        "teardown": "done" if step is not None else "missing_step",
                    }
                )
                row.external_resources = {
                    **(row.external_resources or {}),
                    "steps": cast(list[JsonValue], steps),
                    "torn_down": history,
                }
                flag_modified(row, "external_resources")
                await db.commit()

        async with self._sessionmaker() as db:
            row = await _locked(db, claim)
            if row.status == COMPANION_INSTALLATION_STATUS_FAILED:
                # Nothing is owed any more: a NULL due time takes the row out of the claim scan.
                row.next_attempt_at = None
            _release(row)
            await db.commit()

    async def _release_with_backoff(self, claim: ClaimedCompanionLifecycle) -> None:
        """An unexpected failure: keep the state, retry later with capped exponential backoff."""
        try:
            async with self._sessionmaker() as db:
                row = await _locked(db, claim)
                backoff_seconds = min(
                    2 ** min(max(row.attempt_count - 1, 0), 10),
                    COMPANION_LIFECYCLE_BACKOFF_CAP_SECONDS,
                )
                row.next_attempt_at = self._clock() + timedelta(seconds=backoff_seconds)
                _release(row)
                await db.commit()
        except CompanionLifecycleClaimLostError:
            log.info("companion lifecycle claim lost installation_id=%s", claim.installation_id)


__all__ = [
    "COMPANION_LIFECYCLE_BACKOFF_CAP_SECONDS",
    "COMPANION_LIFECYCLE_CLAIM_LEASE_SECONDS",
    "COMPLETION_STEPS",
    "PROVISIONING_STEPS",
    "REACHABILITY_PROBE_LEASE_SECONDS",
    "REPORT_DEADLINE_SECONDS",
    "STEP_RETRY_BASE_SECONDS",
    "ClaimedCompanionLifecycle",
    "CompanionLifecycleClaimLostError",
    "CompanionLifecycleWorker",
    "JsonObject",
    "LifecycleStepContext",
    "ProvisionAction",
    "ProvisioningStep",
    "TeardownAction",
    "claim_companion_lifecycle",
    "teardown_installation_resources",
]
