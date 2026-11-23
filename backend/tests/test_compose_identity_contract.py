"""Bind the compose/Postgres rename runbook to the Compose file it describes.

Plan 03.3-08 flipped the self-hosted Compose project name and the Postgres database and role
defaults, and shipped ``docs/runbooks/iforge-compose-postgres-migration.md`` for the operator who
wants to move a LIVE stack. The code deliberately performs neither rename (P33-06): it is a
destructive operation on somebody's production data, and no CI runs against this origin that
would catch a mistake in it.

That deliberate refusal is exactly what makes this contract necessary. A procedure nothing
executes is a procedure nothing checks, so the only failure mode left is silent drift -- the
Compose default moves again in some later phase and the runbook keeps telling operators to
rename their cluster to a name the repository no longer uses. Every assertion below is a claim a
human reading a diff would plausibly miss.

Four properties, in the order they matter:

* the runbook's rename TARGETS are the Compose file's current defaults, read from the Compose
  file rather than restated here;
* the runbook LINKS the Compose file as its authority, the same move
  ``docs/runbooks/iforge-env-migration.md`` makes towards the environment contract;
* the runbook does not carry a COPY of those defaults, because a copy in a document is a second
  place to forget;
* the backup step precedes every destructive command IN DOCUMENT ORDER. Ordering is the claim
  most likely to rot: reordering two sections leaves every substring assertion in this file green
  while turning the procedure into one that destroys data before it is backed up.

A fifth property, added by plan 03.3-18 for verification gap 6: the DEFAULTS moved and the LIVE
identities were left for an operator, and until then that split was argued in an allowlist
comment rather than asserted. ``config/deploy.yml``'s ``psql`` alias was the last such site. The
iforge Kamal stack was never deployed, so no running cluster carries the pre-migration role or
database, and the alias moved with the defaults. (The preview stack's Compose project name was
the other site until upstream sync step 4 deleted that stack, #1560; 06.1-11 accepted the
deletion.) ``LIVE_ATTACHED_IDENTITY_SITES`` is therefore empty, and the assertions below admit no
third state: a legacy identity that is neither moved nor enumerated fails with its file and line
named.

Shape follows ``test_infra_layout_contract.py``: repository root and prose normalisation from
``tests.doc_contract``, raw-line parsing wherever structure matters, and a loud failure on an
enumeration that yields nothing, so no assertion can pass by comparing an empty set to itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.doc_contract import REPO_ROOT as _REPO_ROOT
from tests.doc_contract import normalized_text as _normalized_text

_RUNBOOK = _REPO_ROOT / "docs" / "runbooks" / "iforge-compose-postgres-migration.md"
_COMPOSE = _REPO_ROOT / "deploy" / "self-host" / "docker-compose.yml"

# The Compose file is the authority, so the expected values are READ from it. Each pattern
# captures the default -- the text after `:-` -- of one `${VAR:-default}` interpolation.
_COMPOSE_DEFAULTS = {
    "NEW_PROJECT": re.compile(r"^name:\s*\$\{COMPOSE_PROJECT_NAME:-([^}]+)\}", re.MULTILINE),
    "NEW_DB": re.compile(r"^\s*POSTGRES_DB:\s*\$\{POSTGRES_DB:-([^}]+)\}", re.MULTILINE),
    "NEW_USER": re.compile(r"^\s*POSTGRES_USER:\s*\$\{POSTGRES_USER:-([^}]+)\}", re.MULTILINE),
}

# The runbook states its targets as shell assignments an operator pastes, which is also the form
# that makes them mechanically comparable. `NEW_ROLE` is the runbook's word for what the Compose
# file calls `POSTGRES_USER`; they name one object and the mapping is asserted, not assumed.
_RUNBOOK_TARGETS = {
    "NEW_PROJECT": re.compile(r"^NEW_PROJECT=(\S+)$", re.MULTILINE),
    "NEW_DB": re.compile(r"^NEW_DB=(\S+)$", re.MULTILINE),
    "NEW_USER": re.compile(r"^NEW_ROLE=(\S+)$", re.MULTILINE),
}

_AUTHORITY_LINK = "../../deploy/self-host/docker-compose.yml"

# The backup step, and every command that cannot be undone by editing a file back. Matched inside
# fenced blocks only: the opening paragraphs discuss the rename statements in prose on purpose,
# and a naive whole-document search would read that discussion as a command and always fail.
_BACKUP_MARKER = "pg_dumpall"
_DESTRUCTIVE_MARKERS = (
    "alter database",
    "alter role",
    'docker compose -p "$OLD_PROJECT" down',
    "docker volume rm",
)

# Content this repository's OSS boundary (AGENTS.md) keeps out of tracked docs. The runbook is a
# generic operator procedure; a hosted address or a deployment-specific path landing in it is the
# leak this pattern set exists to catch.
_HOSTED_TOPOLOGY_PATTERNS = (
    re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    re.compile(r"\bdeploy_host\b"),
    re.compile(r"\bkamal\b"),
    re.compile(r"\baccessory\b"),
    re.compile(r"/home/[a-z][a-z0-9_-]*/"),
)

# Loopback is the one address family a generic procedure legitimately names, and the operator
# runs these commands on their own machine. Excluding it by value keeps the address guard from
# being satisfiable only by a document that says nothing.
_ALLOWED_ADDRESSES = frozenset({"127.0.0.1", "0.0.0.0"})


def _fenced_blocks() -> list[tuple[int, str]]:
    """Every fenced code block as ``(offset of its first line, text)``.

    Offsets are into the raw document, so they order blocks against each other and against the
    prose between them. Raises if the document has no fenced block at all -- a runbook with no
    commands would make every ordering assertion below vacuously true.
    """
    raw = _RUNBOOK.read_text(encoding="utf-8")
    blocks: list[tuple[int, str]] = []
    offset = 0
    current: list[str] | None = None
    start = 0
    for line in raw.splitlines(keepends=True):
        if line.startswith("```"):
            if current is None:
                current, start = [], offset + len(line)
            else:
                blocks.append((start, "".join(current)))
                current = None
        elif current is not None:
            current.append(line)
        offset += len(line)

    assert current is None, f"{_RUNBOOK} has an unclosed fenced code block"
    assert blocks, f"{_RUNBOOK} contains no fenced code block; it prescribes no commands"
    return blocks


def _first_offset(marker: str) -> int | None:
    """Offset of the first fenced-block line containing ``marker``, or None."""
    for start, text in _fenced_blocks():
        lowered = text.casefold()
        index = lowered.find(marker.casefold())
        if index >= 0:
            return start + index
    return None


def _sole_match(pattern: re.Pattern[str], text: str, label: str) -> str:
    matches = pattern.findall(text)
    assert len(matches) == 1, (
        f"expected exactly one {label} matching {pattern.pattern!r}, found {len(matches)}: "
        f"{matches}. An identity declared twice is an identity that can disagree with itself."
    )
    return matches[0].strip().strip('"').strip("'")


def test_runbook_rename_targets_are_the_compose_defaults() -> None:
    """The names the runbook tells an operator to rename TO are the Compose file's own defaults.

    Read from the Compose file rather than pinned here, so this test cannot be the thing that
    goes stale. If a later phase moves the default again, this fails and names both values.
    """
    compose = _COMPOSE.read_text(encoding="utf-8")
    runbook = _RUNBOOK.read_text(encoding="utf-8")

    for key, compose_pattern in _COMPOSE_DEFAULTS.items():
        expected = _sole_match(compose_pattern, compose, f"{key} default in {_COMPOSE.name}")
        actual = _sole_match(_RUNBOOK_TARGETS[key], runbook, f"{key} target in {_RUNBOOK.name}")
        assert actual == expected, (
            f"the runbook tells an operator to rename to {actual!r}, but "
            f"{_COMPOSE.name} now defaults to {expected!r}. A procedure that renames a live "
            "cluster to a name the repository no longer uses is worse than no procedure."
        )


def test_runbook_points_at_the_compose_file_as_its_authority() -> None:
    """It links the Compose file, the way the env runbook links the environment contract."""
    runbook = _RUNBOOK.read_text(encoding="utf-8")
    assert _AUTHORITY_LINK in runbook, (
        f"{_RUNBOOK} does not link {_AUTHORITY_LINK}. The runbook's stated position is that the "
        "Compose file is the authority for which values exist; an unlinked claim to that effect "
        "is prose, not a reference an operator can follow."
    )
    assert (_REPO_ROOT / "deploy" / "self-host" / "docker-compose.yml").is_file()


def test_runbook_does_not_restate_the_compose_defaults() -> None:
    """No copy of the authority.

    The runbook names its rename targets -- it has to, they are what the operator types -- but it
    must not reproduce the Compose file's `${VAR:-default}` declarations, which is the form that
    turns a reference into a duplicate somebody later edits in one place only.
    """
    runbook = _RUNBOOK.read_text(encoding="utf-8")
    for declaration in (
        "POSTGRES_DB: ${POSTGRES_DB:-",
        "POSTGRES_USER: ${POSTGRES_USER:-",
        "name: ${COMPOSE_PROJECT_NAME:-",
    ):
        assert declaration not in runbook, (
            f"{_RUNBOOK} restates the Compose declaration {declaration!r}. Point at the "
            "authority; a copy in a document is a second place to forget."
        )


def test_backup_precedes_every_destructive_command() -> None:
    """Document order, not merely presence.

    Both halves are needed. Presence alone passes on a document that tells the operator to rename
    the cluster and then, three sections later, mentions taking a dump -- which is a procedure
    that destroys data before it protects it, written in the right words.
    """
    backup_at = _first_offset(_BACKUP_MARKER)
    assert backup_at is not None, (
        f"{_RUNBOOK} prescribes no {_BACKUP_MARKER} command. The backup is the only step that "
        "makes the rest of this procedure recoverable."
    )

    found = {marker: _first_offset(marker) for marker in _DESTRUCTIVE_MARKERS}
    missing = [marker for marker, at in found.items() if at is None]
    assert not missing, (
        f"{_RUNBOOK} contains no command for {missing}. This test orders the backup against the "
        "destructive commands; with none present it would pass over a runbook that prescribes "
        "nothing at all."
    )

    for marker, at in found.items():
        assert at is not None and at > backup_at, (
            f"{marker!r} appears at offset {at} in {_RUNBOOK.name}, before the {_BACKUP_MARKER} "
            f"step at {backup_at}. Nothing after the backup is undone by editing a file back, "
            "so the backup goes first in the document an operator reads top to bottom."
        )


def test_runbook_states_the_constraints_postgres_imposes() -> None:
    """The two cannot-rename-the-current rules, and the password consequence with its check."""
    text = _normalized_text(_RUNBOOK)

    for required in (
        "you cannot rename the database you are connected to",
        "you cannot rename the role you are authenticated as",
        "renaming a role discards its password if that password is md5-encrypted",
        "scram-sha-256",
        "pg_authid",
    ):
        assert required in text, f"{_RUNBOOK} does not state: {required!r}"


def test_runbook_makes_volume_reattachment_explicit() -> None:
    """The compose-project half is a down-then-up with the volume copy spelled out.

    The failure this guards is the quiet one: a renamed project finds no volumes under its new
    prefix, creates empty ones, and PostgreSQL initialises cleanly into them. The stack comes up
    green and empty, and nothing reports an error.
    """
    text = _normalized_text(_RUNBOOK)
    for required in (
        "the project name prefixes every named volume the project creates",
        "creates empty ones",
    ):
        assert required in text, f"{_RUNBOOK} does not state: {required!r}"

    down_at = _first_offset('docker compose -p "$OLD_PROJECT" down')
    copy_at = _first_offset("docker volume create")
    up_at = _first_offset('docker compose -p "$NEW_PROJECT" up')
    assert None not in (down_at, copy_at, up_at), (
        f"{_RUNBOOK} is missing part of the down-then-up sequence: "
        f"down={down_at}, copy={copy_at}, up={up_at}"
    )
    assert down_at < copy_at < up_at, (  # type: ignore[operator]
        "the volume copy must sit between the down and the up: copying while the old project is "
        "running reads a live data directory, and copying after the new one starts is too late "
        "-- it has already initialised an empty database over the top."
    )


def test_runbook_names_no_hosted_topology() -> None:
    """AGENTS.md's OSS boundary, asserted rather than trusted.

    Hosted infrastructure is owned outside this repository. A runbook is the likeliest place for
    a private address or a deployment-specific path to arrive, because the person writing it has
    a specific deployment in mind while writing a generic procedure.
    """
    raw = _RUNBOOK.read_text(encoding="utf-8")
    assert len(raw) > 2000, (
        f"{_RUNBOOK} is {len(raw)} bytes; the absence checks below would pass over a stub"
    )

    for pattern in _HOSTED_TOPOLOGY_PATTERNS:
        for match in pattern.finditer(raw.casefold()):
            assert match.group(0) in _ALLOWED_ADDRESSES, (
                f"{_RUNBOOK} names {match.group(0)!r}. Hosted service runbooks, private "
                "addresses and internal deployment details are owned outside this repository "
                "(AGENTS.md, OSS Boundary); this document is a generic operator procedure."
            )


# ---------------------------------------------------------------------------------------------
# Gap 6 (plan 03.3-18): the moved-vs-live split, enumerated in one place and asserted.
# ---------------------------------------------------------------------------------------------

_RUNBOOK_FILENAME = "iforge-compose-postgres-migration.md"

# The identity every site is expected to have moved TO. A value is "moved" when it is this name
# or a name built from it (`iforge-self-host`, `iforge-preview`), which is the shape every moved
# site in these two files actually uses.
_MOVED_IDENTITY = "iforge"

# The whole contract, in one tuple: `(repository-relative path, anchor substring, reason)`.
# A site that still names the pre-migration identity must either move or appear here with a
# reason. Adding an entry with an empty reason is rejected -- an unexplained carve-out is the
# state this enumeration exists to make impossible.
# Empty: the last entry was config/deploy.yml's psql alias, which moved to the iforge identity
# because no Kamal cluster was ever created under the pre-migration one.
LIVE_ATTACHED_IDENTITY_SITES: tuple[tuple[str, str, str], ...] = ()

# What counts as a site NAMING a Postgres database, a Postgres role, or a Compose project.
# Deliberately narrow, and narrow in a way worth stating: backup stanza names (`STANZA:`,
# `PGBACKREST_STANZA:`) are NOT matched, because the runbook's "What this does not cover" section
# decided archive identity is a separate migration under its own tool's documentation. Host paths
# are not database identities either.
# A single identity token: a literal name, or one `${VAR:-default}` interpolation. Spelled out
# rather than a loose character class, because a class containing `:` and `$` swallows the
# password of `postgresql://role:${PG_PASSWORD:-...}@host/db` into the role it was reading.
_TOKEN = r"(?:\$\{[^}]+\}|[\w.-]+)"

_IDENTITY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("postgres-db", re.compile(rf"\bPOSTGRES_DB:\s*({_TOKEN})")),
    ("postgres-role", re.compile(rf"\bPOSTGRES_USER:\s*({_TOKEN})")),
    ("postgres-role", re.compile(rf"\b(?:psql|pg_isready|pg_dump)\b[^\n]*?-U\s+({_TOKEN})")),
    ("postgres-db", re.compile(rf"\bpsql\b[^\n]*?-U\s+{_TOKEN}\s+({_TOKEN})")),
    ("postgres-role", re.compile(rf"\bpostgresql(?:\+\w+)?://({_TOKEN}):[^@\s]*@")),
    ("postgres-db", re.compile(rf"\bpostgresql(?:\+\w+)?://[^@\s]+@[^/\s]+/({_TOKEN})")),
    ("compose-project", re.compile(rf"^name:\s*({_TOKEN})")),
)

_DEFAULTED_VALUE = re.compile(r"^\$\{[^:}]+:-([^}]+)\}$")


@dataclass(frozen=True)
class IdentitySite:
    """One line that names a Postgres database, a Postgres role or a Compose project."""

    path: str
    lineno: int
    line: str
    kind: str
    value: str

    def __str__(self) -> str:
        return f"{self.path}:{self.lineno}: {self.kind}={self.value!r} in {self.line.strip()!r}"


def _bare_value(raw: str) -> str:
    """The identity a site actually names: quotes stripped, `${VAR:-default}` reduced."""
    value = raw.strip().strip(",").strip('"').strip("'")
    defaulted = _DEFAULTED_VALUE.match(value)
    return defaulted.group(1) if defaulted else value


def scan_identity_sites(path: str, text: str) -> list[IdentitySite]:
    """Every identity-naming line in ``text``, tagged with ``path`` and its 1-based line number.

    Comment lines are skipped on purpose: the pointer comments this plan adds discuss
    ``POSTGRES_DB`` and the role by name, and a scanner that read prose as configuration would
    report the explanation of a carve-out as a second carve-out.
    """
    sites: list[IdentitySite] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith("#"):
            continue
        for kind, pattern in _IDENTITY_PATTERNS:
            for match in pattern.finditer(line):
                value = _bare_value(match.group(1))
                if not value:
                    continue
                site = IdentitySite(path=path, lineno=lineno, line=line, kind=kind, value=value)
                if site not in sites:
                    sites.append(site)
    return sites


def _validate_enumeration(enumeration: tuple[tuple[str, str, str], ...]) -> None:
    for index, entry in enumerate(enumeration):
        assert len(entry) == 3, (
            f"LIVE_ATTACHED_IDENTITY_SITES[{index}] is not a (path, anchor, reason): {entry!r}"
        )
        path, anchor, reason = entry
        assert path.strip(), f"LIVE_ATTACHED_IDENTITY_SITES[{index}] has an empty path"
        assert anchor.strip(), f"LIVE_ATTACHED_IDENTITY_SITES[{index}] ({path}) has no anchor"
        assert reason.strip(), (
            f"LIVE_ATTACHED_IDENTITY_SITES[{index}] ({path}) carries an empty reason. A site that "
            "keeps the pre-migration identity without a stated reason is indistinguishable from "
            "one nobody noticed; that is the whole defect this enumeration closes."
        )


def _is_moved(site: IdentitySite) -> bool:
    return site.value == _MOVED_IDENTITY or site.value.startswith(f"{_MOVED_IDENTITY}-")


def _is_enumerated(site: IdentitySite, enumeration: tuple[tuple[str, str, str], ...]) -> bool:
    return any(path == site.path and anchor in site.line for path, anchor, _ in enumeration)


def split_identity_sites(
    sites: list[IdentitySite],
    enumeration: tuple[tuple[str, str, str], ...] = LIVE_ATTACHED_IDENTITY_SITES,
) -> tuple[list[IdentitySite], list[IdentitySite], list[IdentitySite]]:
    """``(moved, live_attached, unaccounted)`` for ``sites`` against ``enumeration``."""
    _validate_enumeration(enumeration)
    moved: list[IdentitySite] = []
    live: list[IdentitySite] = []
    unaccounted: list[IdentitySite] = []
    for site in sites:
        if _is_moved(site):
            moved.append(site)
        elif _is_enumerated(site, enumeration):
            live.append(site)
        else:
            unaccounted.append(site)
    return moved, live, unaccounted


def describe_split(
    sites: list[IdentitySite],
    enumeration: tuple[tuple[str, str, str], ...] = LIVE_ATTACHED_IDENTITY_SITES,
) -> str:
    """A one-line report. An empty enumeration is a legal state, reported as such.

    Every identity having moved is the end state this phase works towards, so the report says
    "0 live-attached sites" rather than raising on an empty list.
    """
    moved, live, unaccounted = split_identity_sites(sites, enumeration)
    return (
        f"{len(moved)} moved sites, {len(live)} live-attached sites, "
        f"{len(unaccounted)} unaccounted sites"
    )


def assert_no_third_state(
    sites: list[IdentitySite],
    enumeration: tuple[tuple[str, str, str], ...] = LIVE_ATTACHED_IDENTITY_SITES,
) -> tuple[list[IdentitySite], list[IdentitySite]]:
    """``(moved, live_attached)``, failing with file and line on anything that is neither."""
    moved, live, unaccounted = split_identity_sites(sites, enumeration)
    assert not unaccounted, (
        "identity sites that neither carry the moved name nor appear in "
        "LIVE_ATTACHED_IDENTITY_SITES:\n"
        + "\n".join(f"  {site}" for site in unaccounted)
        + "\nMove the identity, or add the site to the enumeration with the reason it stays."
    )
    return moved, live


def _read_tracked(path: str) -> str:
    target = _REPO_ROOT / path
    assert target.is_file(), f"{path} does not exist; the enumeration names a file that is gone"
    return target.read_text(encoding="utf-8")


def _pointer_comment_above(path: str, anchor: str) -> tuple[int, list[str]]:
    """``(anchor line number, the three raw lines above it)``. Raises if the anchor is gone."""
    lines = _read_tracked(path).splitlines()
    for index, line in enumerate(lines):
        if anchor in line and not line.lstrip().startswith("#"):
            return index + 1, lines[max(0, index - 3) : index]
    raise AssertionError(
        f"{path} contains no line matching the anchor {anchor!r}. The enumeration is the "
        "contract; an anchor matching nothing accounts for nothing while looking like it does."
    )


def test_deploy_yml_identities_are_all_moved() -> None:
    """Behaviour 1: every Postgres identity in the Kamal config, the psql alias included, is moved.

    Read from the file rather than restated, so a new legacy-identity site added anywhere in
    ``config/deploy.yml`` fails here instead of arriving unnoticed.
    """
    path = "config/deploy.yml"
    sites = scan_identity_sites(path, _read_tracked(path))
    assert sites, f"{path} yielded no identity sites; the assertions below would be vacuous"

    moved, live = assert_no_third_state(sites)

    assert {site.value for site in moved} == {_MOVED_IDENTITY}, (
        f"moved sites in {path} name {sorted({s.value for s in moved})}, expected only "
        f"{_MOVED_IDENTITY!r}"
    )
    assert live == [], f"{path} still has live-attached identity sites: {live}"
    # The alias names a role AND a database on one line; both must have moved.
    alias = [site for site in moved if "psql:" in site.line]
    assert {site.kind for site in alias} == {"postgres-role", "postgres-db"}, (
        f"the psql alias should name the moved role and database; the reader saw {alias}"
    )


def test_a_legacy_site_outside_the_enumeration_fails_with_file_and_line(tmp_path: Path) -> None:
    """Behaviour 3: the failing direction, proven on a synthetic file rather than a deployed one.

    The fixture goes through ``scan_identity_sites`` -- the same reader the two assertions above
    use -- so this proves the real contract fails, not a parallel one written to fail.
    """
    fixture = tmp_path / "synthetic-compose.yml"
    fixture.write_text(
        "name: iforge-synthetic\n"
        "services:\n"
        "  postgres:\n"
        "    environment:\n"
        "      POSTGRES_DB: iforge\n"
        "      POSTGRES_USER: clawdi\n",
        encoding="utf-8",
    )

    sites = scan_identity_sites("synthetic-compose.yml", fixture.read_text(encoding="utf-8"))
    assert len(sites) == 3, f"the reader found {sites}"

    with pytest.raises(AssertionError) as raised:
        assert_no_third_state(sites)

    message = str(raised.value)
    assert "synthetic-compose.yml:6" in message, message
    assert "clawdi" in message, message


def test_an_empty_enumeration_is_legal_and_reported_as_zero(tmp_path: Path) -> None:
    """Behaviour 4: every identity moved is the end state, not a crash on an empty list."""
    fixture = tmp_path / "all-moved.yml"
    fixture.write_text(
        "name: iforge-preview\n      POSTGRES_DB: iforge\n      POSTGRES_USER: iforge\n",
        encoding="utf-8",
    )
    sites = scan_identity_sites("all-moved.yml", fixture.read_text(encoding="utf-8"))

    moved, live = assert_no_third_state(sites, enumeration=())
    assert live == []
    assert len(moved) == len(sites) == 3

    assert "0 live-attached sites" in describe_split(sites, enumeration=())
    # The real Kamal config is in the same end state: its psql alias moved with the defaults.
    assert "0 live-attached sites" in describe_split(
        scan_identity_sites("config/deploy.yml", _read_tracked("config/deploy.yml"))
    )

    with pytest.raises(AssertionError, match="empty reason"):
        split_identity_sites(sites, enumeration=(("all-moved.yml", "name:", "   "),))


def test_every_enumerated_site_points_at_the_runbook_that_moves_it() -> None:
    """Behaviour 5: a reader who finds a legacy name learns why without leaving the file.

    The enumeration is empty today; a site added back to it must carry the runbook pointer.
    """
    _validate_enumeration(LIVE_ATTACHED_IDENTITY_SITES)

    for path, anchor, _reason in LIVE_ATTACHED_IDENTITY_SITES:
        lineno, above = _pointer_comment_above(path, anchor)
        pointers = [
            line for line in above if line.lstrip().startswith("#") and _RUNBOOK_FILENAME in line
        ]
        assert pointers, (
            f"{path}:{lineno} keeps the pre-migration identity and carries no comment naming "
            f"{_RUNBOOK_FILENAME} within the three lines above it. The reason lives in this "
            "test file and in an allowlist; a reader of the deployment config sees neither."
        )
