"""Server-side billing gate (ACCESS-02, D-09 — "through the door" means a
real subscription object is active; trialing counts).

`ensure_unlocked` runs after principal resolution on every authenticated
path (Clerk JWT, API key, dev bypass — see `app.core.auth`), so web, CLI
and MCP are closed at once. It reads subscription state from the /v2
deploy service (`GET {deploy_api_url}/v2/subscription?owner_user_id=…`,
where the owner is the Clerk user id derived by
`app.services.hosted_owner.hosted_owner_id` — 06.2 D-09, never the internal
`users.id` UUID) — NEVER from Stripe directly (ADR-0005: billing surfaces
live behind the /v2 contract; `mock_deploy_api.py` is the executable
reference).

iforge keeps this app-wide gate as deliberate policy (06.3 D-03). Upstream
has no app-wide gate and enforces payment only at hosted compute, where a
lapsed subscription is refused with 402 `payment_required`. Here every
authenticated route is gated except those depending on
`app.core.auth.get_auth_billing_exempt`, the only exempt dependency (identity
bootstrap: GET /auth/me), pinned by `tests/test_billing_gate_allowlist.py`.
GET /v1/access/status is the one `fresh=True` caller: the post-checkout poll
must observe an unlock without waiting out the negative cache below.

Design:
- Short-circuits BEFORE any network I/O: gate disabled, sticky
  `users.billing_unlocked_at`, or the billing exemption
  (`users.invite_exempted_at`; the column name is kept, and it is set for
  grandfathered, admin-provisioned and partner-tenant rows).
- A principal with no hosted owner (no Clerk id: partner tenants) never
  reaches /v2; it answers 403 billing_required, as `subscription: null`
  would, without network I/O (06.2 D-09).
- Unlock: the first active/trialing read persists `billing_unlocked_at`
  (with a `billing.unlocked` audit event).
- Lapse re-check (INT-1, superseding 06.3 D-03's sticky unlock): an unlocked
  user's subscription is re-read at most once per
  `billing_recheck_interval_seconds`. A re-read that finds it neither active
  nor trialing stamps `billing_lapsed_at` (`billing.lapse_observed`) and the
  user keeps access; once the lapse has lasted `billing_lapse_grace_seconds`
  (one day) the unlock is cleared (`billing.relocked`) and the user is
  refused like any unpaid user. A paid re-read clears the lapse
  (`billing.lapse_cleared`). A re-read that cannot reach /v2 keeps access:
  the user has paid before, and the outage is retried after the 60s window.
- Fail CLOSED: transport errors / non-200s from /v2 answer
  503 {"code": "billing_unavailable"} — grant-on-outage is the exact
  failure mode Pitfall 6 names.
- A 60s in-process TTL cache covers both the `billing_required` and
  `billing_unavailable` outcomes so locked users cannot hammer /v2
  (threat T-03-15).
- `fresh=True` ignores a cached negative outcome once
  `_FRESH_MIN_INTERVAL_SECONDS` have passed since that user's last /v2 read,
  and answers the cached outcome inside that window, so the poll stays
  bounded to one /v2 read per user per interval (T-03-15). The fresh path
  claims the read slot synchronously, before its first `await`, so a burst of
  concurrent polls inside one process cannot all pass the "due" check.
- Both the negative cache and the read-slot times live in process memory, so
  every bound above is PER PROCESS: with N workers a client spreading polls
  across them gets up to N /v2 reads per interval.
"""

from __future__ import annotations

from datetime import UTC, datetime
from time import monotonic
from uuid import UUID

import httpx
from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.user import User
from app.services.audit import record_control_plane_audit
from app.services.hosted_owner import hosted_owner_id

_UNLOCKING_STATUSES = frozenset({"active", "trialing"})
_CACHE_TTL_SECONDS = 60.0
_FRESH_MIN_INTERVAL_SECONDS = 3.0
_DEPLOY_API_TIMEOUT_SECONDS = 5.0

# users.id -> (expires_at_monotonic, "billing_required" | "billing_unavailable").
# The cache key stays internal; only the /v2 wire uses the Clerk owner id.
# Only negative/unavailable outcomes are cached: positive outcomes persist as
# billing_unlocked_at and never consult this cache again.
_negative_cache: dict[UUID, tuple[float, str]] = {}

# users.id -> monotonic time of that user's most recent /v2 read (or of the
# fresh-path slot claim that precedes it). Bounds the fresh path to one /v2
# read per user per `_FRESH_MIN_INTERVAL_SECONDS`, per process.
_last_v2_read: dict[UUID, float] = {}

# Test seam (03-04 transport-injection pattern): tests point this at an
# httpx.MockTransport standing in for /v2. None means httpx's default
# transport (real network).
_transport_override: httpx.AsyncBaseTransport | None = None


def reset_billing_gate_cache() -> None:
    """Clear the negative TTL cache and last-read times (test isolation helper)."""
    _negative_cache.clear()
    _last_v2_read.clear()


def _fresh_read_due(user_id: UUID) -> bool:
    last = _last_v2_read.get(user_id)
    return last is None or monotonic() - last >= _FRESH_MIN_INTERVAL_SECONDS


def _cached_outcome(user_id: UUID) -> str | None:
    entry = _negative_cache.get(user_id)
    if entry is None:
        return None
    expires_at, outcome = entry
    if expires_at <= monotonic():
        _negative_cache.pop(user_id, None)
        return None
    return outcome


def _cache_outcome(user_id: UUID, outcome: str) -> None:
    _negative_cache[user_id] = (monotonic() + _CACHE_TTL_SECONDS, outcome)


def _billing_required() -> HTTPException:
    return HTTPException(status.HTTP_403_FORBIDDEN, detail={"code": "billing_required"})


def _billing_unavailable() -> HTTPException:
    return HTTPException(
        status.HTTP_503_SERVICE_UNAVAILABLE, detail={"code": "billing_unavailable"}
    )


async def _read_subscription_status(user_id: UUID, owner_id: str) -> str | None:
    """Read the owner's subscription status from /v2. `owner_id` is the
    hosted owner (the Clerk user id, D-09) sent on the wire; `user_id` keys
    only the in-process negative cache. Returns the status string, None for
    "no subscription", and raises the fail-closed 503 for any transport
    error or non-200 answer (caching the unavailable outcome)."""
    url = f"{settings.deploy_api_url.rstrip('/')}/v2/subscription"
    headers = {
        "Authorization": f"Bearer {settings.deploy_api_key}",
        "User-Agent": "iforge-backend/1.0",
    }
    _last_v2_read[user_id] = monotonic()
    try:
        async with httpx.AsyncClient(
            transport=_transport_override, timeout=_DEPLOY_API_TIMEOUT_SECONDS
        ) as client:
            response = await client.get(url, params={"owner_user_id": owner_id}, headers=headers)
        if response.status_code != 200:
            # 5xx is an outage; an unexpected 4xx is a misconfigured
            # deploy_api_url/key. Either way we cannot assert absence of a
            # subscription, so fail closed rather than refuse payment state.
            _cache_outcome(user_id, "billing_unavailable")
            raise _billing_unavailable()
        payload = response.json()
    except httpx.HTTPError as error:
        _cache_outcome(user_id, "billing_unavailable")
        raise _billing_unavailable() from error
    except ValueError as error:  # malformed JSON body
        _cache_outcome(user_id, "billing_unavailable")
        raise _billing_unavailable() from error

    subscription = payload.get("subscription") if isinstance(payload, dict) else None
    if not isinstance(subscription, dict):
        return None
    raw_status = subscription.get("status")
    return raw_status if isinstance(raw_status, str) else None


def _recheck_due(user: User) -> bool:
    """Whether an unlocked user's subscription is due a /v2 re-read. The clock
    starts at the unlock itself, so a fresh unlock is not re-read at once, and a
    read this process made inside the 60s window (an outage, or a concurrent
    request that claimed the slot) defers it."""
    last_read = _last_v2_read.get(user.id)
    if last_read is not None and monotonic() - last_read < _CACHE_TTL_SECONDS:
        return False
    return _recheck_due_ignoring_slot(user)


def _copy_billing_state(source: User, user: User) -> None:
    user.billing_unlocked_at = source.billing_unlocked_at
    user.billing_checked_at = source.billing_checked_at
    user.billing_lapsed_at = source.billing_lapsed_at


async def _recheck_unlocked(db: AsyncSession, user: User) -> str:
    """Re-read an unlocked user's subscription (INT-1). Answers "unlocked" when
    access continues (paid, inside the grace period, or /v2 unreachable),
    "relocked" when the lapse outlasted the grace and the unlock was cleared,
    and "stale" when the authoritative row is already locked."""
    # Claim the read slot before the first `await`, as the fresh path does, so
    # a burst of this user's requests makes one /v2 read per process.
    _last_v2_read[user.id] = monotonic()
    db_user = await db.get(User, user.id)
    target = db_user if db_user is not None else user
    if target is not user:
        _copy_billing_state(target, user)
    if target.billing_unlocked_at is None:
        return "stale"
    if target.invite_exempted_at is not None or not _recheck_due_ignoring_slot(target):
        return "unlocked"
    owner_id = hosted_owner_id(target)
    if owner_id is None:
        # No hosted owner to ask (partner tenant): nothing can show a lapse.
        return "unlocked"
    try:
        status_value = await _read_subscription_status(user.id, owner_id)
    except HTTPException:
        lapsed_at = target.billing_lapsed_at
        if lapsed_at is None or not _grace_over(lapsed_at, datetime.now(UTC)):
            _negative_cache.pop(user.id, None)
            return "unlocked"
        # The outage tolerance covers only a paid user or a lapse still inside
        # its grace. Past the grace, an unanswered read must not extend access:
        # relock, and the locked path fails closed until /v2 answers.
        status_value = None
        v2_unavailable = True
    else:
        v2_unavailable = False

    now = datetime.now(UTC)
    target.billing_checked_at = now
    outcome = "unlocked"
    action: str | None = None
    details: dict[str, object] = {"subscription_status": status_value}
    if v2_unavailable:
        details["v2_unavailable"] = True
    if status_value in _UNLOCKING_STATUSES:
        if target.billing_lapsed_at is not None:
            action = "billing.lapse_cleared"
            details["lapse_observed_at"] = target.billing_lapsed_at.isoformat()
        target.billing_lapsed_at = None
    elif target.billing_lapsed_at is None:
        target.billing_lapsed_at = now
        action = "billing.lapse_observed"
    elif _grace_over(target.billing_lapsed_at, now):
        action = "billing.relocked"
        details["lapse_observed_at"] = target.billing_lapsed_at.isoformat()
        details["previous_billing_unlocked_at"] = target.billing_unlocked_at.isoformat()
        target.billing_unlocked_at = None
        target.billing_lapsed_at = None
        outcome = "relocked"
        _cache_outcome(user.id, "billing_required")
    if target is not user:
        _copy_billing_state(target, user)
    if action is not None:
        record_control_plane_audit(
            db,
            actor_type="user",
            actor_user_id=user.id,
            target_user_id=user.id,
            action=action,
            resource_type="user",
            resource_id=str(user.id),
            source="billing_gate",
            details=details,
        )
    await db.flush()
    # As with the unlock: plain GETs never commit, and the re-read must stick.
    await db.commit()
    return outcome


def _grace_over(lapsed_at: datetime, now: datetime) -> bool:
    return (now - lapsed_at).total_seconds() >= settings.billing_lapse_grace_seconds


def _recheck_due_ignoring_slot(user: User) -> bool:
    checked = user.billing_checked_at or user.billing_unlocked_at
    if checked is None:
        return True
    elapsed = (datetime.now(UTC) - checked).total_seconds()
    return elapsed >= settings.billing_recheck_interval_seconds


async def ensure_unlocked(db: AsyncSession, user: User, *, fresh: bool = False) -> None:
    """Refuse authenticated access until the user's /v2 subscription is
    active or trialing (D-09), with the INT-1 lapse re-check and D-07
    exemptions.

    Raises HTTPException(403 {"code": "billing_required"}) for locked users
    and HTTPException(503 {"code": "billing_unavailable"}) when /v2 cannot
    answer (fail closed). Passes silently otherwise.

    `fresh=True` (GET /v1/access/status only) skips a cached negative outcome
    once `_FRESH_MIN_INTERVAL_SECONDS` have passed since the user's last /v2
    read; inside that window it answers the cached outcome. The slot is
    claimed before the first `await`, so concurrent fresh calls in one process
    make at most one /v2 read per interval (the bound is per process). It
    never grants on error: a failed re-read is still the fail-closed 503.
    """
    if not settings.billing_gate_enabled:
        return
    if user.invite_exempted_at is not None:
        return
    # A relock in this process caches `billing_required`; an unlocked snapshot
    # (the API-key auth cache) must not outrun it.
    if user.billing_unlocked_at is not None and _cached_outcome(user.id) != "billing_required":
        if not _recheck_due(user):
            return
        outcome = await _recheck_unlocked(db, user)
        if outcome == "unlocked":
            return
        if outcome == "relocked":
            raise _billing_required()
        # "stale": another request or worker already cleared the unlock this
        # snapshot still carries; continue as the locked user it now is.

    cached = _cached_outcome(user.id)
    if cached is not None and fresh and _fresh_read_due(user.id):
        # Claim the read slot synchronously, before any `await` below can
        # yield: concurrent fresh calls for this user then see the slot taken
        # and answer the cached outcome instead of each reading /v2.
        _last_v2_read[user.id] = monotonic()
        cached = None
    if cached == "billing_required":
        raise _billing_required()
    if cached == "billing_unavailable":
        raise _billing_unavailable()

    # The passed user may be a detached snapshot (the API-key auth cache
    # reconstructs User objects); re-read the authoritative row so an unlock
    # persisted by another request/worker short-circuits without /v2 I/O.
    db_user = await db.get(User, user.id)
    if db_user is not None and db_user.billing_unlocked_at is not None:
        user.billing_unlocked_at = db_user.billing_unlocked_at
        return
    if db_user is not None and db_user.invite_exempted_at is not None:
        user.invite_exempted_at = db_user.invite_exempted_at
        return

    # D-09: /v2 knows the owner by Clerk user id. A principal without one
    # (partner tenant) has no hosted subscription and never reaches /v2.
    owner_id = hosted_owner_id(db_user if db_user is not None else user)
    if owner_id is None:
        _cache_outcome(user.id, "billing_required")
        raise _billing_required()

    status_value = await _read_subscription_status(user.id, owner_id)
    if status_value in _UNLOCKING_STATUSES:
        now = datetime.now(UTC)
        target = db_user if db_user is not None else user
        target.billing_unlocked_at = now
        target.billing_checked_at = now
        target.billing_lapsed_at = None
        if target is not user:
            _copy_billing_state(target, user)
        _negative_cache.pop(user.id, None)
        _last_v2_read.pop(user.id, None)
        record_control_plane_audit(
            db,
            actor_type="user",
            actor_user_id=user.id,
            target_user_id=user.id,
            action="billing.unlocked",
            resource_type="user",
            resource_id=str(user.id),
            source="billing_gate",
            details={"subscription_status": status_value},
        )
        await db.flush()
        # The sticky unlock must survive this request even when the handler
        # never commits (plain GETs) — flush alone would silently discard it
        # at session close and re-consult /v2 on every request.
        await db.commit()
        return

    _cache_outcome(user.id, "billing_required")
    raise _billing_required()
