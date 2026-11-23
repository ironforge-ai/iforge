"""The data-asserting, single-tenant PITR rehearsal (plan 06-16, D-16, OPS-03, OPS-04).

D-16: a rehearsal restores ONE installation's database to a chosen instant into a scratch
environment and passes only when the marker row written before that instant is present and the
marker row written after it is absent. A blank Postiz answers HTTP 200 and ``/`` answers 307 with
its backend dead, so reachability is never evidence; rows are.

OPS-04: the runner reads the repository with a short-lived key minted for the rehearsal alone --
``listFiles`` and ``readFiles`` only, the installation's prefix, valid for at most an hour --
distinct from the runtime's write key and from the operator's delete-capable key, and revoked the
moment the result is recorded.

Nothing here reaches a live API: B2 is the fake from the 06-15 suite, extended with the key
expiry B2 answers for ``validDurationInSeconds``.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import re
import stat
import subprocess
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import ControlPlaneAuditEvent
from app.models.companion_installation import CompanionInstallationSecret
from app.services.vault_crypto import encrypt
from tests.test_companion_backup_repo import (
    B2_ACCOUNT_ID,
    BUCKET_ID,
    BUCKET_NAME,
    S3_ENDPOINT,
    S3_REGION,
    FakeB2,
    _wire_b2,
)
from tests.test_companion_install_job import _reload
from tests.test_companion_projection import _agent_with_runtime, _installation
from tests.test_vault_key_rotation import _ADMIN_AUTH, _admin_client

REHEARSAL_ACTION = "companion.pitr_rehearsal"
READ_ONLY_CAPABILITIES = ["listFiles", "readFiles"]
TARGET_FORMAT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}\+00:00$")
CIPHER_PASS = "rehearsal-cipher-pass-SECRET-" + "c" * 35

# Three markers from three scheduled backups: each written just before its backup, each paired
# with its backup's completion. The newest restorable pair is (day 2, day 3).
MARKER_DAY_1 = {
    "id": 1,
    "token": "1" * 32,
    "written_at": "2026-09-19T03:00:00.111111+00:00",
    "backup_completed_at": "2026-09-19T03:04:10.500000+00:00",
}
MARKER_DAY_2 = {
    "id": 2,
    "token": "2" * 32,
    "written_at": "2026-09-20T03:00:00.222222+00:00",
    "backup_completed_at": "2026-09-20T03:03:30.000001+00:00",
}
MARKER_DAY_3 = {
    "id": 3,
    "token": "3" * 32,
    "written_at": "2026-09-21T03:00:00.333333+00:00",
    "backup_completed_at": "2026-09-21T03:02:40+00:00",
}


# ---------------------------------------------------------------------------
# The fake B2, answering the expiry a validDurationInSeconds key carries.
# ---------------------------------------------------------------------------


class ExpiringB2(FakeB2):
    """06-15's fake B2, plus ``expirationTimestamp`` for a key minted with a validity."""

    def handle(self, request: httpx.Request) -> httpx.Response:
        response = super().handle(request)
        name = str(request.url).rsplit("/", 1)[-1]
        if name != "b2_create_key" or response.status_code != 200:
            return response
        body = response.json()
        requested = json.loads(request.content)
        valid = requested.get("validDurationInSeconds")
        if valid is None:
            return response
        expires = int(time.time() * 1000) + int(valid) * 1000
        self.keys[body["applicationKeyId"]]["expirationTimestamp"] = expires
        return httpx.Response(200, json={**body, "expirationTimestamp": expires})


# ---------------------------------------------------------------------------
# Wiring.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def admin_client(db_session) -> AsyncIterator[httpx.AsyncClient]:
    async def _override_get_session():
        yield db_session

    async with _admin_client(_override_get_session) as client:
        yield client


def _bundle_path(installation_id: uuid.UUID | str) -> str:
    return f"/v1/admin/companions/installations/{installation_id}/rehearsals"


def _result_path(rehearsal_id: uuid.UUID | str) -> str:
    return f"/v1/admin/companions/rehearsals/{rehearsal_id}/result"


def _backup_handles(tenant_id: uuid.UUID, installation_id: uuid.UUID) -> dict[str, Any]:
    return {
        "key_id": "005runtimewritekey0000001",
        "stanza": "postiz-abcdefghijkl",
        "repo_path": f"/{tenant_id}/{installation_id}",
        "bucket_id": BUCKET_ID,
        "bucket": BUCKET_NAME,
        "s3_endpoint": S3_ENDPOINT,
        "s3_region": S3_REGION,
    }


async def _backed_up_installation(
    db_session: AsyncSession,
    seed_user,
    *,
    markers: list[dict[str, Any]] | None = None,
) -> uuid.UUID:
    """A complete installation whose backup step ran and whose backups reported ``markers``."""
    environment_id = await _agent_with_runtime(db_session, seed_user)
    installation_id = await _installation(db_session, seed_user, environment_id, status="complete")
    row = await _reload(db_session, installation_id)
    row.external_resources = {
        "steps": [
            {
                "name": "backup",
                "operation": "install",
                "resources": _backup_handles(row.user_id, installation_id),
                "completed_at": "2026-09-18T12:00:00+00:00",
            }
        ]
    }
    row.backup_state = {
        "last_backup_ok": True,
        "markers": [dict(marker) for marker in (markers if markers is not None else [])],
    }
    encrypted, nonce = encrypt(CIPHER_PASS)
    db_session.add(
        CompanionInstallationSecret(
            installation_id=installation_id,
            secret_name="backup-cipher-pass",
            encrypted_value=encrypted,
            nonce=nonce,
        )
    )
    await db_session.commit()
    return installation_id


async def _rehearsal_events(db_session: AsyncSession) -> list[ControlPlaneAuditEvent]:
    return list(
        (
            await db_session.execute(
                select(ControlPlaneAuditEvent)
                .where(ControlPlaneAuditEvent.action == REHEARSAL_ACTION)
                .execution_options(populate_existing=True)
            )
        ).scalars()
    )


def _parse(moment: str) -> datetime:
    return datetime.fromisoformat(moment)


# ---------------------------------------------------------------------------
# Task 1 (tracer): a rehearsal is planned, run and recorded.
# ---------------------------------------------------------------------------


async def test_rehearsal_is_planned_run_and_recorded(
    admin_client, db_session, seed_user, monkeypatch
) -> None:
    b2 = _wire_b2(monkeypatch, ExpiringB2())
    installation_id = await _backed_up_installation(
        db_session, seed_user, markers=[MARKER_DAY_1, MARKER_DAY_2, MARKER_DAY_3]
    )
    row = await _reload(db_session, installation_id)
    tenant_id = row.user_id

    response = await admin_client.post(_bundle_path(installation_id), headers=_ADMIN_AUTH)
    assert response.status_code == 201, response.text
    assert "no-store" in response.headers.get("cache-control", "")
    bundle = response.json()

    # The newest restorable pair: after day 2's backup completed, before day 3's marker.
    target = bundle["target_time"]
    assert TARGET_FORMAT.match(target), target
    assert (
        _parse(MARKER_DAY_2["backup_completed_at"])
        < _parse(target)
        < _parse(MARKER_DAY_3["written_at"])
    )
    assert bundle["expect_present_token"] == MARKER_DAY_2["token"]
    assert bundle["expect_absent_token"] == MARKER_DAY_3["token"]
    assert bundle["installation_id"] == str(installation_id)
    assert bundle["stanza"] == "postiz-abcdefghijkl"
    assert bundle["repo_path"] == f"/{tenant_id}/{installation_id}"
    assert bundle["bucket"] == BUCKET_NAME
    assert bundle["s3_endpoint"] == S3_ENDPOINT
    assert bundle["s3_region"] == S3_REGION
    assert bundle["cipher_pass"] == CIPHER_PASS

    # One key, minted for this rehearsal alone: read-only, the installation's prefix, one hour.
    (create,) = b2.named("b2_create_key")
    assert create == {
        "accountId": B2_ACCOUNT_ID,
        "keyName": f"pgbr-rh-{bundle['rehearsal_id']}",
        "capabilities": READ_ONLY_CAPABILITIES,
        "bucketIds": [BUCKET_ID],
        "namePrefix": f"{tenant_id}/{installation_id}/",
        "validDurationInSeconds": 3600,
    }
    (read_key,) = b2.keys.values()
    assert bundle["read_key_id"] == read_key["applicationKeyId"]
    assert bundle["read_key"] == b2.issued_application_keys[0]

    # The runner restores to exactly that instant, never pushing WAL back, and decides on rows.
    from scripts.companion_pitr_rehearsal import rehearsal_passed, restore_command

    command = restore_command(bundle)
    assert "--type=time" in command
    # The exact instant, in the spelling pgBackRest's set selection parses: a space for the T,
    # the same microseconds and the same explicit +00:00.
    assert f"--target={target.replace('T', ' ')}" in command
    assert "--target-action=promote" in command
    assert "--archive-mode=off" in command
    restored_tokens = {MARKER_DAY_1["token"], MARKER_DAY_2["token"]}
    present_found = bundle["expect_present_token"] in restored_tokens
    absent_found = bundle["expect_absent_token"] in restored_tokens
    assert rehearsal_passed(present_found, absent_found) is True

    recorded = await admin_client.post(
        _result_path(bundle["rehearsal_id"]),
        headers=_ADMIN_AUTH,
        json={
            "passed": True,
            "present_found": present_found,
            "absent_found": absent_found,
            "schema_survived": True,
            "restored_to": target,
            "detail": "restored and asserted on rows",
        },
    )
    assert recorded.status_code == 200, recorded.text
    assert recorded.json()["outcome"] == "passed"
    assert recorded.json()["key_revoked"] is True

    # One audited event naming the installation, the target and the outcome.
    (event,) = await _rehearsal_events(db_session)
    assert event.actor_type == "admin"
    assert event.source == "api.admin"
    assert event.resource_id == str(installation_id)
    assert event.target_user_id == tenant_id
    assert event.details["installation_id"] == str(installation_id)
    assert event.details["target_time"] == target
    assert event.details["outcome"] == "passed"

    # The read key is revoked: one delete, for exactly that key, and nothing else deleted.
    assert b2.named("b2_delete_key") == [{"applicationKeyId": read_key["applicationKeyId"]}]
    assert b2.keys == {}

    # The pending rehearsal kept only the tokens' hashes, and now carries its outcome.
    row = await _reload(db_session, installation_id)
    (entry,) = row.backup_state["rehearsals"]
    assert entry["id"] == bundle["rehearsal_id"]
    assert entry["outcome"] == "passed"
    assert entry["present_token_sha256"] == hashlib.sha256(b"2" * 32).hexdigest()
    assert entry["absent_token_sha256"] == hashlib.sha256(b"3" * 32).hexdigest()
    assert MARKER_DAY_2["token"] not in repr(entry)
    assert MARKER_DAY_3["token"] not in repr(entry)
    assert _parse(entry["target_time"]) == _parse(target)
    assert datetime.now(UTC) > _parse(entry["created_at"])


# ---------------------------------------------------------------------------
# Task 2: refusals, key hygiene and what counts as a pass.
# ---------------------------------------------------------------------------

NEEDS_TWO_MARKERS = "companion_rehearsal_needs_two_markers"


class WideningB2(ExpiringB2):
    """B2 answering a rehearsal key WIDER than asked: it can also write."""

    def handle(self, request: httpx.Request) -> httpx.Response:
        response = super().handle(request)
        if str(request.url).rsplit("/", 1)[-1] != "b2_create_key":
            return response
        body = response.json()
        widened = [*body["capabilities"], "writeFiles"]
        self.keys[body["applicationKeyId"]]["capabilities"] = widened
        return httpx.Response(200, json={**body, "capabilities": widened})


async def _plan(admin_client: httpx.AsyncClient, installation_id: uuid.UUID) -> dict[str, Any]:
    response = await admin_client.post(_bundle_path(installation_id), headers=_ADMIN_AUTH)
    assert response.status_code == 201, response.text
    return response.json()


def _result_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "passed": True,
        "present_found": True,
        "absent_found": False,
        "schema_survived": True,
        "restored_to": None,
        "detail": None,
    }
    body.update(overrides)
    return body


async def _post_result(
    admin_client: httpx.AsyncClient, rehearsal_id: str, **overrides: Any
) -> httpx.Response:
    return await admin_client.post(
        _result_path(rehearsal_id), headers=_ADMIN_AUTH, json=_result_body(**overrides)
    )


def _without_completion(marker: dict[str, Any]) -> dict[str, Any]:
    return {name: value for name, value in marker.items() if name != "backup_completed_at"}


@pytest.mark.parametrize(
    "markers",
    [
        [],
        [MARKER_DAY_1],
        # The only pair's earlier marker has no completed backup: nothing covers the instant.
        [_without_completion(MARKER_DAY_1), MARKER_DAY_2],
        # The backup finished after the next marker was written: no instant lies between.
        [
            {**MARKER_DAY_1, "backup_completed_at": "2026-09-19T05:00:00+00:00"},
            {**MARKER_DAY_2, "written_at": "2026-09-19T04:00:00+00:00"},
        ],
    ],
    ids=["none", "one", "no-completed-backup", "no-instant-between"],
)
async def test_rehearsal_needs_two_markers_with_a_completed_backup(
    admin_client, db_session, seed_user, monkeypatch, markers
) -> None:
    b2 = _wire_b2(monkeypatch, ExpiringB2())
    installation_id = await _backed_up_installation(db_session, seed_user, markers=markers)

    response = await admin_client.post(_bundle_path(installation_id), headers=_ADMIN_AUTH)

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == NEEDS_TWO_MARKERS
    # Refused before any credential exists.
    assert b2.calls == []
    row = await _reload(db_session, installation_id)
    assert "rehearsals" not in row.backup_state

    # An unknown installation is 404, not 409.
    unknown = await admin_client.post(_bundle_path(uuid.uuid4()), headers=_ADMIN_AUTH)
    assert unknown.status_code == 404, unknown.text


async def test_rehearsal_key_is_read_only_prefixed_and_short_lived(
    admin_client, db_session, seed_user, monkeypatch
) -> None:
    from app.services.companion_backup_repo import rehearsal_key_request

    tenant_id, rehearsal_id = uuid.uuid4(), uuid.uuid4()
    request = rehearsal_key_request(
        account_id=B2_ACCOUNT_ID,
        bucket_id=BUCKET_ID,
        tenant_id=tenant_id,
        installation_id="ab",
        rehearsal_id=rehearsal_id,
    )
    assert request["capabilities"] == READ_ONLY_CAPABILITIES
    assert request["namePrefix"] == f"{tenant_id}/ab/"
    # The trailing slash: installation "ab" can never read installation "abc".
    assert not f"{tenant_id}/abc/data".startswith(str(request["namePrefix"]))
    assert request["validDurationInSeconds"] == 3600
    for too_long in (0, 3601, 86400):
        with pytest.raises(ValueError):
            rehearsal_key_request(
                account_id=B2_ACCOUNT_ID,
                bucket_id=BUCKET_ID,
                tenant_id=tenant_id,
                installation_id="ab",
                rehearsal_id=rehearsal_id,
                valid_seconds=too_long,
            )

    # Through the route: the key B2 holds can only list and read, and lives at most an hour.
    b2 = _wire_b2(monkeypatch, ExpiringB2())
    installation_id = await _backed_up_installation(
        db_session, seed_user, markers=[MARKER_DAY_1, MARKER_DAY_2]
    )
    before = datetime.now(UTC)
    bundle = await _plan(admin_client, installation_id)
    (key,) = b2.keys.values()
    assert key["capabilities"] == READ_ONLY_CAPABILITIES
    assert key["bucketIds"] == [BUCKET_ID]
    assert key["namePrefix"].endswith(f"/{installation_id}/")
    expires = _parse(bundle["expires_at"])
    assert expires - before <= timedelta(seconds=3600 + 5)
    assert bundle["read_key_id"] == key["applicationKeyId"]


@pytest.mark.parametrize(
    "b2_factory",
    [WideningB2, FakeB2],
    ids=["answered-wider", "answered-without-expiry"],
)
async def test_rehearsal_key_answered_wider_or_unbounded_is_revoked_and_refused(
    admin_client, db_session, seed_user, monkeypatch, b2_factory
) -> None:
    b2 = _wire_b2(monkeypatch, b2_factory())
    installation_id = await _backed_up_installation(
        db_session, seed_user, markers=[MARKER_DAY_1, MARKER_DAY_2]
    )

    response = await admin_client.post(_bundle_path(installation_id), headers=_ADMIN_AUTH)

    assert response.status_code == 502, response.text
    assert response.json()["detail"]["code"] == "companion_rehearsal_backup_provider_failed"
    assert "cipher" not in response.text and CIPHER_PASS not in response.text
    assert b2.keys == {}, "the over-wide key must be revoked"
    row = await _reload(db_session, installation_id)
    assert "rehearsals" not in row.backup_state


def test_target_precision_is_microseconds_with_explicit_utc_offset() -> None:
    from app.services.companion_rehearsal import (
        choose_rehearsal_target,
        format_rehearsal_target,
    )

    plus_two = timezone(timedelta(hours=2))
    assert (
        format_rehearsal_target(datetime(2026, 9, 20, 3, 3, 30, 1, tzinfo=UTC))
        == "2026-09-20T03:03:30.000001+00:00"
    )
    assert (
        format_rehearsal_target(datetime(2026, 9, 20, 3, 0, tzinfo=UTC))
        == "2026-09-20T03:00:00.000000+00:00"
    )
    assert (
        format_rehearsal_target(datetime(2026, 9, 20, 5, 0, 0, 250, tzinfo=plus_two))
        == "2026-09-20T03:00:00.000250+00:00"
    )
    assert (
        format_rehearsal_target(datetime(2026, 9, 20, 3, 0, 0, 7))
        == "2026-09-20T03:00:00.000007+00:00"
    )

    # Stored times in another offset and without a fractional part still yield a UTC,
    # microsecond, strictly-between target.
    target = choose_rehearsal_target(
        [
            {
                "id": 7,
                "token": "7" * 32,
                "written_at": "2026-09-20T05:00:00+02:00",
                "backup_completed_at": "2026-09-20T05:00:01+02:00",
            },
            {"id": 8, "token": "8" * 32, "written_at": "2026-09-20T03:00:02+00:00"},
        ]
    )
    assert target is not None
    assert TARGET_FORMAT.match(target.target_text), target.target_text
    assert target.target_text == "2026-09-20T03:00:01.500000+00:00"

    def pair(gap_microseconds: int) -> list[dict[str, Any]]:
        low = datetime(2026, 9, 20, 3, 0, tzinfo=UTC)
        return [
            {
                "id": 1,
                "token": "a" * 32,
                "written_at": "2026-09-20T02:59:00+00:00",
                "backup_completed_at": low.isoformat(),
            },
            {
                "id": 2,
                "token": "b" * 32,
                "written_at": (low + timedelta(microseconds=gap_microseconds)).isoformat(),
            },
        ]

    # One microsecond apart: no instant lies strictly between at microsecond precision.
    assert choose_rehearsal_target(pair(1)) is None
    tight = choose_rehearsal_target(pair(2))
    assert tight is not None
    assert tight.target_text == "2026-09-20T03:00:00.000001+00:00"

    # The runner hands pgBackRest the same instant, microseconds and offset intact, and refuses
    # any other shape rather than let a server time zone or a truncation decide the instant.
    from scripts.companion_pitr_rehearsal import pgbackrest_target

    assert pgbackrest_target(tight.target_text) == "2026-09-20 03:00:00.000001+00:00"
    for malformed in (
        "2026-09-20T03:00:00+00:00",
        "2026-09-20T03:00:00.000001",
        "2026-09-20T03:00:00.000001Z",
        "2026-09-20T05:00:00.000001+02:00",
        "2026-09-20 03:00:00.000001+00:00",
    ):
        with pytest.raises(ValueError):
            pgbackrest_target(malformed)


@pytest.mark.parametrize(
    ("present_found", "absent_found"),
    [(True, False), (True, True), (False, False), (False, True)],
    ids=["present-not-absent", "both", "neither", "absent-only"],
)
async def test_pass_requires_present_and_absent_together(
    admin_client, db_session, seed_user, monkeypatch, present_found, absent_found
) -> None:
    from scripts.companion_pitr_rehearsal import RehearsalRun, rehearsal_passed

    should_pass = present_found and not absent_found
    # The pure decision takes rows and nothing else: no status code, no reachability.
    assert list(inspect.signature(rehearsal_passed).parameters) == [
        "present_found",
        "absent_found",
    ]
    assert rehearsal_passed(present_found, absent_found) is should_pass
    run = RehearsalRun(
        present_found=present_found,
        absent_found=absent_found,
        schema_survived=True,
        restored_to=None,
        tokens_seen=2,
    )
    assert run.passed is should_pass

    _wire_b2(monkeypatch, ExpiringB2())
    installation_id = await _backed_up_installation(
        db_session, seed_user, markers=[MARKER_DAY_1, MARKER_DAY_2]
    )
    bundle = await _plan(admin_client, installation_id)
    # Even a runner that CLAIMS a pass is recorded as failed when the rows say otherwise.
    recorded = await _post_result(
        admin_client,
        bundle["rehearsal_id"],
        passed=True,
        present_found=present_found,
        absent_found=absent_found,
    )
    assert recorded.status_code == 200, recorded.text
    assert recorded.json()["outcome"] == ("passed" if should_pass else "failed")

    # A reachability report is not even accepted as input.
    probe = await admin_client.post(
        _result_path(uuid.uuid4()),
        headers=_ADMIN_AUTH,
        json={**_result_body(), "http_status": 200},
    )
    assert probe.status_code == 422, probe.text


async def test_second_result_for_a_rehearsal_is_409(
    admin_client, db_session, seed_user, monkeypatch
) -> None:
    b2 = _wire_b2(monkeypatch, ExpiringB2())
    installation_id = await _backed_up_installation(
        db_session, seed_user, markers=[MARKER_DAY_1, MARKER_DAY_2]
    )
    bundle = await _plan(admin_client, installation_id)

    first = await _post_result(admin_client, bundle["rehearsal_id"])
    assert first.status_code == 200, first.text
    second = await _post_result(admin_client, bundle["rehearsal_id"], present_found=False)
    assert second.status_code == 409, second.text
    assert second.json()["detail"]["code"] == "companion_rehearsal_already_recorded"

    # One event, one revocation, and the first outcome stands.
    assert len(await _rehearsal_events(db_session)) == 1
    assert len(b2.named("b2_delete_key")) == 1
    row = await _reload(db_session, installation_id)
    (entry,) = row.backup_state["rehearsals"]
    assert entry["outcome"] == "passed"

    # A result for a rehearsal that was never planned is refused (T-06-16-04).
    unplanned = await _post_result(admin_client, str(uuid.uuid4()))
    assert unplanned.status_code == 404, unplanned.text
    assert unplanned.json()["detail"]["code"] == "companion_rehearsal_not_found"


class _FakeContainerCli:
    """Stands in for the container CLI the runner drives; records every argv it was given."""

    def __init__(self, tokens: set[str], *, schema_rows: str = "1") -> None:
        self.tokens = tokens
        self.schema_rows = schema_rows
        self.argvs: list[list[str]] = []
        self.env_files: dict[str, tuple[int, str]] = {}

    def __call__(self, argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        from scripts import companion_pitr_rehearsal as runner

        self.argvs.append(list(argv))
        if "--env-file" in argv:
            path = Path(argv[argv.index("--env-file") + 1])
            self.env_files[str(path)] = (stat.S_IMODE(path.stat().st_mode), path.read_text())
        stdout = ""
        if argv[1] == "inspect":
            stdout = "true\n"
        elif argv[1] == "exec":
            query = argv[-1]
            if query == "select pg_is_in_recovery()":
                stdout = "f\n"
            elif query == runner.SCHEMA_QUERY:
                stdout = f"{self.schema_rows}\n"
            elif query == runner.MARKER_QUERY:
                stdout = "".join(f"{token}\n" for token in sorted(self.tokens))
            elif query == runner.REPLAY_QUERY:
                stdout = "2026-09-20T15:01:45.166667+00:00\n"
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")


def _runner_bundle() -> dict[str, Any]:
    return {
        "rehearsal_id": str(uuid.uuid4()),
        "installation_id": str(uuid.uuid4()),
        "stanza": "postiz-abcdefghijkl",
        "repo_path": "/tenant/installation",
        "s3_endpoint": S3_ENDPOINT,
        "s3_region": S3_REGION,
        "bucket": BUCKET_NAME,
        "read_key_id": "005readonlykey",
        "read_key": "K005-read-only-SECRET-" + "r" * 20,
        "cipher_pass": CIPHER_PASS,
        "target_time": "2026-09-20T15:01:45.166667+00:00",
        "expect_present_token": MARKER_DAY_2["token"],
        "expect_absent_token": MARKER_DAY_3["token"],
        "expect_survivor_token": MARKER_DAY_1["token"],
        "expires_at": "2026-09-20T16:00:00+00:00",
    }


async def test_bundle_and_audit_never_leak_key_or_cipher_into_logs_or_audit(
    admin_client, db_session, seed_user, monkeypatch, caplog
) -> None:
    from app.schemas.admin import AdminCompanionRehearsalBundle
    from scripts import companion_pitr_rehearsal as runner

    caplog.set_level(logging.DEBUG)
    b2 = _wire_b2(monkeypatch, ExpiringB2())
    installation_id = await _backed_up_installation(
        db_session, seed_user, markers=[MARKER_DAY_1, MARKER_DAY_2, MARKER_DAY_3]
    )
    bundle = await _plan(admin_client, installation_id)
    read_key = bundle["read_key"]
    assert read_key == b2.issued_application_keys[0]
    # A result whose detail echoes the cipher pass is stored redacted.
    recorded = await _post_result(
        admin_client, bundle["rehearsal_id"], detail=f"pgbackrest said {CIPHER_PASS}"
    )
    assert recorded.status_code == 200, recorded.text

    secrets_ = (read_key, CIPHER_PASS)
    tokens = (MARKER_DAY_2["token"], MARKER_DAY_3["token"])
    for secret in secrets_:
        assert secret not in caplog.text
        assert secret not in recorded.text
    (event,) = await _rehearsal_events(db_session)
    audited = json.dumps(event.details)
    for value in (*secrets_, *tokens):
        assert value not in audited
    row = await _reload(db_session, installation_id)
    stored = json.dumps(row.backup_state["rehearsals"])
    for value in (*secrets_, *tokens):
        assert value not in stored
    assert "[redacted]" in stored

    # No repr of the bundle renders a secret.
    model = AdminCompanionRehearsalBundle.model_validate(bundle)
    for secret in secrets_:
        assert secret not in repr(model)
        assert secret not in str(model)

    # The runner hands secrets to pgBackRest only through a 0600 env file it removes; no argv
    # it builds carries one, and a detail it reports is redacted.
    fake = _FakeContainerCli({MARKER_DAY_1["token"], MARKER_DAY_2["token"]})
    monkeypatch.setattr(runner.subprocess, "run", fake)
    run = runner.run_rehearsal(_runner_bundle(), image="house-image@sha256:" + "0" * 64)
    assert run.passed is True
    for argv in fake.argvs:
        for secret in (_runner_bundle()["read_key"][:20], CIPHER_PASS):
            assert all(secret not in part for part in argv), argv
    assert fake.env_files, "the restore must read an env file"
    for path, (mode, text) in fake.env_files.items():
        assert mode == 0o600
        assert CIPHER_PASS in text
        assert not Path(path).exists(), "the env file must be removed"
    restore_argv = next(
        argv for argv in fake.argvs if "--entrypoint" in argv and "pgbackrest" in argv
    )
    assert restore_argv[-7:] == runner.restore_command(_runner_bundle())[1:]
    assert runner.redact(f"x {CIPHER_PASS} y", _runner_bundle()) == "x [redacted] y"


@pytest.mark.parametrize(
    ("schema_survived", "outcome", "reason"),
    [
        (True, "passed", None),
        (False, "failed", "assumption_a4_schema_lost"),
        (None, "failed", "assumption_a4_unchecked"),
    ],
    ids=["survived", "lost", "unchecked"],
)
async def test_first_rehearsal_records_schema_survival(
    admin_client, db_session, seed_user, monkeypatch, schema_survived, outcome, reason
) -> None:
    from scripts import companion_pitr_rehearsal as runner

    _wire_b2(monkeypatch, ExpiringB2())
    installation_id = await _backed_up_installation(
        db_session, seed_user, markers=[MARKER_DAY_1, MARKER_DAY_2, MARKER_DAY_3]
    )
    bundle = await _plan(admin_client, installation_id)
    # The oldest marker the control plane knows: if the restore still holds it, the schema
    # survived every Postiz start (and its prisma db push) between that marker and the target.
    assert bundle.get("expect_survivor_token") == MARKER_DAY_1["token"]

    recorded = await _post_result(
        admin_client, bundle["rehearsal_id"], passed=True, schema_survived=schema_survived
    )

    assert recorded.status_code == 200, recorded.text
    assert recorded.json()["outcome"] == outcome
    assert recorded.json()["reason"] == reason
    (event,) = await _rehearsal_events(db_session)
    assert event.details["outcome"] == outcome
    assert event.details["schema_survived"] is schema_survived
    row = await _reload(db_session, installation_id)
    if outcome == "passed":
        assert row.backup_state.get("rehearsal_schema_survived_at")
    else:
        assert "research assumption A4" in recorded.json()["reason_text"]
        assert "rehearsal_schema_survived_at" not in row.backup_state

    # The runner reports the schema lost when the survivor is gone, even with the pair right.
    fake = _FakeContainerCli({MARKER_DAY_2["token"]})
    monkeypatch.setattr(runner.subprocess, "run", fake)
    lost = runner.run_rehearsal(_runner_bundle(), image="house-image@sha256:" + "0" * 64)
    assert lost.schema_survived is False
    assert lost.passed is False
