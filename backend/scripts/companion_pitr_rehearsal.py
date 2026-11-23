"""Operator-side PITR rehearsal runner for one companion installation (plan 06-16, D-16, OPS-03).

The runner asks the control plane for a rehearsal bundle, restores the installation's pgBackRest
stanza to the bundle's target instant into a SCRATCH container with a fresh volume, starts
PostgreSQL, reads ``iforge_rehearsal.marker`` and reports what the rows said. The control plane
records the outcome as an audited event and revokes the bundle's read-only key.

What counts as a pass is :func:`rehearsal_passed`: the marker written before the target is present
AND the marker written after it is absent. Nothing else. A reachable application or an HTTP 200
is never evidence -- a blank Postiz answers 200 -- so this runner never probes one. The runner
also reports whether the ``iforge_rehearsal`` schema survived (research assumption A4): the
oldest marker the control plane knows must still be in the restore.

Safety:

* The restore runs with ``--archive-mode=off``, so the scratch server can never push WAL into
  the tenant's repository; the bundle's key can only list and read anyway (T-06-16-01).
* The key and the cipher pass reach pgBackRest only through an env file, mode 0600, in a private
  temporary directory -- never argv, never a log line -- and the file is removed in ``finally``.
* The scratch container and its volume are removed in ``finally`` whatever happened, and a
  result is posted even after an operational error, so the key is revoked at once rather than
  left to its one-hour expiry.

Run it from an operator host with Docker (or podman), the control plane's admin key and outbound
reach to the backup provider::

    IFORGE_ADMIN_API_KEY=... python3 backend/scripts/companion_pitr_rehearsal.py \\
        --api-url https://<control plane> --installation-id <installation uuid>

Exit codes: 0 the rehearsal passed; 1 it ran and the rows failed the assertion (or the schema did
not survive); 2 an operational error (no bundle, a failed restore, a server that never started).

Standard library plus ``httpx`` only: no application import, so it runs from a checkout without
the backend's settings.
"""

from __future__ import annotations

import argparse
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

EXIT_PASSED = 0
EXIT_FAILED = 1
EXIT_ERROR = 2

ADMIN_KEY_ENV = "IFORGE_ADMIN_API_KEY"
# The house PostgreSQL image, pinned by digest (the same pin the runtime's database runs).
POSTGRES_IMAGE_FILE = Path(__file__).resolve().parents[2] / "config" / "postgres-image.txt"
# The house image's pg18 layout: the volume holds the whole tree, PGDATA sits beneath it.
DATA_MOUNT = "/var/lib/postgresql"
PGDATA = "/var/lib/postgresql/18/docker"
POSTGRES_OS_USER = "postgres"
# The companion's database and role (the runtime's POSTIZ_DB_USER / POSTIZ_DB_NAME).
DB_USER = "postiz"
DB_NAME = "postiz"

SCHEMA_QUERY = (
    "select count(*) from information_schema.tables "
    "where table_schema = 'iforge_rehearsal' and table_name = 'marker'"
)
MARKER_QUERY = "select token from iforge_rehearsal.marker"
REPLAY_QUERY = (
    "select coalesce(to_char(pg_last_xact_replay_timestamp() at time zone 'UTC', "
    "'YYYY-MM-DD\"T\"HH24:MI:SS.US\"+00:00\"'), '')"
)

# The control plane's target: UTC, microseconds, the offset spelled out (companion_rehearsal).
TARGET_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}\+00:00$")

RESTORE_TIMEOUT_SECONDS = 3600
START_TIMEOUT_SECONDS = 900
POLL_SECONDS = 2.0
MAX_DETAIL_CHARS = 1000


# ---------------------------------------------------------------------------
# The decision and the command: pure, and imported by the tests.
# ---------------------------------------------------------------------------


def rehearsal_passed(present_found: bool, absent_found: bool) -> bool:
    """Exactly "the earlier marker is present AND the later marker is absent"."""
    return present_found and not absent_found


def pgbackrest_target(target_time: str) -> str:
    """pgBackRest's spelling of the bundle's instant: the same microseconds and ``+00:00``.

    pgBackRest selects the backup set for ``--type=time`` itself, and its parser takes
    ``YYYY-MM-DD HH:MM:SS[.fraction][+HH:MM]`` -- a SPACE where ISO 8601 puts ``T``; given the
    ``T`` it refuses with "automatic backup set selection cannot be performed" (observed by the
    live lane against pgBackRest 2.59). PostgreSQL's ``recovery_target_time`` reads the same
    string. Anything but the control plane's exact format is refused, never guessed at.
    """
    if not TARGET_PATTERN.fullmatch(target_time):
        raise ValueError(f"target {target_time!r} is not YYYY-MM-DDTHH:MM:SS.ffffff+00:00")
    return target_time.replace("T", " ", 1)


def restore_command(bundle: Mapping[str, Any]) -> list[str]:
    """pgBackRest's point-in-time restore of the bundle's stanza to the bundle's instant.

    ``--type=time`` with the exact target, ``--target-action=promote`` so the scratch server
    opens read-write at the target, ``--archive-mode=off`` so it can never archive into the
    tenant's repository, and ``--delta`` so a retried restore into the same volume is safe.
    """
    return [
        "pgbackrest",
        f"--stanza={bundle['stanza']}",
        "--delta",
        "--type=time",
        f"--target={pgbackrest_target(str(bundle['target_time']))}",
        "--target-action=promote",
        "--archive-mode=off",
        "restore",
    ]


def restore_env(bundle: Mapping[str, Any]) -> dict[str, str]:
    """Every pgBackRest setting the restore reads, the secrets included. Goes to an env file only.

    The same repository settings the runtime uses (plan 06-15), with the bundle's read-only key.
    """
    return {
        "PGBACKREST_STANZA": str(bundle["stanza"]),
        "PGBACKREST_PG1_PATH": PGDATA,
        "PGBACKREST_PG1_USER": DB_USER,
        "PGBACKREST_REPO1_TYPE": "s3",
        "PGBACKREST_REPO1_PATH": str(bundle["repo_path"]),
        "PGBACKREST_REPO1_S3_BUCKET": str(bundle["bucket"]),
        "PGBACKREST_REPO1_S3_ENDPOINT": str(bundle["s3_endpoint"]),
        "PGBACKREST_REPO1_S3_REGION": str(bundle["s3_region"]),
        "PGBACKREST_REPO1_S3_URI_STYLE": "path",
        "PGBACKREST_REPO1_S3_KEY": str(bundle["read_key_id"]),
        "PGBACKREST_REPO1_S3_KEY_SECRET": str(bundle["read_key"]),
        "PGBACKREST_REPO1_CIPHER_TYPE": "aes-256-cbc",
        "PGBACKREST_REPO1_CIPHER_PASS": str(bundle["cipher_pass"]),
    }


def env_file_text(env: Mapping[str, str]) -> str:
    """``NAME=value`` lines for ``--env-file``; a value that could break a line is refused."""
    for name, value in env.items():
        if any(character in value for character in ("\n", "\r", "\x00")):
            raise ValueError(f"{name} holds a character an env file cannot carry")
    return "".join(f"{name}={value}\n" for name, value in env.items())


def redact(text: str, bundle: Mapping[str, Any]) -> str:
    """``text`` with the bundle's key and cipher pass removed, bounded for the result's detail."""
    for name in ("read_key", "cipher_pass"):
        secret = str(bundle.get(name) or "")
        if secret:
            text = text.replace(secret, "[redacted]")
    return text[-MAX_DETAIL_CHARS:]


# ---------------------------------------------------------------------------
# The scratch restore.
# ---------------------------------------------------------------------------


class RehearsalOperationalError(RuntimeError):
    """The rehearsal could not be carried out; its message is already redacted."""


@dataclass(frozen=True, slots=True)
class RehearsalRun:
    present_found: bool
    absent_found: bool
    schema_survived: bool | None
    restored_to: str | None
    tokens_seen: int

    @property
    def passed(self) -> bool:
        return self.schema_survived is not False and rehearsal_passed(
            self.present_found, self.absent_found
        )


def _container_cli(
    runtime: str, args: Sequence[str], *, timeout: float, check: bool = True
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            [runtime, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise RehearsalOperationalError(f"{runtime} {args[0]} timed out after {timeout:.0f}s")
    if check and result.returncode != 0:
        raise RehearsalOperationalError(
            f"{runtime} {args[0]} failed ({result.returncode}): {result.stderr.strip()[-600:]}"
        )
    return result


def _psql(runtime: str, container: str, query: str) -> subprocess.CompletedProcess[str]:
    return _container_cli(
        runtime,
        [
            "exec",
            "--user",
            POSTGRES_OS_USER,
            container,
            "psql",
            "-X",
            "-q",
            "-t",
            "-A",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            DB_USER,
            "-d",
            DB_NAME,
            "-c",
            query,
        ],
        timeout=60,
        check=False,
    )


def _wait_for_promotion(runtime: str, container: str, *, timeout: float) -> None:
    """Until the scratch server answers and has left recovery (promoted at the target)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = _container_cli(
            runtime, ["inspect", "-f", "{{.State.Running}}", container], timeout=30, check=False
        )
        if state.stdout.strip() != "true":
            logs = _container_cli(runtime, ["logs", "--tail", "40", container], timeout=30)
            raise RehearsalOperationalError(
                "the scratch server stopped before promotion: "
                + (logs.stdout + logs.stderr).strip()[-600:]
            )
        answer = _psql(runtime, container, "select pg_is_in_recovery()")
        if answer.returncode == 0 and answer.stdout.strip() == "f":
            return
        time.sleep(POLL_SECONDS)
    raise RehearsalOperationalError(f"the scratch server did not promote within {timeout:.0f}s")


def run_rehearsal(
    bundle: Mapping[str, Any],
    *,
    image: str,
    runtime: str = "docker",
    network: str | None = None,
    extra_env: Mapping[str, str] | None = None,
    restore_timeout: float = RESTORE_TIMEOUT_SECONDS,
    start_timeout: float = START_TIMEOUT_SECONDS,
) -> RehearsalRun:
    """Restore into a scratch container and read the markers. Always cleans up.

    ``network`` and ``extra_env`` exist for the opt-in live lane (an S3-compatible store in a
    private network with a self-signed certificate); an operator run passes neither.
    """
    name = f"iforge-rehearsal-{secrets.token_hex(6)}"
    volume = f"{name}-data"
    private_dir = Path(tempfile.mkdtemp(prefix="iforge-rehearsal-"))
    env_path = private_dir / "pgbackrest.env"
    network_args = ["--network", network] if network else []
    try:
        env_path.touch(mode=0o600)
        env_path.write_text(env_file_text({**restore_env(bundle), **(extra_env or {})}))
        _container_cli(runtime, ["volume", "create", volume], timeout=60)
        # PGDATA must exist, owned by postgres, before pgBackRest restores into it.
        _container_cli(
            runtime,
            [
                "run",
                "--rm",
                "--user",
                POSTGRES_OS_USER,
                "-v",
                f"{volume}:{DATA_MOUNT}",
                "--entrypoint",
                "mkdir",
                image,
                "-p",
                "-m",
                "0700",
                PGDATA,
            ],
            timeout=300,
        )
        restored = _container_cli(
            runtime,
            [
                "run",
                "--rm",
                "--user",
                POSTGRES_OS_USER,
                *network_args,
                "--env-file",
                str(env_path),
                "-v",
                f"{volume}:{DATA_MOUNT}",
                "--entrypoint",
                "pgbackrest",
                image,
                *restore_command(bundle)[1:],
            ],
            timeout=restore_timeout,
            check=False,
        )
        if restored.returncode != 0:
            raise RehearsalOperationalError(
                "pgbackrest restore failed: "
                + redact((restored.stdout + restored.stderr).strip()[-600:], bundle)
            )
        # The server's restore_command runs archive-get, which reads the same repository settings.
        _container_cli(
            runtime,
            [
                "run",
                "-d",
                "--name",
                name,
                *network_args,
                "--env-file",
                str(env_path),
                "-v",
                f"{volume}:{DATA_MOUNT}",
                image,
            ],
            timeout=300,
        )
        _wait_for_promotion(runtime, name, timeout=start_timeout)
        schema = _psql(runtime, name, SCHEMA_QUERY)
        if schema.returncode != 0:
            raise RehearsalOperationalError(
                "the schema query failed: " + redact(schema.stderr.strip(), bundle)
            )
        schema_present = schema.stdout.strip() == "1"
        tokens: set[str] = set()
        if schema_present:
            rows = _psql(runtime, name, MARKER_QUERY)
            if rows.returncode != 0:
                raise RehearsalOperationalError(
                    "the marker query failed: " + redact(rows.stderr.strip(), bundle)
                )
            tokens = {line.strip() for line in rows.stdout.splitlines() if line.strip()}
        # Research A4: the schema survived every Postiz start (and its prisma db push) since the
        # oldest known marker only if that marker's row is still here. A dropped-and-recreated
        # schema would hold the newer markers and not this one.
        survivor = str(bundle.get("expect_survivor_token") or "")
        schema_survived = schema_present and (not survivor or survivor in tokens)
        replay = _psql(runtime, name, REPLAY_QUERY)
        restored_to = (replay.stdout.strip() or None) if replay.returncode == 0 else None
        return RehearsalRun(
            present_found=str(bundle["expect_present_token"]) in tokens,
            absent_found=str(bundle["expect_absent_token"]) in tokens,
            schema_survived=schema_survived,
            restored_to=restored_to,
            tokens_seen=len(tokens),
        )
    finally:
        _container_cli(runtime, ["rm", "-f", name], timeout=120, check=False)
        _container_cli(runtime, ["volume", "rm", "-f", volume], timeout=120, check=False)
        shutil.rmtree(private_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# The control plane.
# ---------------------------------------------------------------------------


def _pinned_image() -> str:
    return POSTGRES_IMAGE_FILE.read_text().strip()


def _post_result(
    client: httpx.Client, bundle: Mapping[str, Any], body: Mapping[str, Any]
) -> httpx.Response:
    return client.post(
        f"/v1/admin/companions/rehearsals/{bundle['rehearsal_id']}/result", json=body
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--api-url", required=True, help="the control plane's base URL")
    parser.add_argument("--installation-id", required=True)
    parser.add_argument("--image", default=None, help="default: config/postgres-image.txt")
    parser.add_argument("--runtime", default="docker", choices=("docker", "podman"))
    args = parser.parse_args(argv)

    admin_key = os.environ.get(ADMIN_KEY_ENV, "").strip()
    if not admin_key:
        print(f"{ADMIN_KEY_ENV} is unset; the runner never takes the key on argv", file=sys.stderr)
        return EXIT_ERROR
    image = args.image or _pinned_image()
    with httpx.Client(
        base_url=args.api_url.rstrip("/"),
        headers={"X-Admin-Key": admin_key, "User-Agent": "iforge-pitr-rehearsal/1.0"},
        timeout=60.0,
    ) as client:
        try:
            planned = client.post(
                f"/v1/admin/companions/installations/{args.installation_id}/rehearsals"
            )
        except httpx.HTTPError as exc:
            print(f"requesting a rehearsal failed: {exc.__class__.__name__}", file=sys.stderr)
            return EXIT_ERROR
        if planned.status_code != 201:
            print(
                f"the control plane refused the rehearsal: HTTP {planned.status_code} "
                f"{planned.text[:500]}",
                file=sys.stderr,
            )
            return EXIT_ERROR
        bundle: dict[str, Any] = planned.json()
        started = time.monotonic()
        try:
            run = run_rehearsal(bundle, image=image, runtime=args.runtime)
        except RehearsalOperationalError as exc:
            detail = redact(str(exc), bundle)
            print(
                f"rehearsal {bundle['rehearsal_id']}: operational error: {detail}", file=sys.stderr
            )
            # Report anyway: the control plane records the failure and revokes the key now.
            _post_result(
                client,
                bundle,
                {
                    "passed": False,
                    "present_found": False,
                    "absent_found": False,
                    "schema_survived": None,
                    "restored_to": None,
                    "detail": f"operational error: {detail}"[:MAX_DETAIL_CHARS],
                },
            )
            return EXIT_ERROR
        elapsed = time.monotonic() - started
        detail = (
            f"target {bundle['target_time']}; restored to {run.restored_to or 'unknown'}; "
            f"{run.tokens_seen} marker rows; present={run.present_found} "
            f"absent={run.absent_found}; schema_survived={run.schema_survived}; {elapsed:.0f}s"
        )
        recorded = _post_result(
            client,
            bundle,
            {
                "passed": run.passed,
                "present_found": run.present_found,
                "absent_found": run.absent_found,
                "schema_survived": run.schema_survived,
                "restored_to": run.restored_to,
                "detail": detail,
            },
        )
        print(f"rehearsal {bundle['rehearsal_id']}: {detail}")
        if recorded.status_code != 200:
            print(
                f"recording the result failed: HTTP {recorded.status_code} {recorded.text[:500]}",
                file=sys.stderr,
            )
            return EXIT_ERROR
        outcome = recorded.json().get("outcome")
        print(f"recorded outcome: {outcome}; key revoked: {recorded.json().get('key_revoked')}")
        return EXIT_PASSED if outcome == "passed" and run.passed else EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
