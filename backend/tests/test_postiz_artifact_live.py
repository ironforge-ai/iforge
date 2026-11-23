"""Opt-in live lane: the pinned Postiz artifact boots offline, gates registration, takes the key.

Evidence about the ARTIFACT, not about a provisioned host. This lane runs the exact image digests
the companion catalog pins on a Docker daemon, and reproduces the research session's probes
(06-RESEARCH.md Pattern 5, Pitfalls 1 and 4) against them. It says nothing about rootful podman,
Quadlet, the CK-B user namespace or systemd encrypted credentials on a provisioned tenant host;
that is plan 06-12's host human-check, recorded separately.

What it proves (APP-01, APP-03):

- Offline start: with no network at all and ``npm_config_dlx_cache_max_age=5256000``, the
  container's start-time ``pnpm dlx prisma db push`` reaches Prisma from the baked cache instead
  of resolving against the npm registry (research Pitfall 1).
- Full boot on a private network with NO egress (``docker network create --internal``), beside
  the house PostgreSQL image, Redis, and a Temporal dev server whose Postiz namespace was created
  clean with ``temporal operator namespace create`` (research F4 / Pitfall 4):
  is-connected without a key answers 401; the first registration answers 200; the registered
  admin credential signs in with 200 ``{"login": true}`` and a wrong password does not (the
  runtime's ownership proof, plan 06-38); a second registration answers
  400 "Registration is disabled"; the key seeded through psql STANDARD INPUT, exactly as the
  runtime seeds it, makes is-connected answer 200 ``{"connected": true}``; the admin-user and
  organization-count queries the runtime runs return exactly one row.

Every image is referenced by digest, never by tag: the catalog's pins for Postiz, PostgreSQL and
Redis, and one pinned index digest for the Temporal CLI dev server, which is operator
infrastructure under CK-A (a shared Temporal) and therefore has no catalog pin.

Run::

    IFORGE_POSTIZ_LIVE=1 uv run pytest -q -s tests/test_postiz_artifact_live.py -rs

Skipped unless ``IFORGE_POSTIZ_LIVE=1`` AND ``docker info`` answers. Every container and network
name carries a per-run random token and is removed in a ``finally``.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from app.core.companion_catalog import catalog_entry


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
    not (os.environ.get("IFORGE_POSTIZ_LIVE") == "1" and _docker_answers()),
    reason="opt-in live lane: set IFORGE_POSTIZ_LIVE=1 on a host whose Docker daemon answers",
)

_POSTIZ = catalog_entry("postiz")
APP_IMAGE = f"{_POSTIZ.app.image}@sha256:{_POSTIZ.app.sha256}"
POSTGRES_IMAGE = f"{_POSTIZ.postgres.image}@sha256:{_POSTIZ.postgres.sha256}"
REDIS_IMAGE = f"{_POSTIZ.redis.image}@sha256:{_POSTIZ.redis.sha256}"
# temporalio/temporal 1.9.0 (the Temporal CLI, whose `server start-dev` is the dev server), index
# digest resolved with `docker buildx imagetools inspect docker.io/temporalio/temporal:1.9.0`.
TEMPORAL_IMAGE = (
    "docker.io/temporalio/temporal@sha256:"
    "ad4c82c97bd12b417d1ea942610dbcd511afb250c4d5ed26c694009533df447e"
)
LANE_IMAGES = (APP_IMAGE, POSTGRES_IMAGE, REDIS_IMAGE, TEMPORAL_IMAGE)
DIGEST_REFERENCE = re.compile(r"^[a-z0-9.\-/]+@sha256:[a-f0-9]{64}$")

POSTIZ_CONTAINER_PORT = 5000
NPM_DLX_CACHE_MAX_AGE = "5256000"
READINESS_BUDGET_SECONDS = 300
POLL_SECONDS = 5
OFFLINE_BUDGET_SECONDS = 240

# Answers one loopback request from INSIDE the Postiz container: a network created with
# --internal publishes no port, so the probe runs where the app's own nginx listens. The request,
# including any password or key, arrives on standard input, never on the command line.
_PROBE = (
    "let d='';process.stdin.on('data',c=>d+=c).on('end',async()=>{"
    "const r=JSON.parse(d);"
    "try{const res=await fetch('http://127.0.0.1:"
    + str(POSTIZ_CONTAINER_PORT)
    + "'+r.path,{method:r.method,headers:r.headers||{},body:r.body,redirect:'manual'});"
    "console.log(JSON.stringify({status:res.status,body:await res.text()}));"
    "}catch(e){console.log(JSON.stringify({status:0,body:String(e)}));}});"
)


def _docker(
    *args: str, stdin: str | None = None, timeout: float = 120, check: bool = True
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


def _psql_quoted(value: str) -> str:
    # psql meta-command quoting, exactly as the runtime quotes a `\set` value.
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def _env_file(directory: Path, name: str, values: dict[str, str]) -> Path:
    path = directory / name
    path.write_text("".join(f"{key}={value}\n" for key, value in sorted(values.items())))
    path.chmod(0o600)
    return path


class _Lane:
    """One run's containers, network and timings; everything is removed in `close`."""

    def __init__(self) -> None:
        self.token = secrets.token_hex(4)
        self.prefix = f"iforge-postiz-live-{self.token}"
        self.network = f"{self.prefix}-net"
        self.containers: list[str] = []
        self.networks: list[str] = []
        self.timings: dict[str, float] = {}
        self.workdir = Path(tempfile.mkdtemp(prefix=f"{self.prefix}-"))
        self.db_password = secrets.token_urlsafe(24)
        self.jwt_secret = secrets.token_urlsafe(48)
        self.api_key = secrets.token_urlsafe(32)
        self.admin_email = f"owner-{self.token}@example.test"
        self.admin_password = secrets.token_urlsafe(24)
        self.namespace = f"postiz-{self.token}"

    def name(self, role: str) -> str:
        return f"{self.prefix}-{role}"

    def run(self, role: str, *args: str) -> str:
        name = self.name(role)
        self.containers.append(name)
        _docker("run", "-d", "--name", name, *args)
        return name

    def close(self) -> None:
        for name in reversed(self.containers):
            _docker("rm", "-f", "-v", name, check=False, timeout=60)
        for network in self.networks:
            _docker("network", "rm", network, check=False, timeout=60)
        shutil.rmtree(self.workdir, ignore_errors=True)
        if self.timings:
            print(f"postiz-live timings (s): {json.dumps(self.timings, sort_keys=True)}")

    def probe(self, method: str, path: str, *, headers=None, body=None) -> tuple[int, str]:
        request = json.dumps(
            {"method": method, "path": path, "headers": headers or {}, "body": body}
        )
        result = _docker(
            "exec", "-i", self.name("app"), "node", "-e", _PROBE, stdin=request, check=False
        )
        try:
            answer = json.loads(result.stdout.strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError):
            return 0, result.stderr.strip()[-500:]
        return int(answer["status"]), str(answer["body"])

    def psql(self, script: str) -> list[str]:
        # The runtime's exact invocation: fixed argv, the script (and any secret) on stdin.
        result = _docker(
            "exec",
            "-i",
            self.name("db"),
            "psql",
            "-X",
            "-q",
            "-t",
            "-A",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            "postiz",
            "-d",
            "postiz",
            stdin=script,
        )
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _wait_for(what: str, budget: float, check: Callable[[], bool]) -> float:
    started = time.monotonic()
    while True:
        if check():
            return round(time.monotonic() - started, 1)
        if time.monotonic() - started >= budget:
            raise AssertionError(f"{what} did not happen within {budget:.0f} s")
        time.sleep(POLL_SECONDS)


def _pull_all() -> dict[str, float]:
    pulled: dict[str, float] = {}
    for reference in LANE_IMAGES:
        started = time.monotonic()
        _docker("pull", "--quiet", reference, timeout=3600)
        pulled[reference.split("@")[0]] = round(time.monotonic() - started, 1)
    return pulled


@pytest.fixture(scope="module")
def lane() -> Iterator[_Lane]:
    run = _Lane()
    try:
        for key, seconds in _pull_all().items():
            run.timings[f"pull {key}"] = seconds
        yield run
    finally:
        run.close()


def test_every_lane_image_is_a_digest_reference() -> None:
    for reference in LANE_IMAGES:
        assert DIGEST_REFERENCE.match(reference), reference
    # The catalog's own pins, not copies of them.
    assert APP_IMAGE.endswith(_POSTIZ.app.sha256)
    assert POSTGRES_IMAGE.endswith(_POSTIZ.postgres.sha256)
    assert REDIS_IMAGE.endswith(_POSTIZ.redis.sha256)


def test_offline_start_reaches_prisma_without_the_npm_registry(lane: _Lane) -> None:
    env = _env_file(
        lane.workdir,
        "offline.env",
        {
            # Unreachable on purpose: the proof is the step BEFORE the database connection.
            "DATABASE_URL": f"postgresql://postiz:{lane.db_password}@offline-db:5432/postiz",
            "REDIS_URL": "redis://offline-redis:6379",
            "JWT_SECRET": lane.jwt_secret,
            "MAIN_URL": "http://localhost:5000",
            "FRONTEND_URL": "http://localhost:5000",
            "NEXT_PUBLIC_BACKEND_URL": "http://localhost:5000/api",
            "BACKEND_INTERNAL_URL": "http://localhost:3000",
            "IS_GENERAL": "true",
            "npm_config_dlx_cache_max_age": NPM_DLX_CACHE_MAX_AGE,
        },
    )
    started = time.monotonic()
    name = lane.run("offline", "--network", "none", "--env-file", str(env), APP_IMAGE)
    reached = ("Prisma schema loaded from", 'Datasource "db"', "P1001")
    registry = ("ERR_PNPM_META_FETCH_FAIL", "registry.npmjs.org")

    def prisma_ran() -> bool:
        logs = _docker("logs", name, check=False)
        text = logs.stdout + logs.stderr
        assert not any(marker in text for marker in registry), text[-3000:]
        return any(marker in text for marker in reached)

    lane.timings["offline start to prisma"] = _wait_for(
        "the offline Prisma step", OFFLINE_BUDGET_SECONDS, prisma_ran
    )
    lane.timings["offline total"] = round(time.monotonic() - started, 1)
    _docker("rm", "-f", name, check=False)


def test_full_boot_gates_registration_and_accepts_the_seeded_key(lane: _Lane) -> None:
    _docker("network", "create", "--internal", lane.network)
    lane.networks.append(lane.network)
    net = ("--network", lane.network)

    db_env = _env_file(
        lane.workdir,
        "db.env",
        {"POSTGRES_USER": "postiz", "POSTGRES_PASSWORD": lane.db_password, "POSTGRES_DB": "postiz"},
    )
    lane.run("db", *net, "--network-alias", "postiz-db", "--env-file", str(db_env), POSTGRES_IMAGE)
    lane.run("redis", *net, "--network-alias", "postiz-redis", REDIS_IMAGE)
    temporal = lane.run(
        "temporal",
        *net,
        "--network-alias",
        "postiz-temporal",
        TEMPORAL_IMAGE,
        "server",
        "start-dev",
        "--ip",
        "0.0.0.0",
        "--headless",
    )

    lane.timings["database ready"] = _wait_for(
        "PostgreSQL",
        120,
        lambda: (
            _docker(
                "exec", lane.name("db"), "pg_isready", "-U", "postiz", "-d", "postiz", check=False
            ).returncode
            == 0
        ),
    )
    temporal_cli = ("exec", temporal, "temporal", "operator", "--address", "127.0.0.1:7233")
    lane.timings["temporal ready"] = _wait_for(
        "the Temporal dev server",
        120,
        lambda: _docker(*temporal_cli, "namespace", "list", check=False).returncode == 0,
    )
    # F4: a namespace created clean, never an auto-setup default, so Postiz can register its own
    # search attributes (PostgreSQL visibility allows three Text attributes per namespace).
    _docker(*temporal_cli, "namespace", "create", "--namespace", lane.namespace)
    attributes = _docker(
        *temporal_cli, "search-attribute", "list", "--namespace", lane.namespace, "-o", "json"
    )
    listed = json.loads(attributes.stdout or "{}")
    custom = listed.get("customAttributes", {}) if isinstance(listed, dict) else {}
    text_attributes = [key for key, kind in custom.items() if "TEXT" in str(kind).upper()]
    assert len(text_attributes) <= 1, custom

    app_env = _env_file(
        lane.workdir,
        "app.env",
        {
            "DATABASE_URL": f"postgresql://postiz:{lane.db_password}@postiz-db:5432/postiz",
            "REDIS_URL": "redis://postiz-redis:6379",
            "JWT_SECRET": lane.jwt_secret,
            "MAIN_URL": "http://localhost:5000",
            "FRONTEND_URL": "http://localhost:5000",
            "NEXT_PUBLIC_BACKEND_URL": "http://localhost:5000/api",
            "BACKEND_INTERNAL_URL": "http://localhost:3000",
            "IS_GENERAL": "true",
            "DISABLE_REGISTRATION": "true",
            "RUN_CRON": "true",
            "API_LIMIT": "90",
            "STORAGE_PROVIDER": "local",
            "UPLOAD_DIRECTORY": "/uploads",
            "NEXT_PUBLIC_UPLOAD_DIRECTORY": "/uploads",
            "TEMPORAL_ADDRESS": "postiz-temporal:7233",
            "TEMPORAL_NAMESPACE": lane.namespace,
            "npm_config_dlx_cache_max_age": NPM_DLX_CACHE_MAX_AGE,
        },
    )
    started = time.monotonic()
    lane.run("app", *net, "--env-file", str(app_env), APP_IMAGE)

    # Readiness is is-connected answering 401 without a key -- never `/`.
    def backend_up() -> bool:
        status, _ = lane.probe("GET", "/api/public/v1/is-connected")
        return status == 401

    try:
        lane.timings["app start to is-connected 401"] = _wait_for(
            "is-connected 401", READINESS_BUDGET_SECONDS, backend_up
        )
    except AssertionError:
        logs = _docker("logs", "--tail", "80", lane.name("app"), check=False)
        raise AssertionError(f"Postiz never came up:\n{logs.stdout}\n{logs.stderr}") from None

    register = {
        "email": lane.admin_email,
        "password": lane.admin_password,
        "provider": "LOCAL",
        "company": "iforge live lane",
    }
    headers = {"content-type": "application/json"}
    assert lane.psql('SELECT count(*) FROM "Organization";\n') == ["0"]
    status, body = lane.probe(
        "POST", "/api/auth/register", headers=headers, body=json.dumps(register)
    )
    assert status == 200, body
    assert json.loads(body) == {"register": True}

    # The ownership proof the runtime asks before it adopts an existing organization (plan 06-38,
    # CR-02): the platform's admin credential signs in over loopback with 200 `{"login": true}`,
    # and the same email with a wrong password does not answer 200. The probe returns only the
    # status and the body, so the session cookie Postiz sets never leaves the container.
    login = {"email": lane.admin_email, "password": lane.admin_password, "provider": "LOCAL"}
    status, body = lane.probe("POST", "/api/auth/login", headers=headers, body=json.dumps(login))
    assert status == 200, body
    assert json.loads(body) == {"login": True}
    wrong = {**login, "password": f"wrong-{lane.token}"}
    status, _ = lane.probe("POST", "/api/auth/login", headers=headers, body=json.dumps(wrong))
    assert status != 200, status

    stranger = {**register, "email": f"stranger-{lane.token}@example.test"}
    status, body = lane.probe(
        "POST", "/api/auth/register", headers=headers, body=json.dumps(stranger)
    )
    assert status == 400, body
    assert "Registration is disabled" in body

    seeded = lane.psql(
        f"\\set key {_psql_quoted(lane.api_key)}\n"
        'UPDATE "Organization" SET "apiKey" = :\'key\' '
        'WHERE (SELECT count(*) FROM "Organization") = 1 RETURNING "id";\n'
    )
    assert len(seeded) == 1, seeded
    admin = lane.psql(
        f"\\set email {_psql_quoted(lane.admin_email)}\n"
        'SELECT "id" FROM "User" WHERE lower("email") = lower(:\'email\');\n'
    )
    assert len(admin) == 1 and 0 < len(admin[0]) <= 64, admin
    assert lane.psql('SELECT count(*) FROM "Organization";\n') == ["1"]

    status, body = lane.probe(
        "GET", "/api/public/v1/is-connected", headers={"authorization": lane.api_key}
    )
    assert status == 200, body
    assert json.loads(body) == {"connected": True}
    # A wrong key is still refused, so 200 above is the key, not an open door.
    status, _ = lane.probe("GET", "/api/public/v1/is-connected", headers={"authorization": "nope"})
    assert status == 401
    lane.timings["app start to connected"] = round(time.monotonic() - started, 1)
