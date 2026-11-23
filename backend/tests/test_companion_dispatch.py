"""The signed pre-authorized call and its dispatch queue (D-02).

D-02 splits the companion call path: the control plane polices, the runtime executes. The wire
object between them is a signed pre-authorized call that carries a CALL DESCRIPTOR and nothing
else -- no credential, no secret, no bearer token. These tests are the assertion that the split
is real rather than aspirational:

* the minted payload's key set is checked against ``SECRETISH_KEY_PARTS`` itself, imported from
  ``app.services.audit`` rather than restated, so a future claim named ``*_secret`` fails here;
* ``verify_dispatch_call`` is asserted to RAISE on a wrong signing key, an expired ``exp`` and a
  foreign ``aud`` -- three separate assertions, because a decoder that silently accepts any one
  of them turns the envelope into an unauthenticated instruction channel into a tenant network;
* the queue is asserted to hand one row to exactly ONE claimer under real concurrency, which
  needs committed rows and therefore the ``committed_db`` lane.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.models.companion_dispatch import CompanionDispatchCall
from app.services.audit import SECRETISH_KEY_PARTS
from app.services.companion_dispatch import (
    COMPANION_DISPATCH_AUDIENCE,
    CompanionDispatchArgumentError,
    CompanionDispatchClaimError,
    CompanionDispatchTokenError,
    MintedDispatchCall,
    claim_next_call,
    complete_call,
    enqueue_call,
    mint_dispatch_call,
    verify_dispatch_call,
)
from tests.conftest import create_env_with_project


def _mint() -> MintedDispatchCall:
    return mint_dispatch_call(
        companion_key="filebrowser",
        verb="list_dir",
        method="GET",
        path_template="/api/resources",
        arguments={"path": "/"},
    )


def test_minted_payload_carries_the_call_descriptor_and_nothing_else() -> None:
    minted = _mint()

    assert set(minted.payload) == {
        "companion_key",
        "verb",
        "method",
        "path_template",
        "arguments",
        "jti",
        "aud",
        "exp",
    }
    assert minted.payload["companion_key"] == "filebrowser"
    assert minted.payload["verb"] == "list_dir"
    assert minted.payload["method"] == "GET"
    assert minted.payload["path_template"] == "/api/resources"
    assert minted.payload["arguments"] == {"path": "/"}
    assert minted.payload["aud"] == COMPANION_DISPATCH_AUDIENCE


def test_minted_payload_contains_no_key_that_trips_the_shipped_redactor() -> None:
    # Asserted against the imported tuple rather than a hand-copied list: a claim added later
    # whose name matches a SECRETISH part must fail HERE, not in a reviewer's head.
    minted = _mint()

    for key in minted.payload:
        normalized = key.lower().replace("-", "_")
        for part in SECRETISH_KEY_PARTS:
            assert part not in normalized, f"{key} looks like credential material"


def test_verify_round_trips_a_freshly_minted_call() -> None:
    minted = _mint()

    payload = verify_dispatch_call(minted.token)

    assert payload["jti"] == minted.jti
    assert payload["companion_key"] == "filebrowser"


def test_verify_raises_on_a_token_signed_with_a_different_key() -> None:
    minted = _mint()
    previous = settings.encryption_key
    settings.encryption_key = "0" * 64
    try:
        with pytest.raises(CompanionDispatchTokenError):
            verify_dispatch_call(minted.token)
    finally:
        settings.encryption_key = previous


def test_verify_raises_on_an_expired_token() -> None:
    minted = mint_dispatch_call(
        companion_key="filebrowser",
        verb="list_dir",
        method="GET",
        path_template="/api/resources",
        arguments={},
        ttl_seconds=1,
        now=datetime.now(UTC) - timedelta(minutes=5),
    )

    with pytest.raises(CompanionDispatchTokenError):
        verify_dispatch_call(minted.token)


def test_verify_raises_on_a_token_minted_for_another_audience() -> None:
    # Hand-rolled with the SAME signing key: the point is that `aud` is asserted after decode,
    # not that an attacker lacks the key. A decoder that ignores `aud` accepts any token the
    # control plane ever signed for any purpose.
    foreign = jwt.encode(
        {
            "companion_key": "filebrowser",
            "verb": "list_dir",
            "method": "GET",
            "path_template": "/api/resources",
            "arguments": {},
            "jti": uuid4().hex,
            "aud": "some-other-audience",
            "exp": datetime.now(UTC) + timedelta(seconds=30),
        },
        settings.encryption_key,
        algorithm="HS256",
    )

    with pytest.raises(CompanionDispatchTokenError):
        verify_dispatch_call(foreign)


async def _environment(db_session: AsyncSession, seed_user):
    return await create_env_with_project(
        db_session,
        user_id=seed_user.id,
        machine_id=f"dispatch-{uuid4().hex[:8]}",
        machine_name="Dispatch Test Agent",
    )


async def test_enqueue_then_claim_hands_back_the_queued_call(db_session, seed_user) -> None:
    env = await _environment(db_session, seed_user)
    minted = _mint()

    await enqueue_call(
        db_session,
        user_id=seed_user.id,
        environment_id=env.id,
        companion_key="filebrowser",
        verb="list_dir",
        tool_class="read",
        minted=minted,
    )

    claimed = await claim_next_call(db_session, environment_id=env.id, claim_id="claim-one")

    assert claimed is not None
    assert claimed.call_jti == minted.jti
    assert claimed.status == "claimed"
    assert claimed.poll_claim_id == "claim-one"


async def test_claim_ignores_a_call_whose_deadline_has_passed(db_session, seed_user) -> None:
    env = await _environment(db_session, seed_user)
    minted = mint_dispatch_call(
        companion_key="filebrowser",
        verb="list_dir",
        method="GET",
        path_template="/api/resources",
        arguments={},
        ttl_seconds=1,
        now=datetime.now(UTC) - timedelta(minutes=5),
    )
    await enqueue_call(
        db_session,
        user_id=seed_user.id,
        environment_id=env.id,
        companion_key="filebrowser",
        verb="list_dir",
        tool_class="read",
        minted=minted,
    )

    assert await claim_next_call(db_session, environment_id=env.id) is None


async def test_complete_call_writes_the_result_for_the_owning_claim(db_session, seed_user) -> None:
    env = await _environment(db_session, seed_user)
    minted = _mint()
    await enqueue_call(
        db_session,
        user_id=seed_user.id,
        environment_id=env.id,
        companion_key="filebrowser",
        verb="list_dir",
        tool_class="read",
        minted=minted,
    )
    await claim_next_call(db_session, environment_id=env.id, claim_id="claim-one")

    row = await complete_call(
        db_session,
        call_jti=minted.jti,
        poll_claim_id="claim-one",
        result={"status": 200, "body": "{}"},
    )

    assert row.status == "completed"
    assert row.result == {"status": 200, "body": "{}"}


async def test_complete_call_with_a_foreign_claim_raises_and_changes_nothing(
    db_session, seed_user
) -> None:
    env = await _environment(db_session, seed_user)
    minted = _mint()
    await enqueue_call(
        db_session,
        user_id=seed_user.id,
        environment_id=env.id,
        companion_key="filebrowser",
        verb="list_dir",
        tool_class="read",
        minted=minted,
    )
    await claim_next_call(db_session, environment_id=env.id, claim_id="claim-one")

    with pytest.raises(CompanionDispatchClaimError):
        await complete_call(
            db_session,
            call_jti=minted.jti,
            poll_claim_id="claim-two",
            result={"status": 200},
        )

    # Re-read rather than trusting the in-memory object: the assertion is about what was
    # PERSISTED. `expire_all` forces the round trip, and autoflush means an implementation that
    # mutated the row before raising would have its mutation flushed and caught here rather than
    # hidden in the identity map.
    db_session.expire_all()
    row = (
        await db_session.execute(
            select(CompanionDispatchCall).where(CompanionDispatchCall.call_jti == minted.jti)
        )
    ).scalar_one()
    assert row.status == "claimed"
    assert row.result is None


@pytest.mark.committed_db
async def test_two_concurrent_claimers_receive_the_row_at_most_once(
    db_session, seed_user, engine
) -> None:
    env = await _environment(db_session, seed_user)
    minted = _mint()
    await enqueue_call(
        db_session,
        user_id=seed_user.id,
        environment_id=env.id,
        companion_key="filebrowser",
        verb="list_dir",
        tool_class="read",
        minted=minted,
    )
    await db_session.commit()

    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessionmaker() as first, sessionmaker() as second:
            first_claim = await claim_next_call(first, environment_id=env.id, claim_id="first")
            # `first` still holds the row lock in an OPEN transaction, which is exactly the
            # window `skip_locked` exists for: the second poller must come back empty rather
            # than block or, worse, hand the same call to two executors.
            second_claim = await claim_next_call(second, environment_id=env.id, claim_id="second")

            assert [first_claim is not None, second_claim is not None].count(True) == 1

            await first.commit()
            await second.rollback()
    finally:
        async with sessionmaker() as cleanup:
            await cleanup.execute(
                CompanionDispatchCall.__table__.delete().where(
                    CompanionDispatchCall.call_jti == minted.jti
                )
            )
            await cleanup.commit()


async def test_a_call_whose_arguments_would_not_survive_storage_is_refused(
    db_session, seed_user
) -> None:
    """What the executor runs must be what the approver approved. CR-01.

    `enqueue_call` persists arguments through the SHIPPED audit redactor (T-5-10), which
    truncates every string at 500 characters with no marker. But the queued row is ALSO the
    dispatch source of truth: `_claim_envelope` re-mints from `row.arguments`, discarding the
    freshly minted token. Meanwhile `canonical_action_hash` -- the digest the human approves and
    the provenance row records -- covers the FULL argument.

    Nothing compared the two, so a `delete_path` on a 520-character path whose 501st character is
    `/` dispatched a DELETE against the intended file's PARENT DIRECTORY, approved and logged as
    the child. `_MAX_ARGUMENT_VALUE_LENGTH` is 1024, so every length in 501..1024 was in that gap.

    Refusing at the boundary is the fix rather than storing untruncated, because the redaction is
    load-bearing: an argument value that trips `SECRETISH_KEY_PARTS` must not be persisted
    verbatim. Both divergences are the same defect -- the stored row is not the validated call --
    so the guard asserts ROUND-TRIP EQUALITY rather than checking length alone.
    """
    env = await _environment(db_session, seed_user)
    long_path = "/" + ("a" * 499) + "/" + ("b" * 19)
    assert len(long_path) == 520
    minted = mint_dispatch_call(
        companion_key="filebrowser",
        verb="delete_path",
        method="DELETE",
        path_template="/api/resources",
        arguments={"path": long_path},
    )

    with pytest.raises(CompanionDispatchArgumentError):
        await enqueue_call(
            db_session,
            user_id=seed_user.id,
            environment_id=env.id,
            companion_key="filebrowser",
            verb="delete_path",
            tool_class="destroy",
            minted=minted,
        )

    await db_session.rollback()
    rows = (await db_session.execute(select(CompanionDispatchCall))).scalars().all()
    assert rows == [], "a refused call must leave no queued row behind"


async def test_a_redacted_argument_value_is_refused_for_the_same_reason(
    db_session, seed_user
) -> None:
    """The other half of the same divergence, and the reason the guard is not a length check.

    A key tripping `SECRETISH_KEY_PARTS` is rewritten to `[REDACTED]` on the way into the row.
    Dispatching that would send the literal string `[REDACTED]` to the companion as the argument
    the approver authorized -- a different call, arrived at by a different mechanism than
    truncation but with the same consequence.
    """
    env = await _environment(db_session, seed_user)
    secretish_key = f"{next(iter(SECRETISH_KEY_PARTS))}_value"
    minted = mint_dispatch_call(
        companion_key="filebrowser",
        verb="list_dir",
        method="GET",
        path_template="/api/resources",
        arguments={"path": "/", secretish_key: "not-a-real-secret"},
    )

    with pytest.raises(CompanionDispatchArgumentError):
        await enqueue_call(
            db_session,
            user_id=seed_user.id,
            environment_id=env.id,
            companion_key="filebrowser",
            verb="list_dir",
            tool_class="read",
            minted=minted,
        )
