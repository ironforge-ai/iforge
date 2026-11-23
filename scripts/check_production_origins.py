#!/usr/bin/env python3
"""Fail closed while any origin baked into a shipped build does not serve over TLS.

``packages/shared/src/destinations.ts`` is written out verbatim and is compiled into every
native binary and every web bundle. A user's bearer token is sent to the cloud API origin it
names, and a support address it names is rendered at the billing dunning banner and the
deactivated-account page. Neither obligation is checked by anything that runs: the destinations
contract test asserts the record agrees with the build flags and the deploy configuration, which
is a question about *agreement between files* and not about whether the hosts answer.

This guard asks the other question. For every origin in that record it performs one HTTPS request
and records one of three verdicts:

``SERVED``
    Any HTTP status came back. The TLS handshake completed and an origin answered. A 404 is
    served, and so is a redirect that settles on a final answer (the ``www`` alias sending the
    apex's 200): the question is whether the host answers over TLS, not what it answers.
``BAD_REDIRECT``
    The host answered, but only with redirects that never settle: a loop, a hop limit reached, a
    hop off https, or a redirect with no ``Location``. A browser shows "too many redirects", so
    nothing behind the name is reachable. Measured 2026-10-26: four origins answered ``301`` to
    the exact URL requested. No service was deployed behind them; whatever the edge reached only
    redirected to https. The gate used to call that ``SERVED``.
``NO_TLS``
    The edge returned a status that exists specifically to report that the *origin's* handshake
    failed. The name resolves and the edge is reachable; the thing behind it is not.
``NO_ORIGIN``
    The edge answered with one of its own "origin unreachable" statuses (520-524, 530): the
    name routes to Cloudflare but nothing behind it is running, listening or connected (a
    stopped service, a closed port, a tunnel with no connector). The edge answered; no origin
    did.
``UNRESOLVED``
    No address record, or a connection that never reached a TLS handshake at all.

Separately it asks the apex for an MX record and records ``MAIL`` or ``NO_MAIL``, because a
support address rendered at an account-termination surface whose mail is silently discarded
leaves a user worse off than one told there is no support channel.

**The verdict logic is a pure function over a recorded observation.** ``origin_verdict`` and
``mail_verdict`` take a dataclass carrying a status code, an error name and an MX answer; the
network calls are thin adapters that produce one. That separation is what lets
``backend/tests/test_production_origins_gate.py`` prove the failing *and* the passing direction
today, while every host is dead, without mocking a socket and without waiting for a DNS change.

**No hostname is written here.** The probe list is parsed from the destinations record, and the
parse refuses below ``_MINIMUM_PARSED_KEYS`` rather than probing a short list, so the gate cannot
be silently narrowed by deleting an entry. ``grep -c`` for the apex in this file returns ``0``.

Host comparison is byte-exact ASCII. A punycode or case-varied spelling of a declared host is a
*different* host and is reported as a mismatch rather than folded into a match: the whole point of
the check is that the name the binary dials is the name this fork claims to control.

Structure follows the sibling ``scripts/check_*.py`` guards: standard library only (so it runs
under bare ``python3``, not only under ``uv``), paths resolved from a module-level ``ROOT``, a
domain error subclass internally, ``SystemExit`` with a readable message from ``main()``, and one
summary line in the house style. It takes flags where the siblings take none, because the tests
and the runbook both need the machine-readable form.

Deliberately NOT wired into ``scripts/test.sh``. The gate is red until the zone changes, and no
code change in this repository can make it green -- DNS records, certificates and MX records live
in a Cloudflare zone and on a VPS. Turning the canonical suite red for a condition no commit can
fix would train people to ignore it. Its enforcement point is the publish path instead:
``packages/cli/scripts/check-native-release.mjs`` and ``prepublishOnly`` both spawn it.

AGENTS.md OSS boundary: every name this guard handles is a public hostname the shipped binary
already contains, and it records no private address, no VPS identifier and no vendor account
detail.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import socket
import ssl
import subprocess
import sys
from dataclasses import dataclass
from http.client import HTTPSConnection
from pathlib import Path
from urllib.parse import urljoin, urlsplit

ROOT = Path(__file__).resolve().parents[1]

DESTINATIONS_FILE = ROOT / "packages" / "shared" / "src" / "destinations.ts"

# The origin keys, in the order the destinations record declares them. The order is the row order
# and is load-bearing: two runs over the same observations must produce byte-identical output.
#
# The apex and its ``www`` alias are two entries and stay two entries. They are distinct members
# of the record -- one is the marketing homepage, the other exists as a CORS member -- and a gate
# that deduplicated them would report one row for two names, so a `www` record that was never
# created would read as covered by the apex's.
ORIGIN_KEYS: tuple[str, ...] = (
    "webOrigin",
    "wwwOrigin",
    "dashboardOrigin",
    "cloudApiOrigin",
    "deployApiOrigin",
    "assetsOrigin",
)

APEX_KEY = "apexDomain"
SUPPORT_EMAIL_KEY = "supportEmail"

# Every origin key plus the apex and the support address. The parse refuses below this, so a
# deleted entry fails the gate instead of shrinking its coverage.
_MINIMUM_PARSED_KEYS = len(ORIGIN_KEYS) + 2

# The only scheme a probed origin may declare. A value without it is a parse error rather than a
# host with a scheme inferred for it: inferring one would let the gate probe a destination the
# record did not actually name.
_REQUIRED_SCHEME = "https://"

# Statuses the edge returns specifically to report that the *origin's* TLS handshake failed.
# Cloudflare 525 is "SSL handshake failed" and 526 is "invalid SSL certificate"; both mean the
# edge reached the origin and the origin did not complete a handshake the edge would accept.
# Distinguished from SERVED because the request never reached the thing that holds the data --
# and a build whose credential destination answers only this is not shippable.
TLS_HANDSHAKE_FAILURE_STATUSES: frozenset[int] = frozenset({525, 526})

# Statuses the edge generates itself when it cannot get any answer from the origin: 520 unknown
# origin error, 521 web server down, 522 connection timed out, 523 origin unreachable, 524
# origin timeout, 530 (carrying a 1xxx error, e.g. 1033: a tunnel with no connector). A host
# with nothing deployed behind a proxied name answers one of these, so counting them as SERVED
# would turn the gate green on an empty host. A 500, 502 or 503 is not here: those can come from
# a real origin that answered, and the gate asks whether a host answers, not what it answers.
ORIGIN_UNREACHABLE_STATUSES: frozenset[int] = frozenset({520, 521, 522, 523, 524, 530})

# Statuses that carry a ``Location`` to follow. 304 is not here: it is a cache answer, not a
# redirect, and an unconditional GET never receives one.
REDIRECT_STATUSES: frozenset[int] = frozenset({301, 302, 303, 307, 308})

# Hops followed before a chain is declared unsettled. Browsers stop at roughly twenty; a declared
# origin that needs more than a handful is broken whether or not it would eventually land.
MAX_REDIRECTS = 5

SERVED = "SERVED"
NO_TLS = "NO_TLS"
NO_ORIGIN = "NO_ORIGIN"
UNRESOLVED = "UNRESOLVED"
BAD_REDIRECT = "BAD_REDIRECT"
MAIL = "MAIL"
NO_MAIL = "NO_MAIL"

_PROBE_TIMEOUT_SECONDS = 15.0
_MX_TIMEOUT_SECONDS = 15.0


class ProductionOriginsError(RuntimeError):
    """A destinations record that cannot be read, or an observation that does not match it."""


@dataclass(frozen=True)
class OriginObservation:
    """One recorded HTTPS probe. Carries no live connection, so a fixture is as real as a run."""

    host: str
    status: int | None = None
    error: str | None = None
    # Every URL the probe was redirected to, in order. ``status`` is the LAST response's status.
    redirects: tuple[str, ...] = ()
    # Why the chain did not settle, or None when it did (or never redirected).
    redirect_problem: str | None = None


@dataclass(frozen=True)
class MailObservation:
    """One recorded MX lookup for the apex.

    ``source`` names which resolver produced the answer. It is carried because a NEGATIVE MX
    answer is not a fact about the zone -- it can be a cached NXRRSET from before the record
    existed -- and a report that cannot say who it asked cannot be argued with.
    """

    host: str
    mx_records: tuple[str, ...] = ()
    source: str = "recorded fixture"


@dataclass(frozen=True)
class Row:
    host: str
    verdict: str
    detail: str


@dataclass(frozen=True)
class Report:
    origins: tuple[Row, ...]
    mail: Row

    @property
    def counts(self) -> dict[str, int]:
        return {
            SERVED: sum(1 for row in self.origins if row.verdict == SERVED),
            NO_TLS: sum(1 for row in self.origins if row.verdict == NO_TLS),
            NO_ORIGIN: sum(1 for row in self.origins if row.verdict == NO_ORIGIN),
            UNRESOLVED: sum(1 for row in self.origins if row.verdict == UNRESOLVED),
            BAD_REDIRECT: sum(1 for row in self.origins if row.verdict == BAD_REDIRECT),
        }

    @property
    def ok(self) -> bool:
        """Zero origins is NOT a pass.

        A gate that exits 0 because it iterated an empty list reports a state it never
        established. That is the defect this whole guard exists to remove, so the empty case is
        spelled out here rather than falling out of ``all(...)``, which is vacuously true.
        """
        if not self.origins:
            return False
        return all(row.verdict == SERVED for row in self.origins) and self.mail.verdict == MAIL


# --------------------------------------------------------------------------------------------
# Pure verdict logic. Everything below this line up to the adapters is total over its input and
# opens no socket.
# --------------------------------------------------------------------------------------------


def host_matches(observed: str, declared: str) -> bool:
    """Byte-exact ASCII equality.

    No IDNA folding, no case normalisation, no trailing-dot tolerance. A punycode spelling of a
    declared host is a different sequence of bytes and therefore a different host: the gate's
    claim is about the exact name a binary dials, and a fold would let a lookalike satisfy it.
    """
    return observed.encode("utf-8") == declared.encode("utf-8")


def origin_verdict(observation: OriginObservation) -> tuple[str, str]:
    """Return ``(verdict, detail)`` for one recorded probe."""
    if observation.status is not None:
        if observation.status in TLS_HANDSHAKE_FAILURE_STATUSES:
            return NO_TLS, f"edge reports origin handshake failure (HTTP {observation.status})"
        if observation.status in ORIGIN_UNREACHABLE_STATUSES:
            return NO_ORIGIN, f"edge reports no origin answering (HTTP {observation.status})"
        if observation.redirect_problem is not None:
            return BAD_REDIRECT, (
                f"HTTP {observation.status}: {observation.redirect_problem}; a browser cannot "
                "reach this origin"
            )
        if observation.redirects:
            return SERVED, (
                f"HTTP {observation.status} after {len(observation.redirects)} redirect(s), "
                f"ending at {observation.redirects[-1]}"
            )
        return SERVED, f"HTTP {observation.status}"
    reason = observation.error or "no response"
    return UNRESOLVED, f"no TLS handshake ({reason})"


def mail_verdict(observation: MailObservation) -> tuple[str, str]:
    """Return ``(verdict, detail)`` for one recorded MX lookup."""
    if observation.mx_records:
        return MAIL, f"{len(observation.mx_records)} MX record(s) via {observation.source}"
    return NO_MAIL, (
        f"no MX record via {observation.source}; mail to the support address is discarded"
    )


def build_report(
    hosts: list[str],
    observations: list[OriginObservation],
    mail_observation: MailObservation,
) -> Report:
    """Pair declared hosts with their observations positionally and byte-exactly.

    A mismatch is an error, not a reordering opportunity: if the thing probed is not byte-exactly
    the thing declared, the report would otherwise attribute a verdict to a host nobody measured.
    """
    if len(hosts) != len(observations):
        raise ProductionOriginsError(
            f"{len(hosts)} declared origins but {len(observations)} observations"
        )
    rows = []
    for declared, observation in zip(hosts, observations, strict=True):
        if not host_matches(observation.host, declared):
            raise ProductionOriginsError(
                f"observation names {observation.host!r} but the declared origin is "
                f"{declared!r}; host comparison is byte-exact and does not fold a punycode or "
                "case-varied form into a match"
            )
        verdict, detail = origin_verdict(observation)
        rows.append(Row(host=declared, verdict=verdict, detail=detail))
    verdict, detail = mail_verdict(mail_observation)
    return Report(origins=tuple(rows), mail=Row(mail_observation.host, verdict, detail))


def summary_line(report: Report) -> str:
    counts = report.counts
    state = "passed" if report.ok else "failed"
    return (
        f"production origins {state}: {len(report.origins)} origins probed, "
        f"{counts[SERVED]} served, {counts[NO_TLS]} no-tls, {counts[NO_ORIGIN]} no-origin, "
        f"{counts[UNRESOLVED]} unresolved, "
        f"{counts[BAD_REDIRECT]} bad-redirect; "
        f"mail {report.mail.verdict}"
    )


def render_rows(report: Report) -> str:
    width = max((len(row.host) for row in (*report.origins, report.mail)), default=1)
    lines = [
        f"origin  {row.host.ljust(width)}  {row.verdict.ljust(len(BAD_REDIRECT))}  {row.detail}"
        for row in report.origins
    ]
    lines.append(
        f"mail    {report.mail.host.ljust(width)}  "
        f"{report.mail.verdict.ljust(len(BAD_REDIRECT))}  {report.mail.detail}"
    )
    return "\n".join(lines)


def render_json(report: Report) -> str:
    """Exactly one JSON object, and nothing else, on stdout.

    ``origins[*].host`` and ``origins[*].verdict`` are consumed by the publish gate, so the key
    names and the shape are a contract rather than an illustration.
    """
    return json.dumps(
        {
            "origins": [
                {"host": row.host, "verdict": row.verdict, "detail": row.detail}
                for row in report.origins
            ],
            "mail": {
                "host": report.mail.host,
                "verdict": report.mail.verdict,
                "detail": report.mail.detail,
            },
            "summary": summary_line(report),
        }
    )


# --------------------------------------------------------------------------------------------
# The destinations parse.
# --------------------------------------------------------------------------------------------


def parse_destinations(source: str) -> dict[str, str]:
    """Read the quoted string values of the record by regex, the way the sibling guards do.

    The record is a frozen TypeScript object literal that nothing in Python can import. Every key
    this guard needs is a plain double-quoted string on its own line.
    """
    found = {}
    for key in (*ORIGIN_KEYS, APEX_KEY, SUPPORT_EMAIL_KEY):
        match = re.search(rf'^\s*{re.escape(key)}:\s*"([^"]+)"', source, re.MULTILINE)
        if match is not None:
            found[key] = match.group(1)
    if len(found) < _MINIMUM_PARSED_KEYS:
        missing = sorted({*ORIGIN_KEYS, APEX_KEY, SUPPORT_EMAIL_KEY} - set(found))
        raise ProductionOriginsError(
            f"parsed {len(found)} of {_MINIMUM_PARSED_KEYS} required keys from "
            f"{DESTINATIONS_FILE.relative_to(ROOT)}; missing {', '.join(missing)}. The probe list "
            "is never hardcoded, so a short parse fails rather than narrowing the gate"
        )
    return found


def host_from_origin(value: str) -> str:
    """Strip the required scheme byte-exactly. No scheme is ever inferred."""
    if not value.startswith(_REQUIRED_SCHEME):
        raise ProductionOriginsError(
            f"origin {value!r} does not begin with {_REQUIRED_SCHEME!r}; the gate does not infer "
            "a scheme for a value the record did not spell"
        )
    host = value[len(_REQUIRED_SCHEME) :].split("/", 1)[0]
    if not host:
        raise ProductionOriginsError(f"origin {value!r} names no host")
    try:
        host.encode("ascii")
    except UnicodeEncodeError as error:
        raise ProductionOriginsError(
            f"origin {value!r} is not ASCII; host comparison is byte-exact ASCII and a unicode "
            "spelling is a different host, not the same one written differently"
        ) from error
    return host


def declared_hosts(destinations: dict[str, str]) -> list[str]:
    """Origin hosts in declared key order. Duplicates are kept: they are distinct entries."""
    return [host_from_origin(destinations[key]) for key in ORIGIN_KEYS]


def support_domain(destinations: dict[str, str]) -> str:
    """The domain the rendered support address belongs to, taken from the address itself."""
    address = destinations[SUPPORT_EMAIL_KEY]
    if address.count("@") != 1:
        raise ProductionOriginsError(
            f"support address {address!r} has no single local@domain split"
        )
    return address.rsplit("@", 1)[1]


# --------------------------------------------------------------------------------------------
# Network adapters. Each one produces exactly one recorded observation.
# --------------------------------------------------------------------------------------------


def _fetch_once(url: str, timeout: float) -> tuple[int, str | None]:
    """One HTTPS GET with no redirect handling: the status and the raw ``Location``, if any."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    connection = HTTPSConnection(host, timeout=timeout, context=ssl.create_default_context())
    try:
        target = parts.path or "/"
        if parts.query:
            target = f"{target}?{parts.query}"
        connection.request("GET", target, headers={"Host": host, "User-Agent": "check-origins/1"})
        response = connection.getresponse()
        return response.status, response.getheader("Location")
    finally:
        connection.close()


def probe_origin(host: str, timeout: float = _PROBE_TIMEOUT_SECONDS) -> OriginObservation:
    """Request ``https://<host>/`` and follow https redirects until one settles or it cannot.

    Only the FIRST hop's connection failure is reported as an error: a later hop that fails is a
    redirect that never settled on anything reachable, which is a property of the chain.
    """
    url = f"https://{host}/"
    try:
        status, location = _fetch_once(url, timeout)
    except socket.gaierror:
        return OriginObservation(host=host, error="no address record")
    except ssl.SSLError as error:
        return OriginObservation(host=host, error=f"ssl {type(error).__name__}")
    except (OSError, TimeoutError) as error:
        return OriginObservation(host=host, error=type(error).__name__)

    seen = [url]
    redirects: list[str] = []
    while status in REDIRECT_STATUSES:
        if location is None:
            problem = "redirect with no Location header"
        else:
            nxt = urljoin(url, location)
            if not nxt.startswith("https://"):
                problem = f"redirect leaves https for {nxt}"
            elif nxt in seen:
                redirects.append(nxt)
                problem = f"redirect loop at {nxt}"
            elif len(redirects) == MAX_REDIRECTS:
                problem = f"still redirecting after {MAX_REDIRECTS} hops"
            else:
                problem = None
        if problem is not None:
            return OriginObservation(
                host=host, status=status, redirects=tuple(redirects), redirect_problem=problem
            )
        redirects.append(nxt)
        seen.append(nxt)
        url = nxt
        try:
            status, location = _fetch_once(url, timeout)
        except (OSError, TimeoutError) as error:
            return OriginObservation(
                host=host,
                status=status,
                redirects=tuple(redirects),
                redirect_problem=f"redirect target {url} unreachable ({type(error).__name__})",
            )
    return OriginObservation(host=host, status=status, redirects=tuple(redirects))


def _run_resolver(command: list[str], timeout: float) -> str | None:
    """Run one resolver client. ``None`` means "asked nobody", which is not "answered no"."""
    if shutil.which(command[0]) is None:
        return None
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout


def _mx_records_from(output: str) -> tuple[str, ...]:
    return tuple(
        line.strip()
        for line in output.splitlines()
        if line.strip() and ("mail exchanger" in line or _is_dig_mx_line(line))
    )


def _is_dig_mx_line(line: str) -> bool:
    """A ``dig +short MX`` answer is ``<preference> <exchange>`` and nothing else."""
    parts = line.split()
    return len(parts) == 2 and parts[0].isdigit()


def _authoritative_nameservers(domain: str, timeout: float) -> tuple[str, ...]:
    output = _run_resolver(["dig", "+short", "NS", domain], timeout)
    if output is None:
        return ()
    return tuple(sorted(line.strip() for line in output.splitlines() if line.strip()))


def probe_mail(domain: str, timeout: float = _MX_TIMEOUT_SECONDS) -> MailObservation:
    """Ask for the domain's MX records, and escalate past a negative answer before believing it.

    The standard library has no DNS client, and this guard adds no dependency, so it shells out
    to the resolver tools the environment already carries.

    **A negative MX answer from a recursive resolver is not a fact about the zone.** Measured
    while writing this guard: the apex's MX records were added to a live Cloudflare zone, and for
    the next half hour one host's resolver kept answering NOERROR-with-no-MX from a cached
    NXRRSET carrying the PRE-CHANGE SOA serial, while ``1.1.1.1`` and the zone's own nameservers
    all returned three records. The gate flapped between ``MAIL`` and ``NO_MAIL`` between runs of
    the same command, minutes apart, against an unchanging zone.

    A flapping gate is worse than a red one: it makes the publish path's refusal look arbitrary,
    and it invites somebody to re-run until it goes green. So a negative answer is escalated to
    the zone's authoritative nameservers -- the only servers that can actually say no -- and only
    a negative from THEM is recorded as ``NO_MAIL``.

    The escalation runs on the negative path only, which is the safe direction: it can turn a
    false ``NO_MAIL`` into a true ``MAIL``, and it can never invent records the zone does not
    publish. Asking nobody at all (no resolver client installed, every query failing) still reads
    as ``NO_MAIL``, deliberately: a gate that cannot establish mail acceptance must not report it.
    """
    system_output = None
    for command in (
        ["dig", "+short", "MX", domain],
        ["nslookup", "-query=MX", domain],
    ):
        system_output = _run_resolver(command, timeout)
        if system_output is not None:
            break
    if system_output is not None:
        records = _mx_records_from(system_output)
        if records:
            return MailObservation(host=domain, mx_records=records, source="the system resolver")

    for nameserver in _authoritative_nameservers(domain, timeout):
        output = _run_resolver(["dig", "+short", "MX", domain, f"@{nameserver}"], timeout)
        if output is None:
            continue
        records = _mx_records_from(output)
        if records:
            return MailObservation(host=domain, mx_records=records, source=nameserver)

    return MailObservation(
        host=domain, mx_records=(), source="the system resolver and the zone's nameservers"
    )


# --------------------------------------------------------------------------------------------
# Entry point.
# --------------------------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Probe every origin the destinations record bakes into a build, plus the apex's "
            "mail acceptance. Exits non-zero while any origin is unserved."
        )
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="write exactly one JSON object to stdout; the exit status is unchanged",
    )
    parser.add_argument(
        "--hosts",
        default=None,
        metavar="HOST,HOST",
        help=(
            "override the ORIGIN list only, with a comma-separated list of bare hostnames. The "
            "mail row is always derived from the parsed support address, because mail "
            "acceptance is a property of the domain and not of whichever host was passed in."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        destinations = parse_destinations(DESTINATIONS_FILE.read_text(encoding="utf-8"))
        hosts = (
            [entry.strip() for entry in args.hosts.split(",") if entry.strip()]
            if args.hosts is not None
            else declared_hosts(destinations)
        )
        mail_domain = support_domain(destinations)
        report = build_report(
            hosts,
            [probe_origin(host) for host in hosts],
            probe_mail(mail_domain),
        )
    except (ProductionOriginsError, OSError) as error:
        print(f"production origins failed: {error}", file=sys.stderr)
        return 1

    if args.json:
        print(render_json(report))
    else:
        print(render_rows(report))
        print(summary_line(report))
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
