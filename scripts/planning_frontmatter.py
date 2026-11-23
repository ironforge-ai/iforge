"""Read YAML frontmatter from a planning document, without the split('---') trap.

Phase 03.1 (WINDOWS #78) found a verification gate passing VACUOUSLY because it
read frontmatter with ``text.split('---')[1]``. That idiom splits on the
SUBSTRING ``---`` anywhere in the file, so the first three hyphens in the body
end the "frontmatter" -- and a report body that discusses allowlist syntax
naturally contains lines like ``# ---``. Measured on ``03.1-VERIFICATION.md`` at
the time: the naive split returned 13,602 characters of a 31,401-character
frontmatter, under half. The gate then asserted a property of a fragment that
could not contain the block it was checking, and reported success either way.

A gate that cannot see what it is checking is worse than no gate: it converts an
unchecked property into a green tick. The failure is silent by construction,
because a truncated parse looks exactly like a passing one.

So this module does the boring, correct thing:

* the document must OPEN with a ``---`` line -- no leading blank lines, no BOM
  tolerance beyond a UTF-8 signature, because a file that does not start with
  the delimiter has no frontmatter and should say so rather than guess;
* the frontmatter ends at the first line that is EXACTLY ``---`` (after
  stripping trailing whitespace). ``# ---``, ``--- x``, ``----`` and an indented
  ``  ---`` are body text, not terminators;
* the result is parsed with ``yaml.safe_load`` and must be a mapping;
* every failure raises. Nothing here returns a partial parse, because the whole
  point is that a partial parse is indistinguishable from a real one.

Usage from a guard or an ``<automated>`` verify command::

    from planning_frontmatter import read_frontmatter
    fm = read_frontmatter(".planning/phases/<phase>/<n>-VERIFICATION.md")
    assert fm["status"] == "passed", fm["status"]

or, when the document is already in hand::

    from planning_frontmatter import parse_frontmatter
    fm = parse_frontmatter(text)

Those are two functions on purpose (WR-08). ``read_frontmatter`` used to take one
parameter and treat it as a path when a file existed there and as document text
otherwise, constructing ``Path(source)`` from arbitrary multi-line text and
catching the failure. It worked, but a module whose entire thesis is that
guessing at a document's structure is the defect should not guess at what its own
argument is: the failure mode is a caller who passes text that happens to name a
file, and gets a silent parse of the wrong document -- the same silent-wrong-answer
class one layer up.

WHO CONSUMES THIS. ``backend/tests/test_planning_frontmatter.py`` pins the parser,
and its ``automated``-block ratchet is what stops the substring split being
re-invented: it enumerates every ``<automated>`` block under ``.planning/`` and
fails on a new one that reads frontmatter by splitting on ``---``. Before that
ratchet existed this module had no consumer at all -- the defect surface it was
written for was unguarded, and the next such block could re-invent the idiom with
nothing to stop it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

__all__ = [
    "FrontmatterError",
    "parse_frontmatter",
    "read_frontmatter",
    "split_frontmatter",
]

DELIMITER = "---"


class FrontmatterError(ValueError):
    """Raised when a document has no readable YAML frontmatter.

    Deliberately a hard error rather than an empty dict: the defect this module
    exists to prevent is a gate that silently reads nothing and passes.
    """


def _is_delimiter(line: str) -> bool:
    """True only for a line that is exactly ``---``.

    ``rstrip`` covers a trailing newline and stray trailing spaces. Anything
    else on the line -- a leading ``#``, an indent, a fourth hyphen, a comment
    after it -- makes it body text. This is the single rule the substring split
    got wrong.
    """
    return line.rstrip() == DELIMITER


def split_frontmatter(text: str) -> tuple[str, str]:
    """Return ``(frontmatter_text, body_text)``; raise if there is no frontmatter."""
    if text.startswith("﻿"):
        text = text[1:]

    lines = text.splitlines(keepends=True)
    if not lines or not _is_delimiter(lines[0]):
        raise FrontmatterError(
            "document does not open with a '---' delimiter line, so it has no "
            "frontmatter to read"
        )

    for index in range(1, len(lines)):
        if _is_delimiter(lines[index]):
            return "".join(lines[1:index]), "".join(lines[index + 1 :])

    raise FrontmatterError(
        "frontmatter opened with '---' but never closed: no later line is exactly "
        "'---'. Refusing to treat the rest of the document as frontmatter."
    )


def read_frontmatter(path: str | Path) -> dict[str, Any]:
    """Parse the YAML frontmatter of the document at ``path``.

    ``path`` is always a path. It is never inspected to decide whether it might
    be document text instead -- pass text to :func:`parse_frontmatter`.
    """
    return parse_frontmatter(Path(path).read_text(encoding="utf-8"))


def parse_frontmatter(text: str) -> dict[str, Any]:
    """Parse the YAML frontmatter of a document already in memory."""
    raw, _body = split_frontmatter(text)
    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError as error:
        raise FrontmatterError(f"frontmatter is not valid YAML: {error}") from error

    if parsed is None:
        raise FrontmatterError("frontmatter is empty")
    if not isinstance(parsed, dict):
        raise FrontmatterError(
            f"frontmatter parsed as {type(parsed).__name__}, expected a mapping"
        )
    return parsed
