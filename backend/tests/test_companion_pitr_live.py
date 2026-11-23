"""Opt-in live lane: backup, point-in-time restore, row assertion -- end to end (plan 06-16).

Evidence about the MECHANISM, not about a provisioned host or Backblaze B2. On a Docker daemon it
runs the house PostgreSQL image (by the digest in ``config/postgres-image.txt``) as a tenant's
database archiving WAL into a versioned S3-compatible store (MinIO, by digest), and reproduces
what plans 06-15 and 06-16 do on real infrastructure:

- the stanza is created; marker 1 is written into ``iforge_rehearsal.marker`` exactly as the
  runtime writes it; a full backup is taken; marker 2 is written; a trailing write and
  ``pgbackrest check`` put WAL past both markers into the repository;
- the control plane's own ``choose_rehearsal_target`` picks an instant between the backup's
  completion and marker 2, and the operator runner's ``run_rehearsal`` -- the same code, its
  ``restore_command`` with ``--type=time --target-action=promote --archive-mode=off`` -- restores
  into a scratch container with a fresh volume, using a store user that can only LIST and READ;
- restored to that instant, the database holds marker 1 and not marker 2, so the rehearsal
  passes; restored to an instant after both markers it holds marker 2 too, so the same pass
  condition is false -- the assertion can fail;
- no restore writes a single object or version into the tenant's repository.

It says nothing about B2's key restrictions (the unit suite covers the key request and the
refusal of a wider answer), Object Lock, or a real companion host; that is the plan's
human-check, recorded separately.

The store serves TLS with a certificate generated per run, because pgBackRest speaks S3 over
HTTPS only; the lane alone turns certificate verification off (``extra_env``) -- an operator run
never does.

Run::

    IFORGE_PITR_LIVE=1 uv run pytest -q -s tests/test_companion_pitr_live.py -rs

Skipped unless ``IFORGE_PITR_LIVE=1`` AND ``docker info`` answers. Every container, volume and
network name carries a per-run random token and is removed in a ``finally``.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest


def _docker_answers() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(["docker", "info"], capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


# Short-circuit: `docker info` is only ever run when the lane was asked for.
pytestmark = pytest.mark.skipif(
    not (os.environ.get("IFORGE_PITR_LIVE") == "1" and _docker_answers()),
    reason="opt-in live lane: set IFORGE_PITR_LIVE=1 on a host whose Docker daemon answers",
)

REPO_ROOT = Path(__file__).resolve().parents[2]
POSTGRES_IMAGE = (REPO_ROOT / "config" / "postgres-image.txt").read_text().strip()
# MinIO RELEASE.2025-04-22T22-12-26Z and its client RELEASE.2025-04-16T18-13-26Z, index digests
# resolved with `docker buildx imagetools inspect quay.io/minio/{minio,mc}:<release>`.
MINIO_IMAGE = (
    "quay.io/minio/minio@sha256:a1ea29fa28355559ef137d71fc570e508a214ec84ff8083e39bc5428980b015e"
)
MC_IMAGE = (
    "quay.io/minio/mc@sha256:aead63c77f9db9107f1696fb08ecb0faeda23729cde94b0f663edf4fe09728e3"
)
LANE_IMAGES = (POSTGRES_IMAGE, MINIO_IMAGE, MC_IMAGE)

BUCKET = "pitr"
REGION = "us-east-1"
DB_USER = "postiz"
DB_NAME = "postiz"
READY_BUDGET_SECONDS = 180
POLL_SECONDS = 1.0
# pgBackRest refuses S3 without TLS; the lane's certificate is self-signed.
LANE_ONLY_ENV = {"PGBACKREST_REPO1_STORAGE_VERIFY_TLS": "n"}

# Exactly the runtime's marker SQL (packages/cli/src/runtime/container-companion.ts).
_MARKER_SQL = (
    "CREATE SCHEMA IF NOT EXISTS iforge_rehearsal;\n"
    "CREATE TABLE IF NOT EXISTS iforge_rehearsal.marker (id bigserial PRIMARY KEY, token text "
    "NOT NULL, written_at timestamptz NOT NULL DEFAULT now());\n"
    "\\set token '{token}'\n"
    "INSERT INTO iforge_rehearsal.marker (token) VALUES (:'token') RETURNING id, "
    "to_char(written_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"');\n"
)


def _docker(
    *args: str, stdin: str | None = None, timeout: float = 300, check: bool = True
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["docker", *args],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"docker {args[0]} failed ({result.returncode}): {result.stderr.strip()[-2000:]}"
        )
    return result


def _write_env(path: Path, env: dict[str, str]) -> Path:
    path.touch(mode=0o600)
    path.write_text("".join(f"{name}={value}\n" for name, value in env.items()))
    return path


def _self_signed(directory: Path, host: str) -> None:
    """``public.crt`` and ``private.key`` for ``host``, MinIO's certificate file names."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    (directory / "public.crt").write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    private = directory / "private.key"
    private.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    # MinIO runs as root in its container; the file sits in a 0700 per-run directory.
    private.chmod(0o644)


@dataclass(frozen=True)
class Marker:
    id: int
    token: str
    written_at: datetime


@dataclass
class Lane:
    token: str
    network: str
    store_host: str
    database: str
    stanza: str
    repo_path: str
    read_key_id: str
    read_key: str
    cipher_pass: str
    mc_env: Path
    marker_1: Marker
    backup_completed_at: datetime
    marker_2: Marker
    after_both: datetime
    timings: dict[str, float]

    def bundle(self, target_time: str) -> dict[str, Any]:
        return {
            "rehearsal_id": f"live-{self.token}",
            "installation_id": f"live-{self.token}",
            "stanza": self.stanza,
            "repo_path": self.repo_path,
            "s3_endpoint": self.store_host,
            "s3_region": REGION,
            "bucket": BUCKET,
            "read_key_id": self.read_key_id,
            "read_key": self.read_key,
            "cipher_pass": self.cipher_pass,
            "target_time": target_time,
            "expect_present_token": self.marker_1.token,
            "expect_absent_token": self.marker_2.token,
            "expect_survivor_token": self.marker_1.token,
        }


def _mc(lane_network: str, mc_env: Path, script: str, *, check: bool = True) -> str:
    """Run MinIO client commands against the lane's store (root credentials from an env file)."""
    result = _docker(
        "run",
        "--rm",
        "--network",
        lane_network,
        "--env-file",
        str(mc_env),
        "--entrypoint",
        "/bin/sh",
        MC_IMAGE,
        "-c",
        script,
        check=check,
    )
    return result.stdout


def _repository_versions(lane: Lane) -> list[str]:
    """Every object VERSION in the tenant's repository path, as the store lists them."""
    listing = _mc(
        lane.network,
        lane.mc_env,
        f"mc --insecure ls --recursive --versions --json s3/{BUCKET}{lane.repo_path}/",
    )
    return sorted(
        f"{entry.get('key')}@{entry.get('versionId')}"
        for entry in (json.loads(line) for line in listing.splitlines() if line.strip())
        if entry.get("type") == "file"
    )


def _psql(database: str, script: str) -> list[str]:
    result = _docker(
        "exec",
        "-i",
        database,
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
        stdin=script,
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def _write_marker(database: str) -> Marker:
    token = secrets.token_hex(16)
    (row,) = _psql(database, _MARKER_SQL.replace("{token}", token))
    marker_id, written_at = row.split("|")
    return Marker(
        id=int(marker_id),
        token=token,
        written_at=datetime.fromisoformat(written_at.replace("Z", "+00:00")),
    )


def _pgbackrest(database: str, *args: str) -> None:
    _docker("exec", "--user", "postgres", database, "pgbackrest", *args, timeout=900)


def _wait(what: str, probe) -> None:
    deadline = time.monotonic() + READY_BUDGET_SECONDS
    while time.monotonic() < deadline:
        if probe():
            return
        time.sleep(POLL_SECONDS)
    raise AssertionError(f"{what} was not ready within {READY_BUDGET_SECONDS}s")


@pytest.fixture(scope="module")
def lane() -> Iterator[Lane]:
    for image in LANE_IMAGES:
        assert "@sha256:" in image, image
        _docker("pull", "-q", image, timeout=900)

    token = secrets.token_hex(4)
    network = f"iforge-pitr-{token}"
    store_host = f"store-{token}"
    database = f"iforge-pitr-db-{token}"
    workdir = Path(tempfile.mkdtemp(prefix="iforge-pitr-live-"))
    certs = workdir / "certs"
    certs.mkdir()
    _self_signed(certs, store_host)
    root_user, root_password = f"root{token}", secrets.token_hex(16)
    read_key_id, read_key = f"read{token}", secrets.token_hex(16)
    cipher_pass = secrets.token_urlsafe(48)
    stanza = f"postiz-{token}"
    repo_path = f"/tenant-{token}/installation-{token}"
    store_env = _write_env(
        workdir / "store.env", {"MINIO_ROOT_USER": root_user, "MINIO_ROOT_PASSWORD": root_password}
    )
    mc_env = _write_env(
        workdir / "mc.env", {"MC_HOST_s3": f"https://{root_user}:{root_password}@{store_host}"}
    )
    # The rehearsal's store user can list and read the tenant's objects, and nothing else --
    # the lane's stand-in for the B2 key with listFiles and readFiles only.
    (workdir / "read-only.json").write_text(
        json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": ["s3:GetBucketLocation", "s3:ListBucket"],
                        "Resource": [f"arn:aws:s3:::{BUCKET}"],
                    },
                    {
                        "Effect": "Allow",
                        "Action": ["s3:GetObject"],
                        "Resource": [f"arn:aws:s3:::{BUCKET}{repo_path}/*"],
                    },
                ],
            }
        )
    )
    # The tenant's database writes with the store's root credentials, as the runtime writes with
    # its own write key.
    database_env = _write_env(
        workdir / "database.env",
        {
            "POSTGRES_USER": DB_USER,
            "POSTGRES_PASSWORD": secrets.token_hex(16),
            "POSTGRES_DB": DB_NAME,
            "PGBACKREST_STANZA": stanza,
            "PGBACKREST_PG1_PATH": "/var/lib/postgresql/18/docker",
            "PGBACKREST_PG1_USER": DB_USER,
            "PGBACKREST_REPO1_TYPE": "s3",
            "PGBACKREST_REPO1_PATH": repo_path,
            "PGBACKREST_REPO1_S3_BUCKET": BUCKET,
            "PGBACKREST_REPO1_S3_ENDPOINT": store_host,
            "PGBACKREST_REPO1_S3_REGION": REGION,
            "PGBACKREST_REPO1_S3_URI_STYLE": "path",
            "PGBACKREST_REPO1_S3_KEY": root_user,
            "PGBACKREST_REPO1_S3_KEY_SECRET": root_password,
            "PGBACKREST_REPO1_CIPHER_TYPE": "aes-256-cbc",
            "PGBACKREST_REPO1_CIPHER_PASS": cipher_pass,
            "PGBACKREST_EXPIRE_AUTO": "n",
            "PGBACKREST_COMPRESS_TYPE": "zst",
            **LANE_ONLY_ENV,
        },
    )
    timings: dict[str, float] = {}
    try:
        _docker("network", "create", "--internal", network)
        _docker(
            "run",
            "-d",
            "--name",
            store_host,
            "--network",
            network,
            "--env-file",
            str(store_env),
            "-v",
            f"{certs}:/certs:ro",
            MINIO_IMAGE,
            "server",
            "/data",
            "--address",
            ":443",
            "--certs-dir",
            "/certs",
        )
        _mc(
            network,
            mc_env,
            "i=0; until mc --insecure ls s3 >/dev/null 2>&1; do "
            f'i=$((i+1)); [ "$i" -gt {READY_BUDGET_SECONDS} ] && exit 1; sleep 1; done; '
            f"mc --insecure mb --with-versioning s3/{BUCKET} && "
            f"mc --insecure version info s3/{BUCKET}",
        )
        _docker(
            "run",
            "--rm",
            "--network",
            network,
            "--env-file",
            str(mc_env),
            "-v",
            f"{workdir / 'read-only.json'}:/policy/read-only.json:ro",
            "--entrypoint",
            "/bin/sh",
            MC_IMAGE,
            "-c",
            "mc --insecure admin policy create s3 rehearsal-read /policy/read-only.json && "
            f"mc --insecure admin user add s3 {read_key_id} {read_key} && "
            f"mc --insecure admin policy attach s3 rehearsal-read --user {read_key_id}",
        )

        _docker(
            "run",
            "-d",
            "--name",
            database,
            "--network",
            network,
            "--env-file",
            str(database_env),
            POSTGRES_IMAGE,
            "postgres",
            "-c",
            "archive_mode=on",
            "-c",
            f"archive_command=pgbackrest --stanza={stanza} archive-push %p",
            "-c",
            "archive_timeout=60",
        )
        # The entrypoint's init server listens on no TCP port; the real one does.
        _wait(
            "the tenant database",
            lambda: (
                _docker(
                    "exec", database, "pg_isready", "-h", "127.0.0.1", "-U", DB_USER, check=False
                ).returncode
                == 0
            ),
        )
        _pgbackrest(database, f"--stanza={stanza}", "stanza-create")
        marker_1 = _write_marker(database)
        started = time.monotonic()
        _pgbackrest(database, f"--stanza={stanza}", "--type=full", "backup")
        timings["full_backup_seconds"] = time.monotonic() - started
        backup_completed_at = datetime.now(UTC)
        time.sleep(1.2)
        marker_2 = _write_marker(database)
        time.sleep(1.2)
        after_both = datetime.now(UTC)
        time.sleep(1.2)
        # A commit after every target this lane uses, so recovery always meets its target.
        _psql(database, "CREATE TABLE IF NOT EXISTS lane_trailer (at timestamptz);")
        _psql(database, "INSERT INTO lane_trailer VALUES (now());")
        _pgbackrest(database, f"--stanza={stanza}", "check")
        yield Lane(
            token=token,
            network=network,
            store_host=store_host,
            database=database,
            stanza=stanza,
            repo_path=repo_path,
            read_key_id=read_key_id,
            read_key=read_key,
            cipher_pass=cipher_pass,
            mc_env=mc_env,
            marker_1=marker_1,
            backup_completed_at=backup_completed_at,
            marker_2=marker_2,
            after_both=after_both,
            timings=timings,
        )
    finally:
        _docker("rm", "-f", database, store_host, check=False, timeout=120)
        _docker("network", "rm", network, check=False, timeout=120)
        shutil.rmtree(workdir, ignore_errors=True)


def _restore(lane: Lane, target_time: str):
    from scripts import companion_pitr_rehearsal as runner

    before = _repository_versions(lane)
    started = time.monotonic()
    run = runner.run_rehearsal(
        lane.bundle(target_time),
        image=POSTGRES_IMAGE,
        network=lane.network,
        extra_env=LANE_ONLY_ENV,
        restore_timeout=900,
        start_timeout=300,
    )
    elapsed = time.monotonic() - started
    # T-06-16-01: the scratch restore wrote nothing -- not an object, not a version.
    assert _repository_versions(lane) == before
    return run, elapsed


def test_restore_between_markers_holds_the_earlier_and_not_the_later(lane: Lane) -> None:
    from app.services.companion_rehearsal import choose_rehearsal_target

    target = choose_rehearsal_target(
        [
            {
                "id": lane.marker_1.id,
                "token": lane.marker_1.token,
                "written_at": lane.marker_1.written_at.isoformat(),
                "backup_completed_at": lane.backup_completed_at.isoformat(),
            },
            {
                "id": lane.marker_2.id,
                "token": lane.marker_2.token,
                "written_at": lane.marker_2.written_at.isoformat(),
            },
        ]
    )
    assert target is not None
    assert lane.backup_completed_at < target.target_time < lane.marker_2.written_at

    run, elapsed = _restore(lane, target.target_text)

    print(
        f"\n[pitr-live] between: target={target.target_text} restored_to={run.restored_to} "
        f"present={run.present_found} absent={run.absent_found} "
        f"schema_survived={run.schema_survived} rows={run.tokens_seen} "
        f"restore+start={elapsed:.1f}s full_backup={lane.timings['full_backup_seconds']:.1f}s"
    )
    assert run.present_found is True
    assert run.absent_found is False
    assert run.schema_survived is True
    assert run.passed is True


def test_restore_after_both_markers_fails_the_assertion(lane: Lane) -> None:
    from app.services.companion_rehearsal import format_rehearsal_target

    target_time = format_rehearsal_target(lane.after_both)
    assert lane.marker_2.written_at < lane.after_both

    run, elapsed = _restore(lane, target_time)

    print(
        f"\n[pitr-live] after-both: target={target_time} restored_to={run.restored_to} "
        f"present={run.present_found} absent={run.absent_found} rows={run.tokens_seen} "
        f"restore+start={elapsed:.1f}s"
    )
    # The later marker is back, so the very pass condition the rehearsal uses says no.
    assert run.present_found is True
    assert run.absent_found is True
    assert run.passed is False
