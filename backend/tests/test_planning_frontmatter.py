"""Pin ``scripts/planning_frontmatter.py`` against the vacuous-gate defect.

Phase 03.1 (WINDOWS #78) found a verification gate that read frontmatter with
``text.split('---')[1]`` and therefore asserted on a 13,602-character fragment
of a 31,401-character frontmatter. The assertion targeted a key the fragment
could not contain, so the gate passed without checking anything.

The tests that matter here are the ones that FAIL against the old idiom: if a
regression reintroduced the substring split, ``test_a_delimiter_inside_the_
frontmatter_does_not_truncate_it`` goes red. The rest pin the error contract,
because the whole defect class is "returned something plausible instead of
raising".

These are fixture-free so they run under a bare importlib harness as well as
pytest (phase 03.1 relied on that when ``python3 -m pytest`` could not collect;
note ``cd backend && uv run pytest`` works and is the supported invocation).
"""

from __future__ import annotations

import importlib.util
import re
import sys
from types import ModuleType

import pytest

from tests.doc_contract import REPO_ROOT as _REPO_ROOT

_SCRIPTS = _REPO_ROOT / "scripts"


def _load(name: str) -> ModuleType:
    path = _SCRIPTS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_planning_{name}", path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_fm = _load("planning_frontmatter")

# A frontmatter that discusses allowlist syntax, so it legitimately contains a
# line reading `# ---`. This is not contrived: it is the shape of the phase 03.1
# verification report, whose gaps described the INTERIM tripwire evasion.
_TRAP = (
    "---\n"
    "status: gaps_found\n"
    "gaps:\n"
    '  - truth: "the tripwire is defeated by a blank line then a bare marker"\n'
    "    harden: |\n"
    "      A section reset looks like:\n"
    "      # ---\n"
    "      and the marker above it is forgotten.\n"
    "overrides_applied: 3\n"
    "---\n"
    "# Report body\n"
)


def test_a_delimiter_inside_the_frontmatter_does_not_truncate_it() -> None:
    """The regression guard. Red against ``split('---')[1]``, green against a real parser.

    ``overrides_applied`` sits AFTER the ``# ---`` line, so the substring split
    cannot see it while a line-wise parser can. A gate asserting on that key
    reads a fragment that cannot contain it and passes vacuously.
    """
    naive = _TRAP.split("---")[1]
    assert "overrides_applied" not in naive, (
        "the trap document no longer reproduces the truncation it exists to pin; "
        "adjust the fixture rather than deleting this test"
    )

    parsed = _fm.parse_frontmatter(_TRAP)
    assert parsed["overrides_applied"] == 3
    assert parsed["status"] == "gaps_found"
    assert len(parsed["gaps"]) == 1


def test_the_body_is_not_swallowed_into_the_frontmatter() -> None:
    raw, body = _fm.split_frontmatter(_TRAP)
    assert "# Report body" in body
    assert "# Report body" not in raw


@pytest.mark.parametrize(
    "line",
    ["# ---", "  ---", "----", "--- trailing", "-- -"],
    ids=["comment", "indented", "four-hyphens", "trailing-text", "spaced"],
)
def test_only_an_exact_three_hyphen_line_closes_the_frontmatter(line: str) -> None:
    """Anything but a bare ``---`` is body text, not a terminator.

    Asserted at the ``split_frontmatter`` layer deliberately. Delimiter
    detection is a line-shape question, and most of these lines are not valid
    YAML as bare mapping entries -- routing them through ``read_frontmatter``
    would test the YAML parser's error handling instead of the boundary rule.
    """
    doc = f"---\na: 1\n{line}\nsentinel: kept\n---\nbody\n"
    raw, body = _fm.split_frontmatter(doc)
    assert line in raw, f"{line!r} was treated as a closing delimiter"
    assert "sentinel: kept" in raw, (
        f"{line!r} truncated the frontmatter -- content after it was lost"
    )
    assert body.strip() == "body"


def test_a_trailing_space_after_the_delimiter_still_closes_it() -> None:
    """``---`` with trailing whitespace is a delimiter; editors add it silently."""
    assert _fm.parse_frontmatter("---\na: 1\n--- \nbody\n") == {"a": 1}


@pytest.mark.parametrize(
    ("label", "document"),
    [
        ("no opening delimiter", "a: 1\n---\nbody\n"),
        ("never closed", "---\na: 1\nno terminator\n"),
        ("not a mapping", "---\n- one\n- two\n---\n"),
        ("empty frontmatter", "---\n---\nbody\n"),
        ("invalid yaml", "---\na: [unclosed\n---\nbody\n"),
    ],
)
def test_every_unreadable_document_raises_rather_than_returning_a_fragment(
    label: str, document: str
) -> None:
    """The defect class is silent success; nothing here may return a partial parse."""
    with pytest.raises(_fm.FrontmatterError):
        _fm.parse_frontmatter(document)


# ---------------------------------------------------------------------------
# The consumer this module did not have (WR-08).
# ---------------------------------------------------------------------------
#
# `git grep planning_frontmatter -- ':!.planning/'` used to return only the module and this
# file. Nothing imported it, so the 115-line helper's only user was its own test, and the
# actual defect surface -- `<automated>` verify blocks that read frontmatter with
# `text.split('---')[1]` -- was unchanged and unguarded. The next such block could re-invent
# the idiom with nothing to stop it, which is the state WINDOWS #78 was filed from.
#
# The ratchet below is that guard. It reads every `<automated>` block under `.planning/` and
# refuses a NEW one that splits on the delimiter. It is a ratchet rather than a hard zero
# because three such blocks already exist, in an executed plan: rewriting an executed plan's
# recorded commands is not a repair, it is falsifying the record of what was run.

_AUTOMATED_BLOCK = re.compile(r"<automated>(.*?)</automated>", re.S)
_SUBSTRING_SPLIT_IDIOM = re.compile(r"""\.split\(\s*(['"])-{3}\1\s*\)""")

# Measured, not remembered: `python3 - <<PY` over `.planning/**/*.md` at this commit.
# 301 `<automated>` blocks; exactly three carry the idiom, all in the plan that was executing
# when WINDOWS #78 was found. A file is listed rather than a count so a NEW occurrence in an
# already-listed file is caught too.
_KNOWN_SPLIT_BLOCKS: dict[str, int] = {
    ".planning/phases/03.1-clawdi-refactor-rebrand-to-iforge/03.1-15-PLAN.md": 3,
}
_AUTOMATED_BLOCK_FLOOR = 200


def _automated_blocks() -> dict[str, list[str]]:
    """Every ``<automated>`` block under ``.planning/``, by repository-relative path."""
    blocks: dict[str, list[str]] = {}
    for document in sorted((_REPO_ROOT / ".planning").rglob("*.md")):
        text = document.read_text(encoding="utf-8", errors="replace")
        found = _AUTOMATED_BLOCK.findall(text)
        if found:
            blocks[document.relative_to(_REPO_ROOT).as_posix()] = found
    return blocks


def test_the_automated_block_scan_is_not_vacuous() -> None:
    """A ratchet that reads nothing reports a clean result it did not establish.

    The floor is what stops the assertion below passing because the block regex stopped
    matching -- the same silent-pass class the module itself exists to prevent.
    """
    total = sum(len(found) for found in _automated_blocks().values())

    assert total >= _AUTOMATED_BLOCK_FLOOR, (
        f"only {total} <automated> blocks found under .planning/; the scan is not reading "
        "what it claims to read"
    )


def test_no_new_automated_block_reads_frontmatter_by_splitting_on_the_delimiter() -> None:
    """The ratchet. `scripts/planning_frontmatter.py` exists so this idiom is not re-invented.

    Subset rather than equality on purpose: `/gsd-cleanup` archives completed phase
    directories, so the listed plan can legitimately disappear. Removing an occurrence must
    never fail; adding one must.
    """
    offenders = {
        path: sum(1 for block in found if _SUBSTRING_SPLIT_IDIOM.search(block))
        for path, found in _automated_blocks().items()
    }
    offenders = {path: count for path, count in offenders.items() if count}

    unexpected = {
        path: count for path, count in offenders.items() if count > _KNOWN_SPLIT_BLOCKS.get(path, 0)
    }

    assert unexpected == {}, (
        "an <automated> block reads frontmatter with text.split('---'), which truncates at "
        "the first three hyphens ANYWHERE in the document and makes the gate assert on a "
        "fragment that cannot contain what it is checking (WINDOWS #78). Use "
        "scripts/planning_frontmatter.read_frontmatter instead: "
        f"{unexpected}"
    )


def test_every_document_a_pinned_split_block_reads_parses_with_the_real_parser() -> None:
    """The listed blocks are grandfathered, but their INPUTS are still checked properly.

    This is what makes the pin evidence rather than an amnesty: each document those three
    gates read is parsed here with the correct parser, so if one of them ever grows a body
    that truncates the split, this test says so with the real frontmatter in hand.
    """
    read_paths: set[str] = set()
    for path, count in _KNOWN_SPLIT_BLOCKS.items():
        document = _REPO_ROOT / path
        if not document.exists():
            continue  # archived by /gsd-cleanup; the ratchet above already allows this.
        blocks = [
            block
            for block in _AUTOMATED_BLOCK.findall(
                document.read_text(encoding="utf-8", errors="replace")
            )
            if _SUBSTRING_SPLIT_IDIOM.search(block)
        ]
        assert len(blocks) == count, f"{path} no longer carries {count} pinned blocks"
        for block in blocks:
            read_paths.update(re.findall(r"open\(\s*'([^']+\.md)'", block))

    if not read_paths:
        pytest.skip("the pinned plan has been archived; nothing left to cross-check")

    for relative in sorted(read_paths):
        document = _REPO_ROOT / relative
        assert document.exists(), f"a pinned gate reads {relative}, which does not exist"
        parsed = _fm.read_frontmatter(document)
        assert isinstance(parsed, dict) and parsed, f"{relative} parsed to nothing"


def test_the_live_verification_report_parses_in_full() -> None:
    """The real artifact the defect was found in, read end to end."""
    reports = sorted((_REPO_ROOT / ".planning" / "phases").glob("*/[0-9]*-VERIFICATION.md"))
    if not reports:
        pytest.skip("no VERIFICATION.md in .planning/phases yet")

    for report in reports:
        parsed = _fm.read_frontmatter(report)
        assert isinstance(parsed, dict) and parsed, f"{report} parsed to nothing"
        assert "phase" in parsed, f"{report} frontmatter has no phase key"
