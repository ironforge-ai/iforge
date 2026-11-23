"""The untrusted return leg: strip, escape, delimit, datamark, and disclose (TOOL-06).

Companion bytes are read by an LLM that cannot tell content from instruction on its own. Four
defences, all cheap, none sufficient alone:

**Strip.** Every character whose Unicode category is ``Cf`` (format) is dropped, and every ``Cc``
(control) except newline and tab. That is the family that carries bidirectional overrides
(U+202E), zero-width joiners and spaces, byte-order marks, soft hyphens and ANSI escape
introducers -- characters that render as nothing while changing what a human reviewer sees, or
that a terminal interprets. Newline and tab survive because a directory listing legitimately
contains them and stripping them would mangle content rather than defuse it.

**Escape, do not allowlist.** HTML and XML-shaped markup in the body is ESCAPED, never filtered
through an allowlist sanitizer. This is the inversion worth stating plainly: an allowlist
sanitizer's job is to keep markup that is safe to RENDER, but this content is never rendered -- it
is read by a model as potential instructions, and ``<b>Ignore previous instructions</b>`` passes
every sanitizer in existence while being exactly the payload TOOL-06 exists to stop. Escaping
additionally keeps the evidence: markup survives as literal characters, so the human reviewing the
call in plan 05-09 sees the same bytes the agent saw rather than a cleaned-up version of them.

**Delimit AND datamark** (the "spotlighting" pair). A per-response random mark is generated, every
occurrence of it is REMOVED from the body so content cannot forge it, and it is then interleaved
through the body and carried by both delimiters. A reader can therefore tell the companion's bytes
apart from the surrounding instructions positionally, not just by trusting a boundary string --
and an injected string that spells out the closing delimiter survives as visible text WITHOUT the
mark rather than closing the envelope.

**Say what you did.** A body cut at the limit carries a ``TruncationNotice`` in the opening
delimiter naming the limit and the original length. Silent truncation is a lie about what the agent
received, and the prohibition this module works under is explicit about it (T-5-36).

None of these is a gate. The server-side class check is the gate; this is the return leg's harm
reduction, and it is described as such so nobody later mistakes it for authorization.

Standard library only.
"""

from __future__ import annotations

import re
import secrets
import unicodedata
from dataclasses import dataclass

UNTRUSTED_OPEN_PREFIX = "<<<UNTRUSTED_COMPANION_CONTENT"
UNTRUSTED_CLOSE_PREFIX = "<<<END_UNTRUSTED_COMPANION_CONTENT"

# The control characters a listing legitimately carries. Everything else in `Cc` goes.
_PRESERVED_CONTROLS = frozenset("\n\t")

# How much companion content one response may carry. Beyond this the body is truncated AND the
# envelope says so: silent truncation is a lie about what the agent received, and the prohibition
# in this plan's `must_haves` is explicit about it.
UNTRUSTED_BODY_LIMIT = 64 * 1024

# Runs of horizontal whitespace become a mark-bearing separator. Newlines are left alone so the
# content stays readable; the mark still lands between words on every line.
_HORIZONTAL_WHITESPACE = re.compile(r"[ \t]+")

# The three characters that open markup, in the order they must be replaced: `&` first, or the
# ampersands introduced by the other two would be escaped a second time and `<` would arrive as
# `&amp;lt;`. Quotes are deliberately absent -- quote escaping exists for ATTRIBUTE contexts, there
# is no attribute context here, and escaping them would corrupt the content a reviewer is reading
# for no safety gain.
_MARKUP_ESCAPES: tuple[tuple[str, str], ...] = (
    ("&", "&amp;"),
    ("<", "&lt;"),
    (">", "&gt;"),
)


@dataclass(frozen=True)
class TruncationNotice:
    """What was cut, and at what limit.

    Carried in the OPENING delimiter rather than appended after the body, for the same reason the
    façade puts its own cap notice before the envelope: a disclosure placed after companion bytes
    is a disclosure companion bytes could forge. ``original_length`` is there because "truncated at
    64 KiB" tells a reader nothing about whether they are missing a line or a library.
    """

    limit: int
    original_length: int

    def describe(self) -> str:
        """The attributes this notice contributes to the opening delimiter."""
        return f' truncated="true" limit="{self.limit}" original_length="{self.original_length}"'


def strip_active_markup(text: str) -> str:
    """Drop every format character and every control character except newline and tab."""
    return "".join(
        character
        for character in text
        if not (
            unicodedata.category(character) == "Cf"
            or (unicodedata.category(character) == "Cc" and character not in _PRESERVED_CONTROLS)
        )
    )


def escape_markup(text: str) -> str:
    """Escape HTML and XML-shaped markup so it survives as literal, inert characters.

    NOT an allowlist sanitizer, and the difference is the whole point (research assumption A11). A
    sanitizer keeps what is safe to render and drops the rest; this content is read as instructions
    rather than rendered, so "safe to render" is the wrong axis entirely -- a bold tag wrapped
    around an instruction passes every sanitizer and is precisely the payload TOOL-06 exists to
    stop. Escaping neutralizes the markup AND preserves it, which is what lets the reviewer in plan
    05-09 see what the agent saw instead of a version somebody already cleaned up.

    Stdlib-equivalent to ``html.escape(text, quote=False)``; written out so the three replacements
    and their order are visible at the point a reader is deciding whether quotes belong here.
    """
    for character, escaped in _MARKUP_ESCAPES:
        text = text.replace(character, escaped)
    return text


def wrap_untrusted(
    body: str,
    *,
    content_id: str,
    source: str,
    mark: str | None = None,
    limit: int = UNTRUSTED_BODY_LIMIT,
) -> str:
    """Strip, truncate if needed, escape, datamark, and delimit one companion response.

    The ORDER below is the design, and each step depends on the one before it.

    1. **Strip** first, so a zero-width character cannot sit inside a markup token and split it
       past the escaper.
    2. **Truncate** against the STRIPPED length, so the limit bounds what the companion actually
       said rather than what its escaping expands to -- and disclose it.
    3. **Escape**, so markup in the body is inert before anything is built around it. Doing this
       after the envelope was assembled would escape the façade's own delimiters and destroy the
       boundary they exist to draw.
    4. **Remove the mark**, so content can neither carry it nor close the envelope with it.
    5. **Interleave and delimit.**

    ``mark`` is injectable for tests only; production always generates a fresh one per response.
    A fixed or reused mark would be a boundary string an injected payload could learn from one
    response and forge in the next, which is the whole failure this design is built against.
    """
    stripped = strip_active_markup(body)
    notice = (
        TruncationNotice(limit=limit, original_length=len(stripped))
        if len(stripped) > limit
        else None
    )
    if notice is not None:
        stripped = stripped[:limit]
    escaped = escape_markup(stripped)
    token = mark or secrets.token_hex(4)
    # Removed BEFORE interleaving so content can neither carry the mark nor close the envelope.
    escaped = escaped.replace(token, "")
    marked = _HORIZONTAL_WHITESPACE.sub(f" {token} ", escaped)
    disclosure = "" if notice is None else notice.describe()
    opening = (
        f'{UNTRUSTED_OPEN_PREFIX} id="{content_id}" source="{source}" mark="{token}"{disclosure}>>>'
    )
    closing = f'{UNTRUSTED_CLOSE_PREFIX} mark="{token}">>>'
    return f"{opening}\n{marked}\n{closing}"
