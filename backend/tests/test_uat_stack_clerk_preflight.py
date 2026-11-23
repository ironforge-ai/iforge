"""Prove that ``scripts/uat-stack.sh`` reports Clerk token verification the way the backend does it.

G-06.3-R2-1. The UAT stack printed a red "CLERK_PEM_PUBLIC_KEY unset ... Do NOT record ..." line
whenever the PEM was empty. But ``backend/app/core/auth.py`` ``_resolve_clerk_signing_key``
verifies tokens with the PEM when one is set, and otherwise through the Clerk JWKS client built
from ``CLERK_JWT_ISSUER``. It answers 503 only when neither exists. A JWKS-configured stack was
therefore told it was broken while the gate probe had already answered 401 (live). A preflight
that cries wolf trains the operator to skip its red lines, including the one that matters.

``clerk_verification_mode`` mirrors that precedence: ``pem``, then ``jwks``, then ``none``. Only
``none`` gets the red line. The report reads the settings the backend process will load, and that
is not the same as proving tokens verify.

The "Access-gate reachability" probe does not prove it either (06.3 WR-02). It sends
``Bearer uat-probe``, which ``PyJWKClient`` fails to decode before any JWKS fetch, so the backend
answers 401 for a typo'd, foreign or unreachable issuer exactly as for a good one. That 401 proves
only that the route is mounted and Clerk is configured. In ``jwks`` mode the script therefore also
fetches ``<issuer>/.well-known/jwks.json`` itself (``clerk_jwks_probe``) and prints a red line when
the document is unreachable or has no ``keys``. Only a real session token proves verification end
to end. These tests run the probe helper against a stubbed ``curl`` and against a closed loopback
port, so they need no network.

The messages must not cite Phase 03 UAT test numbers, which mean nothing in 06.3. The 503 hint
names the settings that fix it, instead of telling the operator to leave the issuer empty.

Fixture issuers use reserved example hosts only (``https://clerk.example.test``), never a real
Clerk instance.
"""

from __future__ import annotations

import re
import shutil
import subprocess

import pytest

from tests.doc_contract import REPO_ROOT

UAT_STACK = REPO_ROOT / "scripts" / "uat-stack.sh"
FIXTURE_ISSUER = "https://clerk.example.test"

_LINES = UAT_STACK.read_text(encoding="utf-8").splitlines()


def _is_comment(line: str) -> bool:
    return line.lstrip().startswith("#")


def _step_block(title: str) -> list[str]:
    """The lines of one ``step "<title>"`` section, up to the next ``step`` call."""
    start = next(i for i, line in enumerate(_LINES) if line == f'step "{title}"')
    end = next(
        (i for i in range(start + 1, len(_LINES)) if _LINES[i].startswith("step ")),
        len(_LINES),
    )
    return _LINES[start:end]


def _helper_source() -> str:
    start = _LINES.index("clerk_verification_mode() {")
    end = _LINES.index("}", start)
    body = "\n".join(_LINES[start : end + 1])
    assert body.strip(), "clerk_verification_mode has no body"
    return body


def _function_source(name: str) -> str:
    start = _LINES.index(f"{name}() {{")
    end = _LINES.index("}", start)
    return "\n".join(_LINES[start : end + 1])


def _mode(pem: str, issuer: str) -> str:
    script = _helper_source() + '\nclerk_verification_mode "$1" "$2"\n'
    result = subprocess.run(
        ["bash", "-c", script, "clerk_verification_mode", pem, issuer],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


# ------------------------------------------------------------------ the helper


def test_helper_is_defined_once() -> None:
    definitions = [line for line in _LINES if line == "clerk_verification_mode() {"]
    assert len(definitions) == 1


@pytest.mark.parametrize(
    ("pem", "issuer", "expected"),
    [
        pytest.param("True", "", "pem", id="pem only"),
        pytest.param("True", FIXTURE_ISSUER, "pem", id="pem wins over the issuer"),
        pytest.param("False", FIXTURE_ISSUER, "jwks", id="issuer only verifies through jwks"),
        pytest.param("False", "", "none", id="neither"),
        pytest.param("False", "   ", "none", id="whitespace issuer is not an issuer"),
        pytest.param("", "", "none", id="settings unreadable"),
    ],
)
def test_mode_follows_the_backend_precedence(pem: str, issuer: str, expected: str) -> None:
    assert _mode(pem, issuer) == expected


# ------------------------------------------------------------------ the JWKS probe (WR-02)

# grn/red stand-ins that tag the colour instead of emitting ANSI codes.
_PRINT_STUBS = 'grn() { printf "GRN %s\\n" "$*"; }\nred() { printf "RED %s\\n" "$*"; }\n'


def _jwks_probe(issuer: str, *, curl_stub: str | None) -> tuple[str, list[str]]:
    """Run ``clerk_jwks_probe`` under ``set -euo pipefail``, as the script does.

    ``curl_stub`` replaces curl with a bash function. It traces its ``$*`` to fd 3, which is
    the real stderr, because the helper sends curl's own stderr to /dev/null; the test reads
    the URL and flags from that trace. ``None`` keeps the real curl.
    """
    script = "set -euo pipefail\nexec 3>&2\n" + _PRINT_STUBS
    if curl_stub is not None:
        script += 'curl() { printf "CURL %s\\n" "$*" >&3; ' + curl_stub + "; }\n"
    script += _function_source("clerk_jwks_probe")
    script += '\nCLERK_JWKS_OK=""\nclerk_jwks_probe "$1"\nprintf "OK=%s\\n" "$CLERK_JWKS_OK"\n'
    result = subprocess.run(
        ["bash", "-c", script, "clerk_jwks_probe", issuer],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    out = result.stdout.splitlines()
    ok = next(line for line in out if line.startswith("OK="))
    return ok.removeprefix("OK="), [*out, *result.stderr.splitlines()]


def test_jwks_probe_is_defined_once() -> None:
    assert [line for line in _LINES if line == "clerk_jwks_probe() {"] == ["clerk_jwks_probe() {"]


def test_jwks_probe_passes_a_document_with_keys() -> None:
    ok, lines = _jwks_probe(
        FIXTURE_ISSUER + "/", curl_stub='printf \'%s\' \'{"keys":[{"kid":"k1"}]}\''
    )
    assert ok == "true", lines
    url = f"{FIXTURE_ISSUER}/.well-known/jwks.json"
    assert f"GRN ✓ Clerk JWKS reachable at {url}" in lines, lines
    assert not any(line.startswith("RED") for line in lines), lines
    curl_calls = [line for line in lines if line.startswith("CURL ")]
    assert len(curl_calls) == 1, lines
    assert url in curl_calls[0] and "--max-time" in curl_calls[0], curl_calls


@pytest.mark.parametrize(
    "curl_stub",
    [
        pytest.param("return 22", id="http error"),
        pytest.param("return 28", id="timeout"),
        pytest.param("printf '%s' '<html>not found</html>'", id="200 without keys"),
    ],
)
def test_jwks_probe_fails_red_when_the_document_is_unusable(curl_stub: str) -> None:
    ok, lines = _jwks_probe(FIXTURE_ISSUER, curl_stub=curl_stub)
    assert ok == "false", lines
    red = [line for line in lines if line.startswith("RED")]
    assert red and "Clerk JWKS not reachable" in red[0], lines
    assert any("Do NOT record any gate test" in line for line in red), lines
    assert not any(line.startswith("GRN") for line in lines), lines


@pytest.mark.skipif(shutil.which("curl") is None, reason="curl is not installed")
def test_jwks_probe_fails_red_against_an_unreachable_issuer() -> None:
    # Port 1 on loopback is closed: the real curl is refused at once, no network involved.
    ok, lines = _jwks_probe("http://127.0.0.1:1", curl_stub=None)
    assert ok == "false", lines
    assert any(line.startswith("RED ✗ Clerk JWKS not reachable") for line in lines), lines


def test_reachability_step_probes_jwks_only_in_jwks_mode() -> None:
    block = [line for line in _step_block("Access-gate reachability") if not _is_comment(line)]
    calls = [i for i, line in enumerate(block) if re.search(r"\bclerk_jwks_probe\b", line)]
    assert len(calls) == 1, block
    condition = next(
        block[i] for i in range(calls[0], -1, -1) if re.match(r"\s*(if|elif)\b", block[i])
    )
    assert re.search(r'"?\$\{?g_clerk_mode(:-)?\}?"? == "?jwks"?', condition), condition
    assert re.search(r'clerk_jwks_probe "\$\{?g_issuer', block[calls[0]]), block[calls[0]]


def test_probe_rejection_does_not_claim_tokens_verify() -> None:
    block = _step_block("Access-gate reachability")
    start = next(i for i, line in enumerate(block) if line.strip() == "401|403)")
    end = next(i for i in range(start, len(block)) if block[i].strip() == ";;")
    branch = [line for line in block[start:end] if not _is_comment(line)]
    text = "\n".join(branch)
    assert "gate is live" not in text, text
    assert "mounted" in text, text
    assert "does NOT prove real tokens verify" in text, text


# ------------------------------------------------------------------ the Gate configuration report


def test_gate_configuration_reports_the_verification_mode() -> None:
    block = [line for line in _step_block("Gate configuration") if not _is_comment(line)]
    text = "\n".join(block)
    assert re.search(r'clerk_verification_mode "\$g_pem" "\$g_issuer"', text), text
    assert any("Clerk verification" in line for line in block if "echo" in line), text


def test_clerk_failure_is_red_only_when_nothing_verifies_tokens() -> None:
    block = _step_block("Gate configuration")
    failures = [
        i
        for i, line in enumerate(block)
        if not _is_comment(line)
        and re.search(r'\bred "✗', line)
        and re.search(r"clerk", line, re.IGNORECASE)
    ]
    assert len(failures) == 1, [block[i] for i in failures]
    index = failures[0]
    condition = next(
        (
            block[i]
            for i in range(index, -1, -1)
            if re.match(r"\s*(if|elif)\b", block[i]) or re.match(r"\s*(fi|else)\b", block[i])
        ),
        "",
    )
    assert re.match(r"\s*(if|elif)\b", condition), (
        f"the red Clerk line is not inside an if/elif branch: {block[index]!r}"
    )
    assert re.search(r'"?\$\{?\w*mode\w*\}?"? == "?none"?', condition), condition


# ------------------------------------------------------------------ messages


def test_no_message_cites_phase_03_test_numbers() -> None:
    offenders = [
        f"{number}: {line}"
        for number, line in enumerate(_LINES, 1)
        if not _is_comment(line) and re.search(r"tests? [0-9]+(/[0-9]+)+", line)
    ]
    assert not offenders, "\n".join(offenders)


def test_503_hint_names_the_issuer_as_a_fix() -> None:
    block = _step_block("Access-gate reachability")
    start = next(i for i, line in enumerate(block) if line.strip() == "503)")
    end = next(i for i in range(start, len(block)) if block[i].strip() == ";;")
    branch = "\n".join(block[start:end])
    assert "Fix: set CLERK_JWT_ISSUER" in branch, branch
    assert "leave CLERK_JWT_ISSUER" not in branch, branch


# ------------------------------------------------------------------ script integrity


def test_uat_stack_parses() -> None:
    result = subprocess.run(
        ["bash", "-n", str(UAT_STACK)], capture_output=True, text=True, check=False, timeout=30
    )
    assert result.returncode == 0, result.stderr


def test_help_range_is_all_comments() -> None:
    # -h|--help prints lines 2-61 with sed, so a code line there would print as help text.
    assert all(line.startswith("#") for line in _LINES[1:61]), [
        line for line in _LINES[1:61] if not line.startswith("#")
    ]
