"""Shared primitives for the prose doc-contract suites.

Four test modules assert over the same repository's markdown: the managed-runtime contract,
ADR 0005, the Hetzner terms position and the infrastructure layout. Each one previously carried
its own byte-identical ``_REPO_ROOT`` and ``_normalized_text``. That duplication is the exact
failure mode these suites exist to catch in prose -- one copy gets corrected and the others
silently keep the old meaning -- so the definitions live here and are imported everywhere.

``normalized_text`` is the single definition of what "the same claim" means for every prose
assertion in this repository. Change it here and all four suites change together.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).parents[2]

# A claim about Hetzner's published terms that cannot be supported by quotation. It belongs to
# ``docs/hetzner-terms-position.md`` -- the document that owns the vendor-terms question -- and is
# defined here so a sixth phrasing is added once rather than per consuming suite. The ownership
# ADR does not discuss vendor terms and deliberately does not consume this tuple.
FORBIDDEN_TRADING_CLAIMS = (
    "hetzner prohibits trading",
    "the terms prohibit trading",
    "trading is prohibited",
    "prohibition on trading",
    "the published terms prohibit trading",
)


def normalized_text(path: Path) -> str:
    """Casefolded, whitespace-collapsed, apostrophe-folded text of a markdown document.

    Every asserted substring must therefore itself be lowercase, single-spaced and written with
    an ASCII apostrophe, and claims survive prose reflow.

    Typographic apostrophes (U+2019, and U+2018 where a source uses it as an apostrophe) fold to
    ASCII ``'``. Without this, a clause quoted verbatim from a primary source that renders the
    possessive typographically -- as Hetzner's Terms do in Version 2.0.0 §8.3 and §8.4 -- can
    never be matched by an assertion typed on a keyboard, so the assertion silently matches some
    other paraphrase in the same file instead of the quotation it names.
    """
    raw = path.read_text(encoding="utf-8").replace("’", "'").replace("‘", "'")
    return " ".join(raw.casefold().split())
