"""Keep the OSS sign-up runbook free of hosted infrastructure, mechanically (G-06.3-8).

``docs/runbooks/clerk-sign-up-access.md`` is the open-source runbook for deciding who may create
an iforge account (06.3 D-02). The AGENTS.md "OSS Boundary" says hosted infrastructure is owned
outside this repository: no hosted service runbooks, no private addresses, no internal deployment
details. Staging specifics -- which host a walk targets, which Clerk instance backs it, which
tailnet name an operator browses from -- live only in the private hosted repository.

Until this suite that property held only by care: plan 06.3-06 checked it once with a negative
grep in its verify step, and nothing re-checked it after. This module pins it.

FENCED CODE BLOCKS ARE SCANNED LIKE PROSE. A fenced block is what an operator pastes, so a staging
host or a Clerk key inside a fence is the most harmful place for one to survive, not an exempt
one -- the same decision ``test_docs_identity_contract.py`` makes for the same reason.

Every pattern is proven to fire by a synthetic fixture, written to ``tmp_path`` and fed through
the same collector as the real runbook, and the runbook's own legitimate vocabulary is proven to
produce nothing. The fixtures use reserved example names (``example.com``) and generic addresses
from the private ranges themselves; they never carry a real host, instance id or key.

Widening ``SCANNED_DOCUMENTS`` to another runbook is a one-line change once that runbook is clean.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.doc_contract import REPO_ROOT

SCANNED_DOCUMENTS = ("docs/runbooks/clerk-sign-up-access.md",)

# (kind, pattern, reason). The reason is printed beside every finding, so a failure says why the
# line is not allowed rather than only that it matched.
PRIVATE_IDENTIFIER_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "staging_name",
        re.compile(r"\bstaging\b", re.IGNORECASE),
        "names the staging environment; staging specifics live in the hosted repository",
    ),
    (
        "stage_host",
        re.compile(
            r"(?<![\w.-])[a-z0-9-]*(?:stage|stg)[a-z0-9-]*(?:\.[a-z0-9-]+)+\b", re.IGNORECASE
        ),
        "a stage/stg host name is hosted infrastructure",
    ),
    (
        "product_zone_subdomain",
        re.compile(r"(?<![\w.-])(?:[a-z0-9-]+\.)+iforge\.dev\b", re.IGNORECASE),
        "a host in the iforge.dev product zone is a hosted deployment detail",
    ),
    (
        "clerk_instance_id",
        re.compile(r"\bins_[A-Za-z0-9]{8,}"),
        "a Clerk instance id identifies a hosted Clerk instance",
    ),
    (
        "clerk_application_id",
        re.compile(r"\bapp_[A-Za-z0-9]{8,}"),
        "a Clerk application id identifies a hosted Clerk application",
    ),
    (
        "clerk_api_key",
        re.compile(r"\b[ps]k_(?:test|live)_[A-Za-z0-9+/=]{8,}"),
        "a Clerk key (publishable keys encode the instance's Frontend API host)",
    ),
    (
        "clerk_frontend_api_host",
        re.compile(r"(?<![\w.-])(?:[a-z0-9-]+\.)+clerk\.accounts\.dev\b", re.IGNORECASE),
        "a clerk.accounts.dev host is a specific instance's Frontend API",
    ),
    (
        "clerk_custom_frontend_api",
        re.compile(r"(?<![\w.-])clerk\.[a-z0-9-]+(?:\.[a-z0-9-]+)+\b", re.IGNORECASE),
        "a clerk.<domain> host is a specific instance's custom Frontend API",
    ),
    (
        "private_ipv4",
        re.compile(
            r"(?<![\w.])(?:"
            r"10(?:\.\d{1,3}){3}"
            r"|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2}"
            r"|192\.168(?:\.\d{1,3}){2}"
            r"|100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])(?:\.\d{1,3}){2}"
            r"|169\.254(?:\.\d{1,3}){2}"
            r"|127(?:\.\d{1,3}){3}"
            r")(?!\w|\.\d)"
        ),
        "a private, CGNAT/tailnet, link-local or loopback address is internal infrastructure",
    ),
    (
        "private_hostname",
        re.compile(
            r"\blocalhost\b"
            r"|(?<![\w.-])(?:[a-z0-9-]+\.)+(?:ts\.net|internal|local|lan|home\.arpa)\b",
            re.IGNORECASE,
        ),
        "a loopback, tailnet or private-zone host name is internal infrastructure",
    ),
    (
        "private_ipv6",
        re.compile(r"\bf[cd][0-9a-f]{2}:[0-9a-f:]*", re.IGNORECASE),
        "an IPv6 unique local address (fc00::/7) is internal infrastructure",
    ),
)

_REASONS = {kind: reason for kind, _pattern, reason in PRIVATE_IDENTIFIER_PATTERNS}


def collect_findings(path: Path, root: Path = REPO_ROOT) -> list[tuple[str, int, str, str]]:
    """Every private identifier in ``path`` as ``(relative_path, line, kind, match)``.

    ``line`` is 1-based. Every line is scanned, fenced code blocks included (see the module
    docstring), with every pattern, and every occurrence is reported. The result is sorted by the
    whole tuple, so two runs over the same file produce identical output.
    """
    try:
        relative = str(path.relative_to(root))
    except ValueError:
        relative = path.name
    findings: list[tuple[str, int, str, str]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        for kind, pattern, _reason in PRIVATE_IDENTIFIER_PATTERNS:
            findings.extend((relative, number, kind, m.group(0)) for m in pattern.finditer(line))
    return sorted(findings)


def _render(findings: list[tuple[str, int, str, str]]) -> str:
    return "\n".join(
        f"{path}:{line}: {kind}: {match!r} -- {_REASONS[kind]}"
        for path, line, kind, match in findings
    )


def _fixture(tmp_path: Path, text: str, name: str = "fixture.md") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# ------------------------------------------------------------------ the real runbook


def test_the_oss_runbook_names_no_hosted_infrastructure() -> None:
    assert SCANNED_DOCUMENTS, "SCANNED_DOCUMENTS is empty -- this suite would check nothing"
    findings: list[tuple[str, int, str, str]] = []
    for relative in SCANNED_DOCUMENTS:
        document = REPO_ROOT / relative
        assert document.is_file(), f"{relative} does not exist"
        assert document.read_text(encoding="utf-8").strip(), f"{relative} is empty"
        findings.extend(collect_findings(document))
    assert findings == [], "hosted details in an OSS runbook:\n" + _render(findings)


# ------------------------------------------------------------------ every kind fires

SYNTHETIC_CASES: tuple[tuple[str, str], ...] = (
    ("staging_name", "Run this against the staging environment first."),
    ("stage_host", "Point the CLI at api-stage.example.com before the walk."),
    ("product_zone_subdomain", "Open https://example.iforge.dev in the browser."),
    ("clerk_instance_id", "The instance is ins_abcdef123456 in the Dashboard."),
    ("clerk_application_id", "The application is app_abcdef123456 in the Dashboard."),
    ("clerk_api_key", "export CLERK_PUBLISHABLE_KEY=pk_test_AAAAbbbb0000"),
    ("clerk_frontend_api_host", "Sessions come from example-app-12.clerk.accounts.dev here."),
    ("clerk_custom_frontend_api", "The Frontend API is clerk.example.com for this instance."),
    ("private_ipv4", "curl http://10.0.0.1:8000/health"),
    ("private_ipv4", "The database listens on 172.16.0.1."),
    ("private_ipv4", "Browse to 192.168.0.1 from the LAN."),
    ("private_ipv4", "The tailnet address is 100.64.0.1."),
    ("private_ipv4", "Metadata lives at 169.254.0.1 on the host."),
    ("private_ipv4", "Bind to 127.0.0.1 first."),
    ("private_hostname", "curl http://localhost:8000/health"),
    ("private_hostname", "Open https://box.example.ts.net:3000 from the laptop."),
    ("private_hostname", "The database is db.internal on port 5432."),
    ("private_hostname", "Print from printer.local after setup."),
    ("private_hostname", "Mount the share from nas.lan first."),
    ("private_hostname", "The router answers at router.home.arpa."),
    ("private_ipv6", "The ULA prefix is fd12:3456::1 on that network."),
)


@pytest.mark.parametrize(
    ("kind", "text"), SYNTHETIC_CASES, ids=[f"{kind}:{text}" for kind, text in SYNTHETIC_CASES]
)
def test_each_private_identifier_kind_is_reported(tmp_path: Path, kind: str, text: str) -> None:
    fixture = _fixture(tmp_path, f"# Fixture\n\n{text}\n")
    findings = collect_findings(fixture, root=tmp_path)
    assert any(found_kind == kind for _path, _line, found_kind, _match in findings), (
        f"{kind} did not fire on {text!r}; findings: {findings}"
    )


def test_every_pattern_kind_has_a_synthetic_case() -> None:
    declared = {kind for kind, _pattern, _reason in PRIVATE_IDENTIFIER_PATTERNS}
    covered = {kind for kind, _text in SYNTHETIC_CASES}
    assert declared, "PRIVATE_IDENTIFIER_PATTERNS is empty"
    assert declared == covered
    assert all(reason.strip() for _kind, _pattern, reason in PRIVATE_IDENTIFIER_PATTERNS)


def test_a_fenced_block_is_scanned_like_prose(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, "Paste this:\n\n```bash\ncurl http://localhost:8000\n```\n")
    findings = collect_findings(fixture, root=tmp_path)
    assert ("fixture.md", 4, "private_hostname", "localhost") in findings


# ------------------------------------------------------------------ the runbook's own vocabulary


CLEAN_PROSE = """# Sign-up Access (Clerk)

The keys that control sign-up are `sign_up_mode` and `auth_access_control`.

```bash
npx clerk@latest config patch --json '{"sign_up_mode": "restricted"}'
curl -X PUT "$API/v1/admin/auth/billing-exemptions" \\
  -H "X-Admin-Key: $ADMIN_API_KEY" -H 'Content-Type: application/json' \\
  -d '{"target_clerk_id": "user_...", "exempt": true}'
```

See https://clerk.com/docs and the pin in
`backend/tests/test_user_provisioning_first_touch.py`.
"""


def test_the_runbooks_legitimate_vocabulary_produces_no_finding(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, CLEAN_PROSE)
    findings = collect_findings(fixture, root=tmp_path)
    assert findings == [], _render(findings)


# ------------------------------------------------------------------ shape of the result


def test_findings_are_sorted_with_one_based_lines(tmp_path: Path) -> None:
    fixture = _fixture(
        tmp_path,
        "clean first line\nBind to 127.0.0.1 or localhost.\n\nThe staging box.\n",
    )
    findings = collect_findings(fixture, root=tmp_path)
    assert findings == [
        ("fixture.md", 2, "private_hostname", "localhost"),
        ("fixture.md", 2, "private_ipv4", "127.0.0.1"),
        ("fixture.md", 4, "staging_name", "staging"),
    ]
    assert findings == sorted(findings)
