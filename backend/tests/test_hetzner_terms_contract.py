import re
from pathlib import Path

import pytest

from tests.doc_contract import FORBIDDEN_TRADING_CLAIMS
from tests.doc_contract import REPO_ROOT as _REPO_ROOT
from tests.doc_contract import normalized_text as _normalized_text

_POSITION_DOC = _REPO_ROOT / "docs" / "hetzner-terms-position.md"

# This document owns the vendor-terms question, so it is the only consumer of the shared tuple.
_FORBIDDEN_NORMALIZED_TEXT = FORBIDDEN_TRADING_CLAIMS
_REQUIRED_NORMALIZED_TEXT = (
    # Quoted verbatim from the primary terms, re-extracted at write time.
    "the operation of applications for mining cryptocurrencies remains prohibited",
    "mining, farming and plotting of cryptocurrencies",
    "the customer is entitled to grant third parties a contractual term of use",
    "the customer nevertheless remains the sole contractual partner",
    "we are entitled to lock the customer's access to their hetzner services or account",
    # The finding on trading, stated positively — a negation would trip the forbidden set.
    "the published terms are silent on trading",
    "press-reported, not a hetzner-published document",
    "a risk-based decision, not a compliance one",
    # Provenance: a quotation without these three is a paraphrase wearing quotation marks.
    "https://cdn.hetzner.com/assets/uploads/agb-en.pdf",
    "terms and conditions version 2.0.0",
    "retrieved:",
    "document version:",
    # Ticket-log structure. Asserted on shape, never on a ticket ID: an ID is data, and
    # pinning it would turn the suite red the moment a reply is appended — and would also
    # make the phase hostage to an action the operator is free to decline.
    "no reply as of",
    "escalate at approximately 10 business days",
    "confirmation of a reading, not discovery",
    "neither the reply nor the ticket blocks this phase",
)

# The lock entitlement is the evidentiary payload of this whole document: it is the sole support
# for the account-scoped suspension walkthrough in ``docs/infrastructure-layout.md``. It is
# therefore asserted against the QUOTED material only -- ``_quoted_clause_text`` below -- so that
# deleting the verbatim clauses and leaving the transcription note's paraphrase standing fails.
# A paraphrase written by the same author in the same commit is not a primary source.
_LOCK_ENTITLEMENT = (
    "we are entitled to lock the customer's access to their hetzner services or account"
)

# §8.3 and §8.4 each quote it. ``>=`` rather than ``==`` because this document is append-only:
# a later retrieval may quote the same clause again, and that must not turn the suite red.
_MINIMUM_QUOTED_LOCK_ENTITLEMENTS = 2

# Each entry is a whole quoted clause, marker included, so the clause cannot be hollowed out
# while its number survives.
_REQUIRED_QUOTED_CLAUSE_TEXT = (
    "**8.3.** the transmission of spam mail is prohibited.",
    "the operation of applications for mining cryptocurrencies remains prohibited. these include, "
    "but are not limited to, mining, farming and plotting of cryptocurrencies. we are entitled to "
    "lock the customer's access to their hetzner services or account in the event of "
    "non-compliance.",
    "**8.4.** if we become aware of illegal activities, we are obligated under art. 6 abs. 1 dsa "
    "(digital services act) to request that the customer immediately removes the offending content "
    "and we are entitled to lock the customer's access to their hetzner services or account.",
)


# Per-clause provenance. The document's own opening claim is that "each block carries its document
# title, document version, source URL and retrieval instant" -- a per-block property. The four
# entries in _REQUIRED_NORMALIZED_TEXT above each need occur only ONCE ANYWHERE in the file, so
# every per-clause `Source:` line could be deleted and the file still satisfy them; `"retrieved:"`
# in particular would be satisfied by `Retrieved: never`. These assertions read the shape instead.
_PROVENANCE_LINE = re.compile(
    r"^Source: .+?retrieved \d{4}-\d{2}-\d{2}T\d{2}:\d{2}Z", re.MULTILINE | re.DOTALL
)

# §7, §8, and the Cloud/vServer Service Agreement §2. ``>=`` because this document is append-only.
_MINIMUM_PER_CLAUSE_CITATIONS = 3


def _sections(raw: str) -> list[tuple[str, list[str]]]:
    """(heading, body lines) pairs, split on every markdown heading line."""
    sections: list[tuple[str, list[str]]] = [("(document preamble)", [])]
    for line in raw.splitlines():
        if line.startswith("#"):
            sections.append((line.lstrip("#").strip(), []))
        else:
            sections[-1][1].append(line)
    return sections


def _quoted_clause_text(path: Path) -> str:
    """Normalized text of the document's blockquoted material only, ``>`` markers removed.

    A blockquote marker is markdown formatting rather than part of the quotation, and it lands
    mid-sentence whenever a quoted clause wraps -- which is why an assertion typed as one
    continuous sentence cannot match §8.3 in the full-document text. Stripping it here rather
    than in the shared normalizer keeps the other three doc-contract suites reading exactly the
    bytes they read today, while letting these assertions pin the quotation itself instead of
    any prose in the same file that restates it.
    """
    quoted = [
        line.strip()[1:]
        for line in path.read_text(encoding="utf-8").replace("’", "'").splitlines()
        if line.strip().startswith(">")
    ]
    return " ".join(" ".join(quoted).casefold().split())


def test_hetzner_position_rejects_an_unquotable_prohibition() -> None:
    text = _normalized_text(_POSITION_DOC)

    for forbidden in _FORBIDDEN_NORMALIZED_TEXT:
        assert forbidden not in text


def test_hetzner_position_quotes_the_primary_terms() -> None:
    position = _normalized_text(_POSITION_DOC)

    for required in _REQUIRED_NORMALIZED_TEXT:
        assert required in position


def test_hetzner_position_quotes_both_lock_entitlements_verbatim() -> None:
    """The §8.3 and §8.4 lock clauses must survive as quotations, not as a paraphrase."""
    quoted = _quoted_clause_text(_POSITION_DOC)

    for required in _REQUIRED_QUOTED_CLAUSE_TEXT:
        assert required in quoted, (
            f"no quoted block carries {required!r}. The suspension walkthrough in "
            "docs/infrastructure-layout.md cites this clause as its only support; a clause that "
            "is no longer quoted here cannot support it."
        )

    found = quoted.count(_LOCK_ENTITLEMENT)
    assert found >= _MINIMUM_QUOTED_LOCK_ENTITLEMENTS, (
        f"{found} quoted lock entitlement(s) found, expected at least "
        f"{_MINIMUM_QUOTED_LOCK_ENTITLEMENTS} (§8.3 and §8.4). The transcription note's "
        "ASCII rendering is a paraphrase-of-a-quote and does not substitute for either clause."
    )


def test_hetzner_position_cites_every_quoted_block() -> None:
    """A quotation without a retrieval instant above it is a paraphrase wearing quotation marks."""
    raw = _POSITION_DOC.read_text(encoding="utf-8")

    citations = _PROVENANCE_LINE.findall(raw)
    assert len(citations) >= _MINIMUM_PER_CLAUSE_CITATIONS, (
        f"only {len(citations)} per-clause provenance line(s) found, expected at least "
        f"{_MINIMUM_PER_CLAUSE_CITATIONS} (§7, §8, Cloud/vServer §2). This document's opening "
        "claim is that each quoted block carries its own source URL and retrieval instant."
    )

    for heading, body in _sections(raw):
        quote_at = next((i for i, line in enumerate(body) if line.startswith(">")), None)
        if quote_at is None:
            continue

        preamble = "\n".join(body[:quote_at])
        assert _PROVENANCE_LINE.search(preamble), (
            f"the section {heading!r} quotes a clause with no `Source: ... retrieved "
            "<instant>` line above the quotation"
        )


@pytest.mark.parametrize("forbidden", _FORBIDDEN_NORMALIZED_TEXT)
def test_forbidden_and_required_substrings_do_not_collide(forbidden: str) -> None:
    for required in _REQUIRED_NORMALIZED_TEXT:
        assert forbidden not in required
