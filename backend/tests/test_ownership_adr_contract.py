"""Doc-contract assertions pinning ADR 0005, its sibling mirror, and the PROJECT.md rows.

The mechanism is shared with the other prose contracts through ``tests.doc_contract``: every
assertion runs over casefolded, whitespace-collapsed text, so the claims survive prose reflow
but each asserted substring must itself be lowercase and single-spaced.

The sibling ``hermes-deploy`` checkout is located through the ``HERMES_DEPLOY_PATH``
environment variable with **no default**. This file is committed to a public repository, so a
personal filesystem path must never appear in it -- not even as a fallback. When the variable
is unset the mirror assertion skips with a reason that says so; when it is set but wrong the
assertion FAILS rather than skipping, because those are different states and only the first is
a deliberate one. ``AGENTS.md`` records the variable under **Backend checks** so the intended
run mode is discoverable without reading this file.
"""

import os
import re
from pathlib import Path

import pytest

from tests.doc_contract import REPO_ROOT as _REPO_ROOT
from tests.doc_contract import normalized_text as _normalized_text

_ADR_NAME = "0005-clawdi-owns-tenancy-billing-and-vault-the-provisioner-owns-compute.md"
_ADR_PATH = _REPO_ROOT / "docs" / "adr" / _ADR_NAME
_PROJECT_MD = _REPO_ROOT / ".planning" / "PROJECT.md"
_REQUIREMENTS_MD = _REPO_ROOT / ".planning" / "REQUIREMENTS.md"
_STATE_MD = _REPO_ROOT / ".planning" / "STATE.md"

_HERMES_DEPLOY_PATH = os.environ.get("HERMES_DEPLOY_PATH")
_SIBLING_ADR = (
    Path(_HERMES_DEPLOY_PATH) / "docs" / "adr" / _ADR_NAME if _HERMES_DEPLOY_PATH else None
)

# Unset is the ONLY deliberate skip. A set-but-wrong path used to skip identically, so a
# one-character typo in an operator's shell turned the mirror guarantee off with a green exit
# code; that case is now a hard failure inside the test body below.
_MIRROR_SKIP_REASON = (
    "HERMES_DEPLOY_PATH unset; export it as the hermes-deploy checkout to run the byte-for-byte "
    "mirror assertion (AGENTS.md, Backend checks)"
)

_ADR_REQUIRED_NORMALIZED_TEXT = (
    # Terms, defined before any verdict (Finding 1 -- "the provisioner" names two systems).
    "clawdi means this repository together with clawdi-hosted",
    "the provisioner means the hermes-deploy repository",
    # The ownership verdict, one clause per named concern.
    "tenancy is clerk-backed in this repository",
    # Vault is pinned by the sentences that name its owner and its exclusivity, not by the
    # environment-variable name. `VAULT_ENCRYPTION_KEY` is data: it survives a rewrite that moves
    # the vault to the provisioner, which is exactly the inversion this ADR exists to prevent.
    # The key name is kept, attached to the claim it supports.
    "the encrypted vault is this repository's",
    "the vault is the sole store for anything tenant-facing",
    "aes-256-gcm keyed by `vault_encryption_key`",
    "payment is resolved in clawdi-hosted behind /v2/subscription/*",
    "compute moves to the provisioner behind /v2/deployments/*",
    # Entitlement is the one concern nothing can cite, so it is pinned as an open item
    # rather than assigned a location (Finding 5, threat T-01-02).
    "entitlement is the mapping from subscription state to what a tenant may provision",
    "no entitlement module is citable in either repository",
    # D-04: owning payment is meaningless if the holder of compute can decide shutoff.
    "refund and shutoff decisioning stays clawdi's",
    "the provisioner only executes",
    # D-03: the credential partition that keeps tenant-facing secrets in Clawdi's vault.
    "the provisioner holds operational credentials only",
    "passed by reference, never by value",
    # OWN-02: one canonical contract, and the boundary it does not cut.
    "the single canonical provisioning contract",
    "keep_operations_by_path",
    "a responsibility boundary, not a document boundary",
    "the executable reference implementation of the contract",
    # The corrected account of the starting state (Finding 3) and D-18's publisher.
    "the ci-enforced spec source has always been clawdi-side",
    "schema authorship does not move with it",
    "api.clawdi.ai remains the canonical publisher",
    "the document stays merged",
    # Conformance, stated with the drift check's real limits.
    "the conformance obligation lands on the provisioner's side",
    "this repository's drift check is not sufficient evidence on its own",
    # D-09's disposition at Finding 7's precision, and the postures.
    "no removal planned",
    "the payment-triggered provisioning entry point is hard-disabled",
    "survives as provisioner-internal implementation",
    "polling is canonical",
    "the consumer-first protocol binds /v2 changes",
)

# These exist to stop the document re-acquiring a wrong claim after it has been corrected
# once. The guard compares bytes, so a sentence rejecting one of these fails identically to a
# sentence making it: the corrected fact must be written positively instead.
#
# The three trading claims that used to sit here have moved to
# ``tests.doc_contract.FORBIDDEN_TRADING_CLAIMS``, consumed only by the Hetzner contract. This
# ADR contains the word "trading" zero times and does not discuss vendor terms at all, so those
# entries were guaranteed to pass here while the two files' sets had already drifted (3 against
# 5). A guard that cannot fail is not coverage.
_FORBIDDEN_NORMALIZED_TEXT = (
    # Finding 3: the corrected account of the starting state. The likely regression wordings are
    # covered as well as the two phrasings that were actually written and corrected once.
    "inverts today's generation direction",
    "inverts the generation direction",
    "reverses today's generation direction",
    "reverses the generation direction",
    "reverses an existing flow",
    "flips the spec source",
    # D-03 inverted. The credential partition is the load-bearing half of the vault verdict, so
    # the two ways of writing its opposite are rejected outright rather than left to the
    # positive assertions above.
    "vault moves to the provisioner",
    "the provisioner holds tenant application",
)

_PROJECT_MD_REQUIRED_NORMALIZED_TEXT: tuple[str, ...] = (
    "clawdi owns all four",
    "/v2/deployments/* is canonical",
    "api.clawdi.ai remains the publisher",
    f"../docs/adr/{_ADR_NAME}",
)

# Presence assertions alone let a stale paragraph survive beside the verdict it contradicts:
# the ownership row once carried "Unresolved, and blocking both projects" in its Rationale cell
# while its own Outcome cell already read "Clawdi owns all four". Both halves were true of the
# text and false of each other, and every required string above still matched.
#
# Scope and limits, stated so a reader does not over-trust this guard:
#   - It reads THIS repository's PROJECT.md only. The hermes-deploy copy is not pinned here;
#     it is kept in step by the sibling-reconciliation rule in plan 01-04.
#   - It pins these three historical phrasings, not the class of defect. A differently-worded
#     contradiction ("still open", "not yet decided") passes. Broadening the set is not free:
#     this same vocabulary now appears legitimately in the past-tense narration two lines away,
#     so a looser pattern would fire on correct text.
_PROJECT_MD_FORBIDDEN_NORMALIZED_TEXT: tuple[str, ...] = (
    "unresolved, and blocking both projects",
    "are now blocked on one unanswered question",
    "must be resolved before infrastructure phases are planned",
)

# D-14 moved the control plane off the tenant-workload substrate, which narrows what the
# core-value claim's "infrastructure we own" covers. The claim is recorded in four places; this
# tuple is the one sentence all of them must carry, character for character, so a partial edit
# that revises one document and forgets the others fails here rather than shipping a divergence.
#
# ``.claude/CLAUDE.md`` carries the same claim and is deliberately NOT asserted over: its
# Developer Profile section is generated by ``generate-claude-profile``, so a future
# regeneration could fail this suite for a reason unrelated to the ownership decision. It is
# kept in step by hand, and by the plan's grep-every-site rule, not by this test.
_CORE_VALUE_SHARED_NORMALIZED_TEXT = (
    "an agent runtime we own, on infrastructure we own — the runtime and the "
    "tenant-workload substrate beneath it, while the control plane is deliberately hosted "
    "off that substrate — doing real work in applications we deploy."
)

# The claim must name the layers it covers, so a reader cannot take "infrastructure we own"
# as covering the control plane. Asserted separately from the full sentence above so a
# failure says which half drifted.
_CORE_VALUE_SCOPE_NORMALIZED_TEXT = (
    "the runtime and the tenant-workload substrate",
    "the control plane is deliberately hosted off that substrate",
)

_CORE_VALUE_DOCS = (_PROJECT_MD, _REQUIREMENTS_MD, _STATE_MD)

# The three wave-1 documents cross-reference each other. Each wave-1 plan asserted only that
# its own document carried the link text -- none could assert the target existed without
# racing a sibling plan in the same wave. Link-target existence is therefore checked here.
_CROSS_LINKED_DOCS = (
    _ADR_PATH,
    _REPO_ROOT / "docs" / "hetzner-terms-position.md",
    _REPO_ROOT / "docs" / "infrastructure-layout.md",
)

_MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")

# Below this, a traversal that found nothing would pass vacuously. Enforced PER DOCUMENT: a
# single counter summed across all three was satisfied by ADR 0005's four links alone, so the
# other two wave-1 documents could lose every cross-reference with the suite still green.
_MINIMUM_RELATIVE_LINKS_PER_DOC = 2


# The ownership-boundary and provisioning-contract rows resolve in this plan; the
# companion-licensing row is Phase 6's to close and must still read pending.
#
# The count is kept as a secondary check but is not sufficient on its own: it is row-agnostic,
# so resolving the companion-licensing row and adding an unrelated pending row anywhere else
# leaves the total unchanged. The row that must stay pending is therefore named.
_EXPECTED_PENDING_ROWS = 6
_PENDING_MARKER = "— pending"
_ROWS_THAT_MUST_STAY_PENDING = (
    "vibe-trading (mit) clears, the other two are unverified | — pending",
)


def test_ownership_adr_records_the_four_way_verdict() -> None:
    adr = _normalized_text(_ADR_PATH)

    for required in _ADR_REQUIRED_NORMALIZED_TEXT:
        assert required in adr


def test_ownership_adr_rejects_corrected_claims() -> None:
    adr = _normalized_text(_ADR_PATH)

    for forbidden in _FORBIDDEN_NORMALIZED_TEXT:
        assert forbidden not in adr


def test_forbidden_and_required_substrings_do_not_collide() -> None:
    """A forbidden entry inside a required one makes the two assertions unsatisfiable."""
    for forbidden in _FORBIDDEN_NORMALIZED_TEXT:
        for required in _ADR_REQUIRED_NORMALIZED_TEXT:
            assert forbidden not in required


@pytest.mark.skipif(_SIBLING_ADR is None, reason=_MIRROR_SKIP_REASON)
def test_ownership_adr_is_mirrored_byte_for_byte() -> None:
    assert _SIBLING_ADR is not None

    assert _SIBLING_ADR.exists(), (
        f"HERMES_DEPLOY_PATH={_HERMES_DEPLOY_PATH!r} is set but {_SIBLING_ADR} does not exist. "
        "A set-but-wrong path is an operator error, not an absent checkout: unset the variable "
        "to skip deliberately, or fix the path."
    )

    assert _SIBLING_ADR.read_bytes() == _ADR_PATH.read_bytes(), (
        f"{_ADR_PATH} and {_SIBLING_ADR} have diverged. This ADR is mirrored byte-for-byte; a "
        "reader holding either checkout must get the same answer, so re-mirror rather than "
        "editing one copy."
    )


def test_core_value_claim_is_consistent_across_planning_docs() -> None:
    """One shared substring across three files: a partial edit fails, a paraphrase fails."""
    for doc in _CORE_VALUE_DOCS:
        text = _normalized_text(doc)

        # Specific first, verbatim-identity last. Both scope claims are strict substrings of the
        # shared sentence, so asserting the shared sentence first made them unreachable: if it
        # was present they passed trivially, and if it was absent the loop had already raised.
        # In this order a dropped half fails on its own claim, and a paraphrase that keeps both
        # halves fails on the identity check below with a distinguishable message.
        for scope_claim in _CORE_VALUE_SCOPE_NORMALIZED_TEXT:
            assert scope_claim in text, (
                f"{doc.relative_to(_REPO_ROOT)} does not state that {scope_claim!r}"
            )

        assert _CORE_VALUE_SHARED_NORMALIZED_TEXT in text, (
            f"{doc.relative_to(_REPO_ROOT)} states both halves of the core-value claim but not "
            "verbatim; every site must read identically or the claim has diverged"
        )


def _relative_link_targets(doc: Path) -> list[str]:
    raw = doc.read_text(encoding="utf-8")

    targets: list[str] = []
    for target in _MARKDOWN_LINK.findall(raw):
        target = target.strip()
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        # An in-page anchor suffix names a heading, not a second file.
        target = target.split("#", 1)[0]
        if target:
            targets.append(target)
    return targets


def test_cross_document_links_resolve() -> None:
    """Every relative link between the three wave-1 documents points at a file on disk.

    Each target resolves against its own linking document's parent directory, not against the
    repository root: ADR 0005 sits in ``docs/adr/`` and links to ``../hetzner-terms-position.md``,
    while the layout document sits in ``docs/`` and links to ``adr/0005-*.md``. Root-relative
    resolution would report a false failure for both.
    """
    wave_1 = {d.resolve() for d in _CROSS_LINKED_DOCS}

    for doc in _CROSS_LINKED_DOCS:
        assert doc.exists(), f"wave-1 document missing: {doc.relative_to(_REPO_ROOT)}"

        targets = _relative_link_targets(doc)
        resolved_targets = {(doc.parent / target).resolve(): target for target in targets}

        for resolved, target in resolved_targets.items():
            assert resolved.exists(), (
                f"{doc.relative_to(_REPO_ROOT)} links to {target!r}, which resolves to "
                f"{resolved} — no such file. The cross-document link graph does not close."
            )

        assert len(targets) >= _MINIMUM_RELATIVE_LINKS_PER_DOC, (
            f"{doc.relative_to(_REPO_ROOT)} carries {len(targets)} relative link(s), fewer than "
            f"{_MINIMUM_RELATIVE_LINKS_PER_DOC}; its cross-references have been removed and this "
            "check would pass vacuously for that document"
        )

        # Resolvable is not the same as cross-linked: a document could satisfy the floor above
        # with two links to unrelated files. The wave-1 set must point at itself.
        siblings = wave_1 - {doc.resolve()}
        assert resolved_targets.keys() & siblings, (
            f"{doc.relative_to(_REPO_ROOT)} links to no other wave-1 document; the three "
            "documents no longer reference each other"
        )


def test_project_md_records_the_ownership_verdict() -> None:
    project = _normalized_text(_PROJECT_MD)

    for required in _PROJECT_MD_REQUIRED_NORMALIZED_TEXT:
        assert required in project

    for forbidden in _PROJECT_MD_FORBIDDEN_NORMALIZED_TEXT:
        assert forbidden not in project, (
            f".planning/PROJECT.md still claims the ownership question is open: {forbidden!r}. "
            "ADR 0005 resolved it; a present-tense 'unresolved' paragraph contradicts the "
            "verdict recorded in the same document."
        )

    for row in _ROWS_THAT_MUST_STAY_PENDING:
        assert row in project, (
            f"a row expected to remain pending has been resolved: {row!r}. Companion licensing "
            "is Phase 6's to close, not this phase's."
        )

    found = project.count(_PENDING_MARKER)
    assert found == _EXPECTED_PENDING_ROWS, (
        f".planning/PROJECT.md carries {found} pending row(s), expected {_EXPECTED_PENDING_ROWS}"
    )
