"""Tell a stale document from an accurate one, mechanically.

Phase 03.3 moved some identities and deliberately left others where they were, and the
documentation under ``docs/`` carried both kinds of name in the same sentences. A sweep that
treated every pre-rename spelling as residue would have rewritten nine true statements about
units that are running on provisioned hosts right now; a sweep that treated none of them as
residue left forty sites telling a reader their CLI state lives in a directory
``packages/cli/src/lib/state-root.ts`` no longer resolves. Neither failure is visible in a diff.

So this module makes the distinction data rather than judgement. Three tuples carry it:

* ``IDENTITIES_THAT_MOVED`` -- ``(legacy_spelling, source_of_truth_path, current_spelling)``.
  The current spelling is READ from the source file at test time by regex over the constant's
  own declaration. A test that hardcoded both sides could not notice the code moving underneath
  it, which is precisely the drift these prose suites exist to catch.
* ``PRE_RENAME_SPELLINGS_THE_DOC_MUST_STILL_NAME`` -- ``(spelling, reason)`` for the three
  hosted system-scope units and the unit naming rule. These must still be PRESENT; the suite
  fails if a future sweep removes them, and fails if an entry states no reason. They were
  CURRENT when 03.3-20 wrote this tuple; ``b04e4160`` then moved all four and left them here
  unchanged, so for a while a stale document and a stale guard agreed with each other and
  nothing was red. They stay -- as PRE-RENAME spellings a document must still name, because an
  operator on a host provisioned before 2026-09-11 needs the exact filenames running in front of
  them -- but the claim they encode is no longer "these are current".
* ``DELIBERATE_LEGACY_REFERENCES`` -- ``(path, reason)``, PARSED from
  ``config/docs-legacy-root-references.txt`` rather than declared here. The gate command in plan
  03.3-20 reads the same file, so the expected survivors are one authority consulted by both. A
  tuple restated in this module would be a second copy that agrees today and drifts silently, so
  no path string and no reason string for that set is written into this file.

FENCED CODE BLOCKS ARE NOT SKIPPED, and the decision is deliberate rather than incidental.
``docs/agent-docs-guide.md`` makes these documents command-first: "Use one executable example per
convention", and every task-shaped section must carry a runnable check. The fenced blocks in
``docs/`` are therefore the lines an operator PASTES -- ``mkdir -p ...``, ``cat ...``,
``systemctl ...`` -- which makes a stale identity inside a fence the most harmful place for one
to survive, not an exempt one. Skipping fences would blind this guard to exactly the sites whose
failure mode is a command run against the wrong directory or the wrong unit.

Findings are reported as ``(path, line, column, identity)`` and sorted by that tuple, so two runs
over the same tree produce byte-identical output. ``column`` is 1-based and measured over the
DECODED string: a line whose prose uses an em dash or a diaeresis before the match reports the
character offset a reader counts, not the byte offset UTF-8 happens to produce.

Shape follows ``test_readme_identity_contract.py``: repository root from ``tests.doc_contract``,
raw-line parsing wherever line numbers matter, a loud failure on an enumeration that yields
nothing, and a collector fed a ``Path`` so the synthetic fixtures below travel the same read,
parse and sort path as the real tree.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.doc_contract import REPO_ROOT as _REPO_ROOT
from tests.doc_contract import normalized_text as _normalized_text

_DOCS_ROOT = _REPO_ROOT / "docs"
_MANAGED_RUNTIME = _DOCS_ROOT / "managed-runtime.md"
_LEGACY_REFERENCES_FILE = _REPO_ROOT / "config" / "docs-legacy-root-references.txt"

_STATE_ROOT_TS = _REPO_ROOT / "packages" / "cli" / "src" / "lib" / "state-root.ts"
_REGISTRY_TS = _REPO_ROOT / "packages" / "cli" / "src" / "adapters" / "registry.ts"
_SYSTEMD_USER_TS = _REPO_ROOT / "packages" / "cli" / "src" / "runtime" / "systemd-user.ts"

# Each pattern anchors on the DECLARATION of the constant it reads, not on its value. Anchoring
# on the value would be the hardcoding this module refuses one indirection further along.
_STATE_DIR_NAME = re.compile(r'^const STATE_DIR_NAME = "([^"]+)";', re.MULTILINE)
_SYSTEMD_OWNED_PREFIX = re.compile(
    r'^export const RUNTIME_SYSTEMD_OWNED_NAME_PREFIX = "([^"]+)";',
    re.MULTILINE,
)
# The function NAME is part of the anchor, so only `builtinSkillTargetDir`'s own argument is read.
# `[^}]*?` cannot cross the brace that closes the function, which keeps the capture inside the body.
_BUILTIN_SKILL_ID = re.compile(
    r"export function builtinSkillTargetDir\b[^}]*?"
    r'agentSkillTargetDir\(\s*agentType,\s*"([^"]+)"',
    re.DOTALL,
)


class DocsContractError(RuntimeError):
    """The contract could not be established, as distinct from being violated."""


def _read_constant(path: Path, pattern: re.Pattern[str]) -> str:
    """The single captured group of ``pattern`` in ``path``, or a loud failure."""
    if not path.exists():
        raise DocsContractError(f"{path} is missing -- the authority this suite reads is gone")
    match = pattern.search(path.read_text(encoding="utf-8"))
    if not match:
        raise DocsContractError(
            f"{path.name} no longer matches {pattern.pattern!r} -- the parser has drifted from "
            "the declaration and every comparison below would compare against nothing"
        )
    return match.group(1)


# `(legacy_spelling, source_of_truth_path, format_template)`. The template composes the captured
# constant into the spelling a DOCUMENT writes, which is not always the constant itself: the
# state root is a `~/`-prefixed path, the Files unit appends `files.service` to a prefix, and the
# bundled skill is named inside backticks in prose.
_MOVED_IDENTITY_SOURCES: tuple[tuple[str, Path, re.Pattern[str], str], ...] = (
    ("~/.clawdi", _STATE_ROOT_TS, _STATE_DIR_NAME, "~/{}"),
    ("clawdi-files.service", _SYSTEMD_USER_TS, _SYSTEMD_OWNED_PREFIX, "{}files.service"),
    ("bundled `clawdi`", _REGISTRY_TS, _BUILTIN_SKILL_ID, "bundled `{}`"),
)

IDENTITIES_THAT_MOVED: tuple[tuple[str, Path, str], ...] = tuple(
    (legacy, source, template.format(_read_constant(source, pattern)))
    for legacy, source, pattern, template in _MOVED_IDENTITY_SOURCES
)

# `(spelling, reason)` for every pre-rename spelling `docs/managed-runtime.md` must still NAME.
# Exactly four: the three hosted system-scope units, plus the naming rule that describes them.
# These are asserted PRESENT, not absent. A sweep that deleted them would hand an operator on a
# pre-rename host a systemctl line naming a unit that is not on the box in front of them.
#
# WHAT CHANGED, and why the tuple survived it. When 03.3-20 wrote this, all four were CURRENT:
# the P33-21 carve-out held the three system-scope units at the `clawdi-` prefix while the rest
# of the platform moved. `b04e4160` ("move the platform identities to iforge, with legacy
# discovery") then reversed that carve-out -- `RUNTIME_SYSTEM_UNIT_WATCH` and its two siblings
# are the `iforge-` spellings now, with the `clawdi-` triple demoted to
# `LEGACY_RUNTIME_SYSTEM_UNIT_NAMES`, read-only and collect-only -- but did not touch this
# tuple. Nothing went red, because `docs/managed-runtime.md` was equally stale: a stale document
# and a stale guard agreed with each other, which is the precise failure mode this module
# exists to catch. 04-07 corrected the document, which is what finally made the disagreement
# visible.
#
# So the entries stay and the CLAIM changes: these are pre-rename spellings a document must
# still name AS PRE-RENAME, not identities that are current.
# `test_iforge_companion_docs_contract.py` holds the other half -- it forbids the specific
# sentences that asserted these spellings as CURRENT, phrase by phrase rather than by bare name,
# so the two suites constrain the same document without pulling against each other.
PRE_RENAME_SPELLINGS_THE_DOC_MUST_STILL_NAME: tuple[tuple[str, str], ...] = (
    (
        "clawdi-*",
        "the `clawdi-*` naming rule, named as UPSTREAM's prefix: ownership is decided by the "
        "`iforge-` prefix or iforge's own header, never by `clawdi-`, so a `clawdi-` entry on a "
        "shared machine is another product's install.",
    ),
)

# 06.1-04 (user directive 2026-09-30, no Clawdi-name aliases): the three pre-rename hosted unit
# FILENAMES used to be required here, because the reconciler collected them through
# LEGACY_RUNTIME_SYSTEM_UNIT_NAMES. Those readers are gone -- there is no production host from
# before the move -- so the document must no longer name them at all.
PRE_RENAME_UNIT_FILENAMES_THE_DOC_MUST_NOT_NAME: tuple[str, ...] = (
    "clawdi-runtime-watch",
    "clawdi-daemon",
    "clawdi-runtime-sidecar",
)
_EXPECTED_PRE_RENAME_SPELLING_ENTRIES = 1


def parse_deliberate_legacy_references(
    path: Path = _LEGACY_REFERENCES_FILE,
) -> tuple[tuple[str, str], ...]:
    """``(path, reason)`` for every document allowed to retain a legacy state-root reference.

    The grammar is the one the file documents and ``config/rebrand-allowlist.txt`` established:
    an empty line, or a line whose first non-space character is ``#``, is skipped; anything else
    is one repository-relative path. The reason is the comment RUN immediately above the entry,
    and a blank line ends a run -- which is what keeps the file's header from being read as a
    reason for the first path beneath it.

    The association logic deliberately mirrors ``load_allowlist`` in
    ``scripts/check_rebrand_residual.py`` rather than inventing a second shape for the same job.
    """
    if not path.exists():
        raise DocsContractError(
            f"{path} is missing -- the survivor set is data this suite reads, not a judgement "
            "recorded in a plan summary"
        )
    entries: list[tuple[str, str]] = []
    run: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            run = []
            continue
        if line.startswith("#"):
            run.append(line.lstrip("#").strip())
            continue
        reason = " ".join(part for part in run if part).strip()
        if not reason:
            raise DocsContractError(
                f"{line!r} is fenced by {path.name} with no reason comment above it -- a fence "
                "nobody justified is indistinguishable from a sweep somebody abandoned"
            )
        entries.append((line, reason))
        run = []
    return tuple(entries)


DELIBERATE_LEGACY_REFERENCES: tuple[tuple[str, str], ...] = parse_deliberate_legacy_references()


def _exempt_paths() -> frozenset[str]:
    return frozenset(path for path, _reason in DELIBERATE_LEGACY_REFERENCES)


def documents(root: Path = _DOCS_ROOT) -> tuple[Path, ...]:
    """Every markdown document under ``root``, in a stable order."""
    found = tuple(sorted(root.rglob("*.md")))
    if not found:
        raise DocsContractError(f"no markdown documents were found under {root}")
    return found


def collect_moved_identities(
    path: Path,
    root: Path | None = None,
    exempt: frozenset[str] = frozenset(),
) -> tuple[tuple[str, int, int, str], ...]:
    """Every site in ``path`` naming an identity that has moved.

    Returns ``(relative_path, line, column, legacy_spelling)`` tuples sorted by that order.
    ``column`` is 1-based over the decoded string. Fenced code blocks are NOT skipped; see the
    module docstring for why. Every occurrence on a line is reported, so a second identity on the
    same line is never shadowed by the first.
    """
    base = root if root is not None else _REPO_ROOT
    try:
        relative = str(path.relative_to(base))
    except ValueError:
        relative = path.name
    if relative in exempt:
        return ()
    findings: list[tuple[str, int, int, str]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        for legacy, _source, _current in IDENTITIES_THAT_MOVED:
            start = line.find(legacy)
            while start != -1:
                findings.append((relative, number, start + 1, legacy))
                start = line.find(legacy, start + 1)
    return tuple(sorted(findings))


def collect_over_tree(
    root: Path = _DOCS_ROOT,
    base: Path | None = None,
) -> tuple[tuple[str, int, int, str], ...]:
    """Findings across every document under ``root``, sorted, with the allow-list applied."""
    exempt = _exempt_paths()
    findings: list[tuple[str, int, int, str]] = []
    for document in documents(root):
        findings.extend(collect_moved_identities(document, root=base, exempt=exempt))
    return tuple(sorted(findings))


def resolve_pre_rename_spellings(
    entries: tuple[tuple[str, str], ...] = PRE_RENAME_SPELLINGS_THE_DOC_MUST_STILL_NAME,
) -> tuple[str, ...]:
    """The spellings that stay, refusing any entry that states no reason."""
    spellings: list[str] = []
    for spelling, reason in entries:
        if not reason.strip():
            raise DocsContractError(
                f"{spelling!r} is kept with no reason -- an exemption nobody justified is "
                "indistinguishable from an oversight, which is the shape "
                "config/rebrand-allowlist.txt refuses for the same class of fence"
            )
        spellings.append(spelling)
    return tuple(spellings)


def _render(findings: tuple[tuple[str, int, int, str], ...]) -> str:
    return "\n".join(
        f"{path}:{line}:{column}: {identity}" for path, line, column, identity in findings
    )


def test_no_document_names_a_moved_identity_outside_the_allow_list() -> None:
    assert IDENTITIES_THAT_MOVED, "no moved identities were resolved from their authorities"
    findings = collect_over_tree()
    assert not findings, "documents under docs/ name identities that have moved:\n" + _render(
        findings
    )


def test_the_moved_identities_resolve_to_the_spelling_the_code_uses() -> None:
    current = {legacy: spelling for legacy, _source, spelling in IDENTITIES_THAT_MOVED}
    assert len(current) == len(_MOVED_IDENTITY_SOURCES)
    for legacy, spelling in current.items():
        assert spelling, f"{legacy} resolved to an empty current spelling"
        assert spelling != legacy, (
            f"{legacy} resolved to itself -- the source of truth has not moved, so this identity "
            "does not belong in IDENTITIES_THAT_MOVED"
        )


def test_a_synthetic_allow_list_is_parsed_into_path_and_reason(tmp_path: Path) -> None:
    fixture = tmp_path / "references.txt"
    fixture.write_text(
        "# Header prose that belongs to no entry.\n"
        "\n"
        "# :12 -- cites the fenced site and paraphrases it\n"
        "docs/example/one.md\n",
        encoding="utf-8",
    )

    parsed = parse_deliberate_legacy_references(fixture)

    assert len(parsed) == 1
    entry_path, reason = parsed[0]
    assert entry_path == "docs/example/one.md"
    assert "cites the fenced site" in reason
    assert "Header prose" not in reason


def test_a_synthetic_allow_list_entry_without_a_reason_is_refused(tmp_path: Path) -> None:
    fixture = tmp_path / "references.txt"
    fixture.write_text(
        "# Header prose, then a blank line, then a bare path.\n\ndocs/example/unjustified.md\n",
        encoding="utf-8",
    )

    with pytest.raises(DocsContractError) as raised:
        parse_deliberate_legacy_references(fixture)

    assert "docs/example/unjustified.md" in str(raised.value)


def test_every_allow_listed_path_actually_carries_a_legacy_reference() -> None:
    # Empty since 06.1-04: no document may keep a legacy state-root reference (user directive
    # 2026-09-30). The parser itself is proven by the synthetic-fixture tests above, so an empty
    # real allow-list is a measurement, not a drifted parser.
    legacy_spellings = tuple(legacy for legacy, _source, _current in IDENTITIES_THAT_MOVED)
    for entry_path, reason in DELIBERATE_LEGACY_REFERENCES:
        document = _REPO_ROOT / entry_path
        assert document.exists(), f"{entry_path} is fenced by the allow-list but does not exist"
        assert reason.strip(), f"{entry_path} is fenced with no reason"
        text = document.read_text(encoding="utf-8")
        assert any(legacy in text for legacy in legacy_spellings), (
            f"{entry_path} is fenced by the allow-list but carries no legacy reference -- a "
            "fence over nothing decays into cover for a real regression"
        )


def test_the_pre_rename_spellings_are_still_named() -> None:
    spellings = resolve_pre_rename_spellings()
    assert len(spellings) == _EXPECTED_PRE_RENAME_SPELLING_ENTRIES
    text = _MANAGED_RUNTIME.read_text(encoding="utf-8")
    missing = [spelling for spelling in spellings if spelling not in text]
    assert not missing, (
        f"docs/managed-runtime.md no longer names {missing} -- the upstream prefix rule an "
        "operator must know is another product's, never reclaimed by iforge."
    )
    carve_out = _normalized_text(_MANAGED_RUNTIME)
    assert "p33-21" in carve_out, (
        "the carve-out paragraph citing P33-21 is gone, leaving the surviving pre-rename names "
        "unexplained"
    )


def test_the_pre_rename_unit_filenames_are_no_longer_named() -> None:
    text = _MANAGED_RUNTIME.read_text(encoding="utf-8")
    named = [name for name in PRE_RENAME_UNIT_FILENAMES_THE_DOC_MUST_NOT_NAME if name in text]
    assert named == [], (
        f"docs/managed-runtime.md still names pre-rename hosted units {named}; iforge keeps no "
        "alias for them (06.1-04, user directive 2026-09-30)"
    )


def test_a_kept_identity_with_an_empty_reason_is_refused() -> None:
    with pytest.raises(DocsContractError):
        resolve_pre_rename_spellings((("clawdi-daemon", "   "),))

    assert resolve_pre_rename_spellings((("clawdi-daemon", "a stated reason"),)) == (
        "clawdi-daemon",
    )


def test_a_synthetic_stale_document_is_reported_with_path_line_and_identity(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "stale.md"
    fixture.write_text(
        "# Setup\n\n```bash\nmkdir -p ~/.clawdi/environments\n```\n",
        encoding="utf-8",
    )

    findings = collect_moved_identities(fixture, root=tmp_path)

    assert len(findings) == 1
    assert findings[0] == ("stale.md", 4, 10, "~/.clawdi")


def test_a_synthetic_hosted_unit_document_produces_no_finding(tmp_path: Path) -> None:
    fixture = tmp_path / "hosted.md"
    fixture.write_text(
        "Support units use `clawdi-*` names.\n"
        "`clawdi-runtime-watch.service` and `clawdi-daemon.service` are hosted-only.\n"
        "`clawdi-runtime-sidecar.service` carries the egress program.\n",
        encoding="utf-8",
    )

    findings = collect_moved_identities(fixture, root=tmp_path)

    assert findings == ()


def test_a_synthetic_clean_document_contributes_zero_findings(tmp_path: Path) -> None:
    fixture = tmp_path / "clean.md"
    fixture.write_text(
        "# Nothing retired here\n"
        "\n"
        "State lives under `~/.iforge`, and the Files unit is `iforge-files.service`.\n",
        encoding="utf-8",
    )

    findings = collect_moved_identities(fixture, root=tmp_path)

    assert findings == ()
    assert len(findings) == 0


def test_two_synthetic_identities_on_one_line_are_both_reported(tmp_path: Path) -> None:
    fixture = tmp_path / "adjacent.md"
    fixture.write_text(
        "The bundled `clawdi` skill is copied into ~/.clawdi/skills/.\n",
        encoding="utf-8",
    )

    findings = collect_moved_identities(fixture, root=tmp_path)

    assert len(findings) == 2
    assert {identity for _path, _line, _column, identity in findings} == {
        "~/.clawdi",
        "bundled `clawdi`",
    }
    assert {line for _path, line, _column, _identity in findings} == {1}
    columns = sorted(column for _path, _line, column, _identity in findings)
    assert columns[0] != columns[1], "the second identity was shadowed by the first"


def test_the_collector_is_deterministic_across_two_synthetic_runs(tmp_path: Path) -> None:
    fixture = tmp_path / "repeat.md"
    fixture.write_text(
        "~/.clawdi and `clawdi-files.service`\nthe bundled `clawdi` skill\n~/.clawdi again\n",
        encoding="utf-8",
    )

    first = collect_moved_identities(fixture, root=tmp_path)
    second = collect_moved_identities(fixture, root=tmp_path)

    assert first == second
    assert first == tuple(sorted(first))


def test_a_synthetic_non_ascii_line_reports_the_decoded_offset(tmp_path: Path) -> None:
    fixture = tmp_path / "unicode.md"
    line = "Naïve — ~/.clawdi\n"
    fixture.write_text(line, encoding="utf-8")

    findings = collect_moved_identities(fixture, root=tmp_path)

    assert len(findings) == 1
    _path, number, column, identity = findings[0]
    assert number == 1
    assert identity == "~/.clawdi"
    assert column == line.index("~/.clawdi") + 1
    assert column < len(line.encode("utf-8")) - len("~/.clawdi")
