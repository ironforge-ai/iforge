"""Red and green proof for the phase 03.1 rebrand guard scripts.

Two committed scripts are the only automated protection the ``clawdi`` -> ``iforge`` rebrand
has: ``scripts/check_rebrand_carveouts.py`` asserts that every do-not-rename literal is still
present at the path that owns it, and ``scripts/check_rebrand_residual.py`` scans the brand
surface for hits the allowlist does not cover. GitHub Actions never execute against this
origin (``lefthook.yml``), so a guard whose *failing* behaviour was never demonstrated is not
protection at all -- it is a green light with no bulb behind it.

Every red case here injects ``contents`` rather than mutating the working tree. A guard proven
by writing a corrupted pin to disk would leave the repository one crashed test away from a real
supply-chain edit, and would be unrunnable in parallel. The guards therefore take their inputs
by dependency injection, exactly as ``scripts/check_postgres_image_parity.py`` already does.

The scripts are not an importable package -- ``scripts/`` has no ``__init__.py`` and the guards
must stay runnable under bare ``python3``. They are loaded by path through
``importlib.util.spec_from_file_location``, with the repository root resolved through
``tests.doc_contract.REPO_ROOT`` so this module agrees with the other repository-root suites.
"""

from __future__ import annotations

import ast
import importlib.util
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from types import ModuleType

import pytest

from tests.doc_contract import REPO_ROOT as _REPO_ROOT
from tests.doc_contract import planning_present

_SCRIPTS = _REPO_ROOT / "scripts"

# Plan 03.1-11 made the residual scanner read UNTRACKED files, so both the hit scan
# and the coverage enumeration shell out to git. The clean Docker runner
# (`scripts/test.sh`) rsyncs the checkout in WITHOUT repository metadata, so every
# git-backed assertion below fails there with "fatal: not a git repository" -- a
# missing tool, not a dirty tree. That is the WINDOWS #38/#77 class, and its accepted
# remedy is to skip inside the container and enforce host-side. The GUARD itself stays
# fail-closed; only the assertions that cannot run without git are gated.
#
# WHAT THE HOST-SIDE RUN ACTUALLY COVERS (WR-06). The previous version of this comment
# claimed the gated tests are "enforced where the evidence exists: scripts/test.sh runs
# both guard scripts host-side against the real checkout before the compose build."
# That is true of the assertions that exercise the GREEN path over the real tree,
# because running the guard IS that assertion. It was not true of all of them, and the
# claim being wider than the coverage is the same defect this file exists to catch, one
# level up. The ledger, per gated test:
#
#   COMPENSATED by `python3 scripts/check_rebrand_carveouts.py` (its main() calls
#   validate_schema_identifiers):
#     - test_every_wire_protocol_schema_identifier_still_resolves_in_the_tree
#
#   COMPENSATED by `python3 scripts/check_rebrand_residual.py` (its main() calls
#   assert_scan_is_not_vacuous, unclassified_entries, casing_violations and scan):
#     - test_the_real_scanner_is_clean_against_the_real_tree
#     - test_the_scan_floor_is_satisfied_by_the_real_tree
#     - test_every_repository_entry_is_either_scanned_or_explicitly_out_of_scope
#     - test_the_real_tree_spells_the_product_name_in_lower_case_everywhere
#     - test_the_unfenceable_rule_matches_no_hit_in_the_real_tree
#
#   NOT COMPENSATED, and recorded rather than implied:
#     - test_every_env_example_in_the_repository_is_inside_the_scanned_set. It asserts
#       that SCAN_PATHSPEC still REACHES every `*.env.example`, which is a property of
#       the pathspec rather than of the tree. Dropping that pathspec entry would leave
#       the guard green host-side -- the file count floor (736 against a floor of 600)
#       absorbs it, and `unclassified_entries` counts a directory as covered when the
#       guard reads any of its files, which `*.md` already does everywhere. So this one
#       is enforced only where git exists. Closing it means widening the guard, not the
#       comment, and that is deliberately not done in a fix pass.
#
#   NO LONGER GATED, because it was the only exit-code-level RED path of the
#   classification gate and a green guard run establishes nothing about a red path:
#     - test_the_scanner_exits_non_zero_on_an_undeclared_section, rewritten to stub the
#       four git-dependent calls main() makes before the check under test.
#
# One marker, not two: `_needs_git` and `_NEEDS_GIT` were separately defined here with
# different reason strings, the second added without noticing the first (IN-02).
_IN_CHECKOUT = (_REPO_ROOT / ".git").exists()
_NEEDS_GIT = pytest.mark.skipif(
    not _IN_CHECKOUT,
    reason=(
        "needs repository metadata; see the host-side coverage ledger at the head of "
        "this module for which assertions scripts/test.sh re-enforces and which it does not"
    ),
)


def _load_guard(name: str) -> ModuleType:
    """Load a bare ``scripts/*.py`` guard by path, under a namespaced module name."""
    path = _SCRIPTS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_rebrand_guard_{name}", path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError(f"cannot load guard script {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# `test_both_rebrand_guards_are_wired_into_the_canonical_test_run` asserts the host-side
# wiring the ledger above depends on, from INSIDE the container where `_NEEDS_GIT` cannot
# reach. Without it the ledger would be a claim about a command nothing checks is run.

carveouts = _load_guard("check_rebrand_carveouts")


def _real_contents() -> dict[str, str]:
    """The exact mapping ``validate_carve_outs`` builds for itself when given no argument."""
    return {
        path: (_REPO_ROOT / path).read_text(encoding="utf-8") for path, _ in carveouts.CARVE_OUTS
    }


def test_carve_out_guard_passes_against_the_untouched_working_tree() -> None:
    """Test 1 (green). D-10 requires the guard be green BEFORE anything moves."""
    verified = carveouts.validate_carve_outs()

    assert isinstance(verified, int)
    assert verified > 0
    assert verified == len(carveouts.CARVE_OUTS)


def test_carve_out_guard_rejects_a_renamed_postgres_image_pin() -> None:
    """Test 2 (red, C-1). ``ghcr.io/clawdi-ai/postgres-pgbackrest`` is a real upstream address.

    Renaming the org segment points every pull at a repository that does not exist.
    """
    contents = _real_contents()
    contents["config/postgres-image.txt"] = (
        "ghcr.io/ironforge-ai/postgres-pgbackrest:pg18@sha256:" + "0" * 64 + "\n"
    )

    with pytest.raises(carveouts.CarveOutError, match="config/postgres-image.txt"):
        carveouts.validate_carve_outs(contents=contents)


def test_carve_out_guard_rejects_a_renamed_enforcing_pin_pattern() -> None:
    """Test 3 (red, C-2). The pitfall the whole guard exists for.

    Renaming ``PIN_PATTERN`` *together with* the five pins keeps
    ``check_postgres_image_parity.py`` green -- the checker validates internal consistency --
    while every image pull 404s. Only an external literal assertion catches it.
    """
    contents = _real_contents()
    path = "scripts/check_postgres_image_parity.py"
    contents[path] = contents[path].replace("clawdi-ai", "ironforge-ai")

    with pytest.raises(carveouts.CarveOutError, match="check_postgres_image_parity.py"):
        carveouts.validate_carve_outs(contents=contents)


def test_carve_out_guard_rejects_a_renamed_api_key_prefix() -> None:
    """Test 4 (red, C-6). Every key already in the database carries the ``clawdi_`` prefix.

    ``auth.py`` validates presented tokens against the constant, so flipping it rejects the
    entire installed base at once.
    """
    contents = _real_contents()
    path = "backend/app/core/auth.py"
    contents[path] = contents[path].replace(
        'API_KEY_PREFIX = "clawdi_"', 'API_KEY_PREFIX = "iforge_"'
    )

    with pytest.raises(carveouts.CarveOutError, match="backend/app/core/auth.py"):
        carveouts.validate_carve_outs(contents=contents)


def test_carve_out_guard_rejects_a_renamed_wire_protocol_schema_identifier() -> None:
    """Test 5 (red, C-4). The ``clawdi.<name>.vN`` literals are pydantic discriminators.

    They are persisted in Postgres JSONB and in on-disk runtime state, so a rename makes every
    already-written row fail its own ``Literal[...]`` validation on read.
    """
    contents = _real_contents()
    path = "backend/app/schemas/runtime_observation.py"
    contents[path] = contents[path].replace(
        "clawdi.hostedRuntimeObserved.v2", "iforge.hostedRuntimeObserved.v2"
    )

    with pytest.raises(carveouts.CarveOutError, match="runtime_observation.py"):
        carveouts.validate_carve_outs(contents=contents)


def test_carve_out_guard_rejects_a_renamed_sentry_scrub_entry() -> None:
    """Test 5b (red, C-5 / T-03.1-02). The phase's highest-severity latent finding.

    ``backend/app/core/sentry.py`` scrubs ``x_clawdi_token`` (dash-normalized since upstream
    #1596). A header rename without a matching scrubber edit starts shipping the auth token to
    Sentry -- and nothing in the application would fail. Only the carve-out catches it.
    """
    contents = _real_contents()
    path = "backend/app/core/sentry.py"
    contents[path] = contents[path].replace("x_clawdi_token", "x_iforge_token")

    with pytest.raises(carveouts.CarveOutError, match="backend/app/core/sentry.py"):
        carveouts.validate_carve_outs(contents=contents)


def test_carve_out_guard_rejects_a_renamed_discord_gateway_jwt_claim() -> None:
    """Test 5c (red, C-9). ``clawdi_discord_gateway`` is the ``sub``/``aud`` pair in tokens
    that have already been issued; renaming it invalidates them mid-flight."""
    contents = _real_contents()
    path = "backend/app/routes/channel_routers/discord.py"
    contents[path] = contents[path].replace("clawdi_discord_gateway", "iforge_discord_gateway")

    with pytest.raises(carveouts.CarveOutError, match="discord.py"):
        carveouts.validate_carve_outs(contents=contents)


def test_carve_out_guard_rejects_a_path_that_is_absent_from_the_supplied_contents() -> None:
    """A carve-out whose file has moved is itself a defect -- it must not silently pass."""
    contents = _real_contents()
    del contents["config/postgres-image.txt"]

    with pytest.raises(carveouts.CarveOutError, match="config/postgres-image.txt"):
        carveouts.validate_carve_outs(contents=contents)


def test_carve_out_inventory_is_broad_and_every_named_path_still_exists() -> None:
    """Test 6 (coverage floor). Fewer than twenty literals is not a guard, it is a sample."""
    assert len(carveouts.CARVE_OUTS) >= 20

    missing = [path for path, _ in carveouts.CARVE_OUTS if not (_REPO_ROOT / path).is_file()]
    assert missing == []


def test_carve_out_literals_are_unique_pairs() -> None:
    """A duplicated pair inflates the reported count without verifying anything new."""
    assert len(set(carveouts.CARVE_OUTS)) == len(carveouts.CARVE_OUTS)


def test_alembic_revision_files_are_still_at_their_original_paths() -> None:
    """C-10. Alembic resolves by revision id, so renaming the files buys nothing and
    guarantees a permanent upstream merge conflict."""
    verified = carveouts.validate_alembic_revisions()

    assert verified == len(carveouts.ALEMBIC_REVISION_FILES)
    assert verified >= 2


@_NEEDS_GIT
def test_every_wire_protocol_schema_identifier_still_resolves_in_the_tree() -> None:
    """C-4. The 55 identifiers are enumerated in the guard; the movable set (native release,
    ``auth status`` and ``config paths`` output) is exempt because nothing persists it."""
    verified = carveouts.validate_schema_identifiers()

    assert verified == len(carveouts.SCHEMA_IDENTIFIERS)
    assert len(carveouts.SCHEMA_IDENTIFIERS) == 55
    assert "clawdi.nativeRelease.v1" in carveouts.SCHEMA_IDENTIFIERS
    assert carveouts.MOVABLE_SCHEMA_IDENTIFIERS == frozenset(
        {"clawdi.nativeRelease.v1", "clawdi.authStatus.v1", "clawdi.configPaths.v1"}
    )
    # Upstream-retired identifiers are a subset of the enumerated 55, never the movable one.
    assert carveouts.RETIRED_UPSTREAM_SCHEMA_IDENTIFIERS <= set(carveouts.SCHEMA_IDENTIFIERS)
    assert not carveouts.RETIRED_UPSTREAM_SCHEMA_IDENTIFIERS & carveouts.MOVABLE_SCHEMA_IDENTIFIERS


residual = _load_guard("check_rebrand_residual")


def _completed(
    returncode: int, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    """A stand-in for one ``git grep`` invocation, so exit-code handling is testable."""
    return subprocess.CompletedProcess(
        args=["git", "grep"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def test_allowlist_loader_skips_comments_and_blanks_and_classifies_every_prefix() -> None:
    """Test 7. The three-kind grammar is the contract plans 02-08 write entries against."""
    entries = residual.load_allowlist(
        "\n".join(
            [
                "# a leading comment",
                "",
                "   ",
                "   # an indented comment",
                "path:apps/web/src/*",
                "re:clawdi_[a-z_]+",
                "clawdi-ai/postgres-",
            ]
        )
    )

    assert entries == [
        ("path", "apps/web/src/*"),
        ("re", r"clawdi_[a-z_]+"),
        ("sub", "clawdi-ai/postgres-"),
    ]


def test_allowlist_loader_rejects_an_unparseable_regex_entry() -> None:
    """Test 8. A pattern that fails to compile would silently cover nothing, and the scanner
    would report clean while the tree was dirty -- a guard that lies is worse than no guard."""
    with pytest.raises(residual.ResidualError, match=r"clawdi\(unclosed"):
        residual.load_allowlist("re:clawdi(unclosed")


def test_find_residual_returns_nothing_when_every_hit_is_covered() -> None:
    """Test 9. One `sub`, one `path` and one `re` entry between them cover all three hits."""
    hits = [
        "config/postgres-image.txt:1:ghcr.io/clawdi-ai/postgres-pgbackrest:pg18",
        "apps/web/src/lib/env.ts:12:VITE_CLAWDI_HOSTED",
        'backend/app/core/auth.py:51:API_KEY_PREFIX = "clawdi_"',
    ]
    allowlist = residual.load_allowlist(
        "clawdi-ai/postgres-\npath:apps/web/src/*\nre:clawdi_[a-z_]*\n"
    )

    assert residual.find_residual(hits, allowlist) == []


def test_find_residual_returns_only_the_uncovered_hits_in_input_order() -> None:
    """Test 10. Order matters: the stderr report is read top-down against the tree."""
    hits = [
        "config/postgres-image.txt:1:ghcr.io/clawdi-ai/postgres-pgbackrest:pg18",
        "docs/readme.md:3:run clawdi auth login",
        "apps/web/src/lib/env.ts:12:VITE_CLAWDI_HOSTED",
        "install.sh:137:clawdi-cli-v1.0.0",
    ]
    allowlist = residual.load_allowlist("clawdi-ai/postgres-\npath:apps/web/src/*\n")

    assert residual.find_residual(hits, allowlist) == [
        "docs/readme.md:3:run clawdi auth login",
        "install.sh:137:clawdi-cli-v1.0.0",
    ]


def test_a_path_entry_matches_the_path_segment_only() -> None:
    """Test 11. The silent-pass mode that would allowlist the whole tree.

    A hit whose *text* quotes a path is not a hit *at* that path. Matching the glob against the
    whole `<path>:<line>:<text>` string would let one entry cover every file that merely
    mentions the directory.
    """
    hit = "docs/frontend-development.md:12:apps/web/src/lib/env.ts reads clawdi settings"
    allowlist = residual.load_allowlist("path:apps/web/src/*\n")

    assert residual.find_residual([hit], allowlist) == [hit]


def test_a_substring_entry_matches_anywhere_including_inside_the_path() -> None:
    """Test 12. `clawdi-ai/postgres-` must cover the config/postgres-image.txt hit, whose
    match lies in the text; the same entry also covers hits whose match lies in the path."""
    text_hit = "config/postgres-image.txt:1:ghcr.io/clawdi-ai/postgres-pgbackrest:pg18"
    path_hit = "config/clawdi-ai/postgres-notes.md:4:nothing brand-shaped on this line"
    allowlist = residual.load_allowlist("clawdi-ai/postgres-\n")

    assert residual.find_residual([text_hit, path_hit], allowlist) == []


# The X-2 unit-name glob as plan 03.1-13 narrowed it. Kept as one constant so the two
# directional cases below and the binding assertion cannot drift apart.
_NARROWED_UNIT_NAME_GLOB = r"clawdi-\*(?:\.service)?(?![\w.-])"


def test_the_narrowed_unit_name_glob_still_covers_every_unit_naming_rule() -> None:
    """Test 12b (X-2, green direction). `clawdi-*` was a bare SUBSTRING entry, and those
    eight characters spell a systemd unit-name glob and a tarball glob alike -- which is how
    a fence written for unit names came to cover six lines of docs/cli-development.md. The
    narrowing must not cost the nine managed-runtime sites that state the naming rule.
    """
    hits = [
        "docs/managed-runtime.md:168:        SystemUnits[systemd/system/clawdi-*.service"
        " or /run/systemd/system]",
        "docs/managed-runtime.md:202:and env. iforge support processes use `clawdi-*`"
        " service names; OpenClaw and",
        "docs/managed-runtime.md:255:`clawdi-*` names.",
        "docs/managed-runtime.md:1226:service installer, systemd must run it only as an"
        " explicit `clawdi-*`",
        "docs/plans/runtime-projection-boundary.md:152:iforge-owned support and"
        " compatibility units keep `clawdi-*` names;",
    ]
    allowlist = residual.load_allowlist(f"re:{_NARROWED_UNIT_NAME_GLOB}\n")

    assert residual.find_residual(hits, allowlist) == []


def test_the_narrowed_unit_name_glob_no_longer_covers_a_pack_artifact() -> None:
    """Test 12c (X-2, RED case -- the defect itself).

    These three lines are what docs/cli-development.md said before plan 03.1-13 measured
    the packer (`bun pm pack --dry-run` and `npm pack --dry-run` both emit
    `iforge-cli-0.13.47.tgz`) and rewrote them. The residual guard cannot prove this
    direction any more, because the lines no longer exist in the tree -- so a green guard is
    NOT evidence the narrowing worked, and this injected case is the only thing that is.
    """
    hits = [
        "docs/cli-development.md:73:tar -tzf clawdi-*.tgz | head",
        "docs/cli-development.md:74:bun install -g ./clawdi-*.tgz",
        "docs/cli-development.md:126:  bun install -g /tmp/clawdi-*.tgz",
    ]
    allowlist = residual.load_allowlist(f"re:{_NARROWED_UNIT_NAME_GLOB}\n")

    assert residual.find_residual(hits, allowlist) == hits


def test_the_real_allowlist_carries_the_narrowed_unit_name_glob() -> None:
    """Test 12d. Binds the two directional cases above to the shipped authority: they prove
    a property of a pattern, and only this assertion proves the shipped file uses it.
    Re-widening the entry back to the bare substring fails here rather than silently.
    """
    entries = residual.load_allowlist(residual.ALLOWLIST.read_text(encoding="utf-8"))

    assert ("re", _NARROWED_UNIT_NAME_GLOB) in entries
    assert ("sub", "clawdi-*") not in entries


def test_the_fences_plan_13_retracted_are_gone_from_the_real_allowlist() -> None:
    """Test 12e. Both entries fenced a DOCUMENTED COMMAND that no longer resolved:
    `bun link clawdi` linked by a package name plan 04 moved to `@iforge/cli`, and the
    `.tgz` regex held the two pack-artifact lines that carry no glob. Each was retracted in
    the same change as its fix (D-10). A reintroduced entry re-fences a fixed defect.
    """
    entries = residual.load_allowlist(residual.ALLOWLIST.read_text(encoding="utf-8"))

    assert ("sub", "bun link clawdi") not in entries
    assert ("re", r"clawdi-[^\s`\"']*\.tgz") not in entries


# The C-4 widened wire-key regex as plan 03.1-13 narrowed it. One constant, so the two
# directional cases and the binding assertion cannot drift apart.
_NARROWED_WIRE_KEY_REGEX = r"(?<!@)clawdi\.[a-z][A-Za-z0-9-]*"


def test_the_narrowed_wire_key_regex_still_covers_every_shape_its_comment_names() -> None:
    """Test 12f (C-4, green direction). The entry's own comment names the shapes it exists
    for. Narrowing it must not cost a single one of them -- un-fencing a real wire key is
    the failure mode with teeth here, because these are pydantic discriminators persisted in
    Postgres JSONB and in on-disk runtime state.
    """
    hits = [
        "docs/ai-provider-agent-contract-audit.md:41:`clawdi.hosted-runtime.manifest.v1`."
        " The running CLI strictly parses that shape",
        "docs/managed-runtime.md:466:`Accept: application/vnd.clawdi.runtime-bundle.v2+json`"
        " and returns strict",
        "docs/designs/channel-runtime-manifest.md:307:   `creds.additionalData"
        '["clawdi.managedWhatsAppSocket"]` and writes that one',
        "docs/designs/channel-runtime-manifest.md:339:iforge runtime plan -f clawdi.runtime.yaml",
        "docs/adr/0004-keep-bun-for-cli-tests.md:131:  `dist` and `bin/clawdi.mjs` with Node,"
        " rather than by moving Bun-specific build",
        "apps/web/src/hosted/billing/billing-client.ts:128:"
        '\t\t"type.googleapis.com/clawdi.v2.DeploymentOperationResponse"',
    ]
    allowlist = residual.load_allowlist(f"re:{_NARROWED_WIRE_KEY_REGEX}\n")

    assert residual.find_residual(hits, allowlist) == []


def test_the_narrowed_wire_key_regex_no_longer_covers_rendered_contact_text() -> None:
    """Test 12g (C-4, RED case -- the defect phase 03.1 verification found).

    A hostname has the same shape as a namespaced wire key, so a regex written for
    `clawdi.<lowercase>` silently covered the support address the app RENDERS in its Help
    menu. An at-sign is the one thing that separates them: every shape the entry exists for
    is preceded by a separator, a dot, a quote or a space, and an email address is not.

    The residual guard cannot prove this direction. The addresses are still in the tree --
    plan 13 did not rename them, because the user ruled the eventual support contact belongs
    to a company identity that does not exist yet -- and they are held by the X-6 `clawdi.ai`
    fence instead. A green guard is therefore consistent with the narrowing having failed;
    only this case distinguishes the two.
    """
    hits = [
        "apps/web/src/components/app-sidebar.tsx:1380:\t\t\t\tsupport@clawdi.ai",
        "apps/web/src/pages/gate/deactivated-page.tsx:23:"
        '\t\t\t\t\trender={<a href="mailto:support@clawdi.ai" />}',
        "backend/.env.example:33:DEV_AUTH_EMAIL=dev@clawdi.local",
    ]
    allowlist = residual.load_allowlist(f"re:{_NARROWED_WIRE_KEY_REGEX}\n")

    assert residual.find_residual(hits, allowlist) == hits


def test_the_real_allowlist_carries_the_narrowed_wire_key_regex() -> None:
    """Test 12h. Binds the two cases above to the shipped authority. Re-widening the entry
    back to the bare `clawdi\\.[a-z]...` form re-fences the rendered contact text and fails
    here rather than silently.
    """
    entries = residual.load_allowlist(residual.ALLOWLIST.read_text(encoding="utf-8"))

    assert ("re", _NARROWED_WIRE_KEY_REGEX) in entries
    assert ("re", r"clawdi\.[a-z][A-Za-z0-9-]*") not in entries


def test_the_deferred_support_contact_is_still_fenced_by_the_hosted_domain_entry() -> None:
    """Test 12i. The other half of the ruling, and its premise has since expired -- so what
    this case pins today is not what it was written to pin. Read the whole docstring before
    changing it; the assertions below are correct and the reasoning around them was not.

    AS WRITTEN (plan 03.1-13): plan 13 narrowed C-4 without renaming the support address, which
    was only safe because the X-6 `clawdi.ai` entry still covered the eight rendered
    `support@clawdi.ai` sites. The three strings below were copies of real lines in the tree.

    AS IT STANDS: there is no `support@clawdi.ai` anywhere in the scanned surface.
    ``packages/shared/src/destinations.ts`` carries ``supportEmail = "support@iforge.dev"`` and
    plan 03.3-07 moved every render site onto it. The 06.1 upstream sync briefly made that
    statement false again (the contact_support CTA in hosted-agent-detail.tsx). Quick task
    261002-dx9 moved that site onto DESTINATIONS.supportEmail, and the statement is now pinned
    for non-test apps/web/src by the describe block "06.1 UI review: retired upstream support
    address" in apps/web/src/lib/web-destinations-contract.test.ts, not by this docstring.
    The three strings are therefore SYNTHETIC FIXTURES now, and keeping them is deliberate:
    they are the only way to exercise this direction with a SUPPORT ADDRESS, because the tree
    no longer supplies one. The tree does now supply one at-sign occurrence of the domain: that
    describe block's own needle (web-destinations-contract.test.ts:372). It is live proof of
    this case's property, because the X-6 entry is its only fence (measured 2026-10-02 by quick
    261002-dx9: 1 at-sign hit, fenced by the full allowlist, unfenced without X-6).

    WHAT IT ACTUALLY GUARDS, which is a property the tree cannot demonstrate on its own: C-4's
    ``(?<!@)`` lookbehind refuses the one position an email address occupies, so an at-sign
    occurrence of the domain has exactly ONE possible fence in the whole file -- the X-6
    `clawdi.ai` substring entry asserted present below. Test 12g proves the narrowing from the
    other side (that C-4 does NOT cover these); this case proves the allowlist as a whole still
    does. Between them the two cases pin a coverage boundary, not a tree state, which is why
    this one survived its own premise.

    Plan 03.3-24 re-measured the X-6 scope against the tree after this phase's gap-closure set
    and kept it DEFERRED -- sole-fence 0, but seven documentation destinations under it still
    send a reader to a host this fork does not own -- so the entry asserted here is a live
    fence with a stated reason, not an expired one. Whoever eventually retires it deletes this
    case in the same change, and in that change re-fences or deletes the needle above; a test
    whose premise has expired is removed, never weakened. The function NAME is left as it is:
    it records where the case came from.
    """
    entries = residual.load_allowlist(residual.ALLOWLIST.read_text(encoding="utf-8"))
    rendered = [
        "apps/web/src/components/app-sidebar.tsx:1380:\t\t\t\tsupport@clawdi.ai",
        "apps/web/src/hosted/billing/components/compute-dunning-banner.tsx:131:"
        '\t\t\t\t\t\t\trender={<a href="mailto:support@clawdi.ai" />}',
        "apps/web/src/pages/gate/access-unavailable-page.tsx:66:"
        '\t\t\t\t\trender={<a href="mailto:support@clawdi.ai" />}',
    ]

    assert ("sub", "clawdi.ai") in entries
    assert residual.find_residual(rendered, entries) == []


def test_the_dev_auth_bypass_identity_is_consistent_across_every_default() -> None:
    """Test 12j. The dev-auth bypass builds one identity from three independent defaults,
    and a fourth site EXPORTS the variable, which overrides the backend default entirely.
    Renaming a subset leaves the backend issuing one address while the frontend renders
    another -- a silent mismatch, not a failure. Plan 13 moved all five together; this pins
    them so a future edit cannot move four.
    """
    sites = {
        "backend/.env.example": "DEV_AUTH_EMAIL=dev@iforge.local",
        "backend/app/core/config.py": 'dev_auth_email: str = "dev@iforge.local"',
        "scripts/paseo/dev.sh": 'export DEV_AUTH_EMAIL="${DEV_AUTH_EMAIL:-dev@iforge.local}"',
        "apps/web/src/lib/env.ts": (
            'VITE_DEV_AUTH_EMAIL: z.string().min(1).default("dev@iforge.local")'
        ),
        "apps/web/src/pages/auth/dev-auth-bypass-page.tsx": ">dev@iforge.local<",
    }

    for path, expected in sites.items():
        text = (residual.ROOT / path).read_text(encoding="utf-8")
        assert expected in text, f"{path} lost the shared dev-auth identity"
        assert "dev@clawdi.local" not in text, f"{path} still carries the old dev identity"


def test_git_grep_finding_nothing_is_success_not_an_error() -> None:
    """Test 14. ``git grep`` exits 1 when it matches nothing, which is the state this phase is
    driving towards. Treating a non-zero exit as failure would invert the guard at the finish
    line; treating every non-zero exit as success would hide a broken git."""
    assert residual.collect_hits(run=lambda *a, **k: _completed(1)) == []


def test_a_real_git_failure_is_not_mistaken_for_a_clean_tree() -> None:
    """The other half of test 14: exit code 128 is git failing, not the tree being clean."""
    with pytest.raises(residual.ResidualError, match="not a git repository"):
        residual.collect_hits(run=lambda *a, **k: _completed(128, stderr="not a git repository"))


@_NEEDS_GIT
def test_the_real_scanner_is_clean_against_the_real_tree() -> None:
    """Test 13, the load-bearing one. With the allowlist final, this is BRAND-01's own
    wording: zero residual in the brand surface, proven by a committed scanner rather than by
    review."""
    assert residual.scan() == []


# ---------------------------------------------------------------------------
# Plan 08 -- the final allowlist, and the tripwire that keeps it final.
# ---------------------------------------------------------------------------

_CITATION = re.compile(r"[CXP]-[0-9]")

_ALLOWLIST_TEXT = residual.ALLOWLIST.read_text(encoding="utf-8")


def test_load_allowlist_rejects_an_entry_under_a_temporary_section_heading() -> None:
    """Test A (red). A temporary exemption must not outlive the plan that added it.

    Every plan in phase 03.1 leaned on a temporary section while its own surface was still
    mid-rename. That is a legitimate staging device exactly once; a temporary exemption nobody
    removes is indistinguishable from a permanent carve-out nobody decided (T-03.1-34), and it
    silently hides a later rename of the very identifier it was fencing. The loader therefore
    refuses to parse one at all.
    """
    text = "\n".join(
        [
            "# ===========================================================",
            "# INTERIM -- removed by a named later plan.",
            "# ===========================================================",
            "",
            "# Removed by plan 99.",
            "re:CLAWDI_[A-Z0-9_]+",
        ]
    )

    with pytest.raises(residual.ResidualError, match=r"CLAWDI_\[A-Z0-9_\]\+"):
        residual.load_allowlist(text)


def test_the_real_allowlist_carries_no_surviving_temporary_exemption() -> None:
    """Test B. The assertion that closes the phase: the real file parses, so no entry in it
    sits under a temporary heading."""
    assert residual.load_allowlist(_ALLOWLIST_TEXT)


def test_the_real_allowlist_names_no_temporary_section_at_all() -> None:
    """The tripwire is only load-bearing if the marker is absent from the authority itself."""
    assert "INTERIM" not in _ALLOWLIST_TEXT


# ---------------------------------------------------------------------------
# Plan 11 -- the tripwire that cannot be walked around, and the run that cannot
# report a result it did not establish.
# ---------------------------------------------------------------------------
#
# Phase 03.1 verification proved by EXECUTION -- not by reading -- that the temporary-marker
# tripwire above is defeated by a blank line followed by a bare comment. Two of the four
# fragments it tried fired correctly and two were parsed. All four are pinned below, so the
# next editor sees the boundary rather than inferring it. The property being asserted is that
# the marker is refused ANYWHERE IN THE FILE while entries exist, not merely in the section
# that is open at the moment an entry is read: a tripwire whose reach depends on where a
# banner sits is a tripwire that moving a banner disarms.

_TEMPORARY_SECTION_HEAD = "# ====\n# INTERIM: staging while plan 99 lands.\n"


def test_a_temporary_marker_fires_when_the_entry_follows_the_heading_directly() -> None:
    """Control 1 of the verification table. Already true before plan 11; kept as the floor."""
    with pytest.raises(residual.ResidualError):
        residual.load_allowlist(f"{_TEMPORARY_SECTION_HEAD}some-entry\n")


def test_a_temporary_marker_fires_across_a_blank_line() -> None:
    """Control 2 of the verification table. Already true before plan 11; kept as the floor."""
    with pytest.raises(residual.ResidualError):
        residual.load_allowlist(f"{_TEMPORARY_SECTION_HEAD}\nsome-entry\n")


def test_a_temporary_marker_is_not_reset_by_a_bare_comment_line() -> None:
    """Evasion 1, proven parseable at HEAD by the verifier.

    A blank line followed by a line of nothing but ``#`` matches ``_SECTION_BANNER`` and so
    opened a fresh section, discarding the marker that the section above it carried. The entry
    beneath was then read as an ordinary permanent carve-out.
    """
    with pytest.raises(residual.ResidualError):
        residual.load_allowlist(f"{_TEMPORARY_SECTION_HEAD}\n#\nsome-entry\n")


def test_a_temporary_marker_is_not_reset_by_a_short_rule() -> None:
    """Evasion 2, proven parseable at HEAD by the verifier. Same mechanism as evasion 1."""
    with pytest.raises(residual.ResidualError):
        residual.load_allowlist(f"{_TEMPORARY_SECTION_HEAD}\n# ---\nsome-entry\n")


def test_a_temporary_heading_with_no_entries_beneath_it_stays_legal() -> None:
    """The hardening must not outlaw the shape the real file is actually in.

    Plan 08 emptied the staging section and left its retirement record behind as prose. That
    record is the audit trail of what was retired and by which plan; a file-wide refusal that
    also refused an entry-less heading would force a later editor to delete the history in
    order to make the guard green, which is the opposite of what the tripwire is for.
    """
    assert residual.load_allowlist(f"{_TEMPORARY_SECTION_HEAD}\n# ---\n") == []


def test_the_coverage_check_refuses_an_empty_repository_enumeration() -> None:
    """An empty enumeration is not the same claim as 'every entry is covered'.

    ``unclassified_entries`` returned ``[]`` for a repository git reported as having no files
    at all -- indistinguishable, at the call site in ``main()``, from a tree in which every
    entry really is classified. A guard must not report a result it did not establish.
    """
    with pytest.raises(residual.ResidualError, match="did not establish"):
        residual.unclassified_entries(run=lambda *a, **k: _completed(0, stdout=""))


def test_the_scan_refuses_a_pathspec_that_matches_almost_nothing() -> None:
    """The vacuous pass, one level up from the broken-git case the module docstring names.

    ``git grep`` exiting 1 is mapped to "no hits", which is correct when the pathspec really
    did read the surface. Against a pathspec that matches nothing -- a typo, a moved
    directory, an ``:(exclude)`` that swallowed its own siblings -- the same code path prints
    the success line having read no file at all.
    """
    with pytest.raises(residual.ResidualError, match="did not establish"):
        residual.assert_scan_is_not_vacuous(run=lambda *a, **k: _completed(0, stdout="README.md\0"))


@_NEEDS_GIT
def test_the_scan_floor_is_satisfied_by_the_real_tree() -> None:
    """The other half: the floor is a floor, not a wall the ordinary tree walks into."""
    assert residual.assert_scan_is_not_vacuous() >= residual._MINIMUM_SCANNED_FILES


def test_the_hit_scan_reads_files_that_are_not_committed_yet() -> None:
    """WR-06. A sweep is judged before it is committed, which is when git cannot see it.

    Asserted through the injected ``run`` seam by inspecting the argv the guard hands git,
    rather than by reading the source: the property is what the subprocess is asked to do.
    """
    argv: list[list[str]] = []
    residual.collect_hits(run=lambda *a, **k: (argv.append(a[0]), _completed(1))[1])

    assert "--untracked" in argv[0], f"the hit scan is still tracked-only: {argv[0]}"


def test_the_coverage_enumeration_reads_the_same_set_the_hit_scan_does() -> None:
    """The coverage claim is only about the set it enumerated, so the two sets must agree."""
    argv: list[list[str]] = []
    residual._ls_files([], run=lambda *a, **k: (argv.append(a[0]), _completed(0, stdout=""))[1])

    assert "--others" in argv[0], f"the coverage enumeration is still tracked-only: {argv[0]}"
    assert "--exclude-standard" in argv[0], f"ignored files would be enumerated: {argv[0]}"


def test_every_allowlist_entry_cites_a_carve_out_id() -> None:
    """Test C. An unexplained exemption is indistinguishable from a bug (T-03.1-35).

    Every entry must sit in a section whose comments name a carve-out id (``C-n``), a deferral
    id (``X-n``) or a project-rule id (``P-n``). That citation is what lets the next phase to
    touch one of these surfaces find the decision instead of guessing at it.
    """
    uncited = [
        entry
        for section, entry in residual.iter_allowlist_entries(_ALLOWLIST_TEXT)
        if not _CITATION.search(section)
    ]

    assert uncited == []


# ---------------------------------------------------------------------------
# Plan 11 -- a fence must say what kind of fence it is.
# ---------------------------------------------------------------------------
#
# The citation rule above asks "which decision keeps this entry". It cannot ask the question
# phase 03.1 verification found unanswered: whether the entry fences something that is never
# expected to move, or something that IS expected to move and has not moved yet. Three entries
# in this file were measured to be fencing DEFECTS -- documentation that tells a developer to
# run commands that no longer resolve -- and they were indistinguishable from the carve-outs
# beside them, because nothing in the grammar could tell the two apart.
#
# A declaration governs the entries beneath it until the next blank-line-separated comment run
# or the next section banner, whichever comes first. That is the granularity the file already
# writes in: its two largest sections are umbrellas over sub-blocks that each carry their own
# citation and each belong to a different class.

_PERMANENT = "# CLASS: PERMANENT"
_DEFERRED = "# CLASS: DEFERRED -> 03.1-13"


# ---------------------------------------------------------------------------
# CR-02 -- the shape the allowlist does not get a vote on.
# ---------------------------------------------------------------------------

_INSTALL_PIPELINES = [
    "README.md:1:curl -sSL https://clawdi.ai/install.sh | sh",
    "README.md:1:curl -fsSL https://clawdi.ai/install.sh | bash",
    "README.md:1:wget -qO- https://clawdi.ai/install.sh | sh",
    "README.md:1:curl -fsSL https://clawdi.ai/install.sh | IFORGE_HOME=/opt sh",
    "README.md:1:curl -fsSL https://clawdi.ai/install.sh | zsh",
]


@pytest.mark.parametrize("hit", _INSTALL_PIPELINES)
def test_no_allowlist_entry_can_fence_an_upstream_install_pipeline(hit: str) -> None:
    """Red, CR-02. This fork's front page piping upstream's installer into a shell.

    The first three were MEASURED to pass this scanner before the rule existed, because two
    separate entries cover ``clawdi.ai`` inside a URL: the X-6 domain substring and the C-4
    wire-key regex. Narrowing either one left the other covering the pipeline, which is why the
    refusal is a property of the SCANNER rather than of an entry. ``find_residual`` is given the
    REAL allowlist so the assertion is about the authority in force, not a fixture.
    """
    assert residual.unfenceable(hit) != ""
    assert residual.find_residual([hit], residual.load_allowlist(_ALLOWLIST_TEXT)) == [hit]


@pytest.mark.parametrize(
    "hit",
    [
        'README.md:15:  <a href="https://clawdi.ai">Website</a>',
        "config/deploy.yml:304:    PUBLIC_API_URL: https://cloud-api.clawdi.ai",
        "docs/x.md:218:| Connectors | `mcp.clawdi.ai` directly | shared MCP server |",
        "README.md:64:curl -sS http://localhost:8000/health",
        'apps/web/src/pages/gate/deactivated-page.tsx:23:href="mailto:support@clawdi.ai"',
    ],
)
def test_the_unfenceable_rule_refuses_nothing_but_the_pipeline(hit: str) -> None:
    """Green. A tripwire that also fires on a hostname or a health check is a sweep, not a rule."""
    assert residual.unfenceable(hit) == ""


@_NEEDS_GIT
def test_the_unfenceable_rule_matches_no_hit_in_the_real_tree() -> None:
    """Measured, not assumed: the rule must add nothing to the current residual count.

    Asserted against the LIVE tree rather than against the hit count recorded in the guard's
    docstring, because the property that matters is that the rule stays a tripwire as the tree
    changes -- a hand-copied number would stop being evidence the first time one moved.
    """
    assert [hit for hit in residual.collect_hits() if residual.unfenceable(hit)] == []


def test_a_permanent_declaration_parses_and_keeps_its_entries() -> None:
    """Test 1. Declaring a classification must not change what the allowlist covers."""
    text = f"# ====\n# C-1 -- a durable carve-out.\n{_PERMANENT}\nan-entry\n"

    assert residual.load_allowlist(text) == [("sub", "an-entry")]
    assert residual.unclassified_sections(text) == []


def test_a_deferred_declaration_is_reported_with_the_phase_that_receives_it() -> None:
    """Test 2. A deferral is only a deferral if somebody is named to take it."""
    text = f"# ====\n# X-9 -- moves later.\n{_DEFERRED}\nan-entry\n"
    classified = residual.classify_entries(text)

    assert [(entry, kind, receiving) for _, entry, kind, receiving, _ in classified] == [
        ("an-entry", "deferred", "03.1-13")
    ]
    assert residual.unclassified_sections(text) == []


def test_a_section_that_declares_neither_classification_is_reported() -> None:
    """Test 3 (red). The whole point: a fence that does not say what it fences fails."""
    text = "# ====\n# a reason that declares nothing\nan-entry\n"

    assert residual.unclassified_sections(text) == [
        ("# a reason that declares nothing", "an-entry")
    ]


def test_an_entry_appended_after_a_blank_line_does_not_inherit_the_declaration_above_it() -> None:
    """Test 3b (red, CR-01). The reset must fire for an ENTRY run, not only a COMMENT run.

    Until this case existed the reset in ``classify_entries`` fired only when the next
    blank-line-separated run opened with a comment. Every red case above happens to take that
    shape, which is why the cheapest possible evasion survived: a blank line and a bare entry
    appended at the foot of the file carried the previous run's declaration forward and parsed
    as a declared, permanent carve-out. Appending ``re:.`` that way was measured to make
    ``find_residual`` report 0 uncovered of 1,091 hits over an entirely unswept tree while
    ``unclassified_sections`` reported nothing at all -- a guard reporting a result it had not
    established, which is the one property the module docstring says it must never have.
    """
    text = f"# ====\n# C-1 -- a durable carve-out.\n{_PERMANENT}\nan-entry\n\nre:.\n"
    classified = residual.classify_entries(text)

    assert [(entry, kind) for _, entry, kind, _, _ in classified] == [
        ("an-entry", "permanent"),
        ("re:.", ""),
    ]
    assert residual.unclassified_sections(text) == [("re:.", "re:.")]


def test_a_deferred_declaration_also_stops_at_a_blank_line_before_an_entry() -> None:
    """The same reset, proven against the other declaration kind.

    A deferral that leaked across a blank line would hand an undeclared entry a receiving
    phase, which is worse than handing it PERMANENT: it reads as scheduled work that nobody
    ever scheduled, and the phase named in the arrow would retract a fence it never installed.
    """
    text = f"# ====\n# X-9 -- moves later.\n{_DEFERRED}\nan-entry\n\nanother-entry\n"

    assert [
        (entry, kind, receiving) for _, entry, kind, receiving, _ in residual.classify_entries(text)
    ] == [("an-entry", "deferred", "03.1-13"), ("another-entry", "", "")]
    assert residual.unclassified_sections(text) == [("another-entry", "another-entry")]


def test_the_real_allowlist_has_no_entry_run_that_opens_after_a_blank_line() -> None:
    """The green counterpart: the reset above changes nothing about the real authority.

    Measured rather than asserted from memory, because the reset would silently reclassify
    every entry beneath such a run if one existed, and the failure mode of getting this wrong
    is a guard that goes red for a file nobody edited.
    """
    previous_blank = True
    orphans: list[str] = []
    for raw in _ALLOWLIST_TEXT.splitlines():
        line = raw.strip()
        if not line:
            previous_blank = True
            continue
        if not line.startswith("#") and previous_blank:
            orphans.append(line)
        previous_blank = False

    assert orphans == [], "entry runs that open after a blank line and now reset the declaration"


def _stub_git_backed_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralise the four git-dependent calls ``main()`` makes before the check under test.

    WR-06. This test used to be gated behind ``_NEEDS_GIT``, which made it the ONE gated test
    that the host-side guard run cannot compensate for: it is the only exit-code-level RED path
    of the classification gate, and running the guard green against an already-clean allowlist
    establishes nothing about how it behaves against a dirty one. So in the canonical suite the
    newest guard behaviour had no executed red path at all.

    It needed git only incidentally -- not for anything it asserts, but because ``main()``
    performs its enumerations before reaching the undeclared-section check. Stubbing exactly
    those four leaves the control flow under test untouched: the parse, the check, the message
    and the exit code are all still the real ones.
    """
    monkeypatch.setattr(residual, "assert_scan_is_not_vacuous", lambda: None)
    monkeypatch.setattr(residual, "unclassified_entries", lambda: [])
    monkeypatch.setattr(residual, "casing_violations", lambda: [])
    monkeypatch.setattr(residual, "scan", lambda: [])


def _allowlist_file(text: str) -> Path:
    """Write ``text`` to a temporary file, for repointing ``residual.ALLOWLIST`` at.

    A red case proven by corrupting the authority on disk leaves the repository one crashed
    test away from a silently weakened guard, which is why every red case here injects instead.
    """
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as handle:
        handle.write(text)
        return Path(handle.name)


def test_the_scanner_exits_non_zero_on_an_undeclared_section(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test 3, at the exit code. A pure function nobody calls is not a guard."""
    temporary = _allowlist_file("# ====\n# a reason that declares nothing\nan-entry\n")
    monkeypatch.setattr(residual, "ALLOWLIST", temporary)
    _stub_git_backed_calls(monkeypatch)
    try:
        with pytest.raises(SystemExit, match="allowlist sections fence entries"):
            residual.main()
    finally:
        temporary.unlink()


def test_the_scanner_exits_zero_when_every_section_declares(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The green control for the test above, and the reason its red result means anything.

    Without it the stubs are the suspect: a ``main()`` that raised SystemExit for some unrelated
    reason would satisfy the red case just as well. Same stubs, same repointed allowlist, one
    declaration added -- and main() must now run to completion.
    """
    temporary = _allowlist_file(f"# ====\n# a real reason.\n{_PERMANENT}\nan-entry\n")
    monkeypatch.setattr(residual, "ALLOWLIST", temporary)
    _stub_git_backed_calls(monkeypatch)
    try:
        residual.main()
    finally:
        temporary.unlink()


def test_the_scanner_exits_non_zero_on_an_entry_that_inherited_across_a_blank_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CR-01 at the exit code. The shape that silently covered the whole tree.

    The unit-level assertion for this lives beside the other classify_entries cases; this is the
    same input reaching the same exit path a developer actually sees, which is what makes the
    classification gate a guard rather than a function.
    """
    temporary = _allowlist_file(f"# ====\n# a real reason.\n{_PERMANENT}\nan-entry\n\nre:.\n")
    monkeypatch.setattr(residual, "ALLOWLIST", temporary)
    _stub_git_backed_calls(monkeypatch)
    try:
        with pytest.raises(SystemExit, match="allowlist sections fence entries"):
            residual.main()
    finally:
        temporary.unlink()


def test_a_deferral_that_names_no_receiving_phase_is_reported() -> None:
    """Test 4. A deferral with no destination is the exact defect this rule exists to catch.

    It reads as a decision -- 'this moves later' -- while committing nobody to move it, which
    is how a fence over an unfixed defect survives a phase looking like a carve-out.
    """
    text = "# ====\n# X-9 -- moves later.\n# CLASS: DEFERRED ->\nan-entry\n"

    assert residual.unclassified_sections(text) == [("# X-9 -- moves later.", "an-entry")]


def test_the_real_allowlist_declares_a_classification_for_every_entry() -> None:
    """Test 5, the load-bearing one. No entry in the authority fences anything silently."""
    assert residual.unclassified_sections(_ALLOWLIST_TEXT) == []


def test_every_declaration_in_the_real_allowlist_governs_at_least_one_entry() -> None:
    """The other direction, which is what makes the rule a bijection rather than a floor.

    Both numbers are derived from the parse rather than hand-counted: the phase has been wrong
    about a hand count eight times. A declaration that governs nothing is a classification of
    the empty set -- it reads as coverage that is not there, exactly like a stale OUT_OF_SCOPE
    entry, and it is how a section could be emptied while still looking declared.
    """
    governing = {
        declaration for _, _, _, _, declaration in residual.classify_entries(_ALLOWLIST_TEXT)
    }
    written = {
        line.strip()
        for line in _ALLOWLIST_TEXT.splitlines()
        if any(marker in line for marker in residual._CLASSIFICATION_MARKERS)
    }

    assert written - governing == set(), "declaration lines that govern no entry"
    assert governing - written == set(), "governing declarations absent from the file"


def test_the_classification_rule_reads_the_same_entries_the_loader_does() -> None:
    """A second parse that disagreed with the loader would classify a different file."""
    classified = [entry for _, entry, _, _, _ in residual.classify_entries(_ALLOWLIST_TEXT)]
    loaded = [entry for _, entry in residual.iter_allowlist_entries(_ALLOWLIST_TEXT)]

    assert classified == loaded


@_NEEDS_GIT
def test_every_repository_entry_is_either_scanned_or_explicitly_out_of_scope() -> None:
    """The pathspec is an opt-in list, so anything new is invisible to the guard by default.

    Twice in this phase a directory inside the brand surface turned out to sit outside the
    pathspec and was found by hand rather than by the guard (WINDOWS #29 ``scripts/``, #32
    ``apps/web/e2e``). The pathspec being incomplete is not the defect -- deciding what it
    omits is a real decision -- but its omissions being *unrecorded* is. Every entry under a
    scanned root must therefore be classified, so a directory added by a later phase fails this
    test until somebody decides which side of D-01's boundary it is on.
    """
    unclassified = residual.unclassified_entries()

    assert unclassified == []


# ---------------------------------------------------------------------------
# Plan 11 -- coverage honesty: the compensating control, the env-example surface, and D-04.
# ---------------------------------------------------------------------------

_CLI_HELP_CONTRACT = "packages/cli/tests/cli-help-brand-contract.test.ts"


def test_the_excluded_cli_source_tree_names_the_control_that_covers_its_strings() -> None:
    """Verification root cause 2, closed by record rather than by rediscovery.

    ``packages/cli/src`` is correctly outside the pathspec for IDENTIFIERS (D-01 keeps them
    upstream-aligned so the fork can keep merging). It is also where ``iforge --help`` is
    written, so the exclusion made the CLI's own self-description invisible to the guard
    BRAND-01 names as its proof. The exclusion is now justified by a named file that can be
    checked rather than by a comment that has to be believed -- and the file must EXIST, so
    the record cannot rot into a reference to something deleted.
    """
    reason = residual.OUT_OF_SCOPE["packages/cli/src"]

    assert _CLI_HELP_CONTRACT in reason, "the compensating control is not recorded by path"
    assert (residual.ROOT / _CLI_HELP_CONTRACT).exists(), "the recorded control does not exist"


@_NEEDS_GIT
def test_every_env_example_in_the_repository_is_inside_the_scanned_set() -> None:
    """WINDOWS #40. The guard that polices the env contract must read what it is derived from.

    ``*.env.example`` is membership source 1 of config/rebrand-env-contract.txt. Asserted by
    set difference rather than by naming files, so an env-example added by a later phase in a
    directory nobody thought about is caught by the same assertion.
    """
    all_examples = set(residual._ls_files(["*.env.example"]))
    scanned = set(residual._ls_files(list(residual.SCAN_PATHSPEC)))

    assert all_examples, "no env-example files found at all -- the probe itself is vacuous"
    assert sorted(all_examples - scanned) == []


def test_a_mixed_case_spelling_of_the_product_name_is_a_violation() -> None:
    """D-04 says the product name is lowercase in prose, UI and docs, everywhere.

    Until now that rule was enforced by a verifier happening to run a grep by hand. A rule
    nothing executes is a convention, and a convention survives exactly as long as the next
    person remembers it.
    """
    grep = "README.md:3:Install IForge today, then run iForge setup.\n"

    assert residual.casing_violations(run=lambda *a, **k: _completed(0, stdout=grep)) == [
        "README.md:3:Install IForge today, then run iForge setup."
    ]


def test_the_uppercase_environment_register_is_not_a_casing_violation() -> None:
    """The other half, and the one that decides whether the rule is usable at all.

    ``IFORGE_API_URL`` and ``VITE_IFORGE_HOSTED`` are SCREAMING_SNAKE register names, not
    prose. Measured across the scanned surface at the commit that added this: 241 uppercase
    occurrences, every one of them inside such a name, and zero mixed-case spellings. A rule
    that flagged the register would have to be switched off on the day it was written.
    """
    grep = "install.sh:12:export IFORGE_VERSION=1 VITE_IFORGE_API_URL=x  # iforge\n"

    assert residual.casing_violations(run=lambda *a, **k: _completed(0, stdout=grep)) == []


def test_a_grep_failure_is_not_read_as_an_absence_of_casing_violations() -> None:
    """Same rule as ``collect_hits``: exit 1 is 'no match', anything else is git failing."""
    assert residual.casing_violations(run=lambda *a, **k: _completed(1)) == []
    with pytest.raises(residual.ResidualError, match="not a git repository"):
        residual.casing_violations(
            run=lambda *a, **k: _completed(128, stderr="not a git repository")
        )


@_NEEDS_GIT
def test_the_real_tree_spells_the_product_name_in_lower_case_everywhere() -> None:
    """D-04, as a committed check against the real surface rather than a remembered command."""
    assert residual.casing_violations() == []


def test_out_of_scope_entries_all_still_exist_and_carry_a_reason() -> None:
    """A stale exclusion is a fence around nothing, and reads as coverage that is not there.

    The public mirror drops the ``.planning/`` record, so only there are entries under it left
    out of the existence check. Every reason is still checked.
    """
    planning = planning_present(residual.ROOT)
    missing = [
        path
        for path in residual.OUT_OF_SCOPE
        if (planning or Path(path).parts[:1] != (".planning",))
        and not (residual.ROOT / path).exists()
    ]

    assert missing == []
    assert all(reason.strip() for reason in residual.OUT_OF_SCOPE.values())


def test_both_rebrand_guards_are_wired_into_the_canonical_test_run() -> None:
    """BRAND-03 is a standing property, not phase scaffolding (T-03.1-38).

    ``scripts/test.sh`` runs both guards host-side ahead of the compose build. That host-side
    invocation is the enforcement: the two scans below take their evidence from ``git grep``,
    and the clean runner deliberately copies the repository without ``.git``, so they cannot
    run inside the container. Asserting the wiring from inside the suite is what keeps a later
    phase from removing the guards as cleanup.
    """
    runner = (residual.ROOT / "scripts/test.sh").read_text(encoding="utf-8")

    assert "scripts/check_rebrand_carveouts.py" in runner
    assert "scripts/check_rebrand_residual.py" in runner


_ENV_CONTRACT = residual.ROOT / "config/rebrand-env-contract.txt"
_MIGRATION_RUNBOOK = residual.ROOT / "docs/runbooks/iforge-env-migration.md"
_CONTRACT_PAIR = re.compile(r"^((?:VITE_)?IFORGE_[A-Z0-9_]+)=((?:VITE_)?CLAWDI_[A-Z0-9_]+)$")


def test_the_migration_runbook_still_matches_the_env_contract() -> None:
    """The control that makes excluding the runbook from SCAN_PATHSPEC safe.

    The runbook is a specification of the rename rather than a carrier of it -- a rename table
    that did not name the old spelling would not be a rename table -- so the residual scan
    skips it, exactly as it skips config/rebrand-allowlist.txt. An unscanned file can drift,
    and a migration recipe that has drifted from the contract the code reads is worse than no
    recipe: it tells a developer to rename a variable to a name nothing looks for. Both sides
    of every pair, commented Vite keys included, must appear in it.
    """
    runbook = _MIGRATION_RUNBOOK.read_text(encoding="utf-8")
    pairs = [
        match.groups()
        for raw in _ENV_CONTRACT.read_text(encoding="utf-8").splitlines()
        if (match := _CONTRACT_PAIR.match(raw.strip().lstrip("#").strip()))
    ]

    # 47 uncommented runtime pairs + the 4 commented VITE_ build-time keys. Was 48 until phase
    # 03.1 plan 16 added IFORGE_STATE_DIR=CLAWDI_STATE_DIR; 06.1-03 added an AI-key pair (50) and
    # 06.1-04 retired it on user directive (no Clawdi-name aliases, 2026-09-30). 06.1-11 added
    # the two step-4 public names (R9): 47 runtime pairs + 4 VITE keys. 06.1-14 added the six
    # desktop release names (D-23): 53 runtime pairs + 4 VITE keys.
    assert len(pairs) == 57
    undocumented = [pair for pair in pairs if pair[0] not in runbook or pair[1] not in runbook]

    assert undocumented == []


_RUNTIME_ENTRYPOINT = residual.ROOT / "backend/app/runtime_entrypoint.py"
_BOUNDARY_CALLS = ("refuse_user_supplied_internal_names", "apply_brand_env_aliases")


def test_the_backend_entrypoint_refuses_before_it_applies() -> None:
    """BRAND-02 at the one boundary whose environment an operator writes by hand.

    Existence is not the property. A ``refuse_user_supplied_internal_names`` that is defined,
    exported and unit-tested but never invoked leaves the backend boundary additive while every
    other gate reports green -- so this asserts INVOCATION, at the single process entry the
    backend has (``main()``, reached by ``python -m app.runtime_entrypoint``).

    Order is asserted too, not just presence. Applying first copies public values into the
    internal register, after which a name this process just wrote cannot be told apart from one
    an operator supplied, and the refusal becomes a no-op that still looks wired.

    Parsed with ``ast`` rather than matched with a regex: a call inside a comment or a string is
    not a call, and this guard is the control that a later editor deleting the wiring turns a
    suite red rather than a plan stale.
    """
    source = _RUNTIME_ENTRYPOINT.read_text(encoding="utf-8")
    calls = sorted(
        (node.lineno, node.func.id)
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in _BOUNDARY_CALLS
    )
    names = [name for _, name in calls]

    assert "apply_brand_env_aliases" in names, f"the entrypoint no longer applies: {calls}"
    assert "refuse_user_supplied_internal_names" in names, (
        f"the refusal exists but is never invoked at the backend entrypoint: {calls}"
    )
    assert names.index("refuse_user_supplied_internal_names") < names.index(
        "apply_brand_env_aliases"
    ), f"the backend entrypoint applies the boundary before it refuses: {calls}"


# ---------------------------------------------------------------------------
# Plan 03.3-09 -- the brand-string classifier, red and green.
# ---------------------------------------------------------------------------
# `scripts/rewrite_brand_strings.py` is the durable owner of the MIGR-03 string class, and it
# is the first guard in this family with a WRITING mode. That raises the stakes on proving its
# refusals rather than describing them: a classifier whose blocking behaviour was never shown
# is a rewrite tool with a comment where its safety is supposed to be.
#
# Every case below injects `contents` or feeds `apply_to_text` a string. Nothing here reaches
# the working tree through the writing path, and nothing here writes to it. That is the same
# discipline the carve-out cases above follow, for the same two reasons: a red case that
# mutates the tree is unrunnable in parallel, and it leaves the repository one crashed test
# away from a real edit -- which for THIS script would be a real rewrite.
#
# The green cases that read the real tree use `helper_sources()`, which walks the filesystem
# rather than shelling out to git. They therefore run INSIDE the clean container, where the
# checkout arrives without repository metadata. The git-backed half of the same question --
# the scanned-set floor and the two-enumeration comparison -- is enforced host-side by
# `scripts/test.sh`, and `test_the_brand_string_classifier_is_wired_into_the_canonical_test_run`
# is what stops that host-side invocation being quietly deleted.

rewrite = _load_guard("rewrite_brand_strings")

_BRAND = rewrite.load_brand()
_SHAPE = rewrite.committed_invocation_shape()
_STATE_DIR = rewrite.declared_state_dir_name()


def _classify(contents: dict[str, str]) -> list[object]:
    """Classify injected contents against the real authorities, and nothing else."""
    return rewrite.classify_contents(contents, _BRAND, _SHAPE, _STATE_DIR)


def test_the_classifier_keeps_an_occurrence_that_matches_a_keep_class() -> None:
    """Red case 1. The wire header is matched byte-for-byte by the backend; it must not move."""
    contents = {
        "packages/cli/src/lib/skill-sync-protocol.ts": (
            'export const SKILL_SYNC_PROTOCOL_HEADER = "X-Clawdi-Skill-Sync-Protocol";\n'
        )
    }
    occurrences = _classify(contents)

    assert [occurrence.label for occurrence in occurrences] == [rewrite.KEEP_WIRE_HEADER]
    assert occurrences[0].replacement == ""
    assert not occurrences[0].is_replacement


def test_the_classifier_replaces_an_occurrence_that_matches_the_replacement_rule() -> None:
    """Green case 1, the other half of the pair above: the class this script exists for."""
    contents = {
        "packages/cli/src/commands/auth.ts": (
            '\t\tchalk.gray("  1. Sign in at the Clawdi Cloud dashboard\\n") +\n'
        )
    }
    occurrences = _classify(contents)

    assert [occurrence.label for occurrence in occurrences] == [rewrite.REPLACE_PRODUCT_NAME]
    assert occurrences[0].replacement == _BRAND["productName"]
    assert rewrite.apply_to_text(contents[occurrences[0].path], occurrences) == (
        '\t\tchalk.gray("  1. Sign in at the iforge Cloud dashboard\\n") +\n'
    )


def test_a_line_carrying_two_classes_yields_two_records_not_one() -> None:
    """Red case 2, and the reason records are per occurrence rather than per line.

    Quoted from `packages/cli/src/index.ts:73` as it read before the sweep: an example dotenv
    filename and a secret-reference scheme on one line, two facts in two classes. Until 06.1-11
    the scheme was a keep; it now moves under its own class (the fork writes and reads
    `iforge://`), so the line still yields two records with two different labels. A line-level
    classifier would make one decision about two facts.
    """
    line = (
        "  $ iforge run --env-file .env.clawdi -- npm run dev  Resolve clawdi:// refs at runtime\n"
    )
    contents = {"packages/cli/src/index.ts": line}
    occurrences = _classify(contents)

    assert len(occurrences) == 2
    assert [occurrence.label for occurrence in occurrences] == [
        rewrite.REPLACE_EXAMPLE_FILENAME,
        rewrite.REPLACE_REF_SCHEME,
    ]
    assert occurrences[0].column < occurrences[1].column
    # Both rewrites land on the same line in one pass.
    assert rewrite.apply_to_text(line, occurrences) == line.replace(
        ".env.clawdi", ".env.iforge"
    ).replace("clawdi://", "iforge://")


def test_an_occurrence_matching_no_rule_is_reported_unclassified_and_blocks_apply() -> None:
    """Red case 3, the load-bearing one.

    The rule set falls off its end rather than defaulting, and the apply path refuses on the
    result. Defaulting an unclassifiable occurrence to either action is how a blanket
    substitution gets in through the back door of a tool built to prevent one.
    """
    contents = {"packages/cli/src/lib/nowhere.ts": "const x = 1 clawdi 2;\n"}
    occurrences = _classify(contents)

    assert [occurrence.label for occurrence in occurrences] == [""]
    stranded = rewrite.unclassified(occurrences)
    assert len(stranded) == 1
    assert stranded[0].path == "packages/cli/src/lib/nowhere.ts"

    report = rewrite.render_report("sametree", ["packages/cli/src/lib/nowhere.ts"], occurrences)
    assert "unclassified 1" in report
    with pytest.raises(rewrite.RewriteError, match="apply is blocked"):
        rewrite.assert_apply_is_allowed(report, "sametree", occurrences)


def test_apply_refuses_a_report_produced_at_a_different_tree() -> None:
    """Red case 4. A stale report names offsets that have moved under it."""
    report = rewrite.render_report("oldtree", [], [])

    with pytest.raises(rewrite.RewriteError, match="re-run --classify before --apply"):
        rewrite.assert_apply_is_allowed(report, "newtree", [])


def test_apply_refuses_a_keep_bucket_that_names_no_reason() -> None:
    """Red case 5. A keep class nobody wrote down is a decision nobody made."""
    invented = rewrite.Occurrence(
        path="packages/cli/src/lib/x.ts",
        line=1,
        column=1,
        matched="clawdi",
        label="KEEP:invented-by-nobody",
        kind="token",
        replacement="",
        context=rewrite.CODE,
        line_text="clawdi",
    )
    report = rewrite.render_report("sametree", [], [invented])

    with pytest.raises(rewrite.RewriteError, match="names no reason in KEEP_REASONS"):
        rewrite.assert_apply_is_allowed(report, "sametree", [invented])


def test_the_brand_string_scan_refuses_a_pathspec_that_has_stopped_resolving() -> None:
    """Red case 6. The direction a hit-based guard cannot see.

    `packages/cli/src` sits outside `check_rebrand_residual.py`'s pathspec entirely, so nothing
    else in this repository would notice this script's scan going quiet. A run that read almost
    no files reports "0 unclassified" and means nothing by it.
    """
    stub = _completed(0, stdout="packages/cli/src/index.ts\0")

    with pytest.raises(rewrite.RewriteError, match="below the floor"):
        rewrite.scanned_files(run=lambda *a, **k: stub)


def test_the_brand_string_scan_floor_is_satisfied_by_the_real_tree() -> None:
    """The green half of the floor: the number is a smoke alarm, not a ratchet.

    Named for its own guard rather than reusing the residual scanner's test name above. The
    first draft of this file did reuse it, which silently REDEFINED that test out of the suite
    -- a collected count that stayed flat while a case disappeared, which is precisely the
    signal this suite is supposed to raise rather than emit.
    """
    assert len(rewrite.helper_sources()) >= rewrite._MINIMUM_SCANNED_FILES


def test_the_report_is_ordered_by_path_then_line_then_column_whatever_the_input_order() -> None:
    """Red case 7. Two runs at the same tree must produce byte-identical output.

    The contents mapping is fed in REVERSE path order, because a report that inherited its
    input's order would diff against itself the next time a mapping happened to arrive
    differently -- and a report that cannot be diffed is a report nobody reviews twice.
    """
    reversed_input = {
        "packages/cli/src/z.ts": "// `clawdi push` twice on one line: `clawdi pull`\n",
        "packages/cli/src/a.ts": "// leading\n// `clawdi setup`\n",
    }
    occurrences = _classify(reversed_input)
    keys = [(o.path, o.line, o.column) for o in occurrences]

    assert keys == sorted(keys)
    assert keys[0][0] == "packages/cli/src/a.ts"
    assert rewrite.render_report("t", [], occurrences) == rewrite.render_report(
        "t", [], _classify(reversed_input)
    )


def test_the_rule_set_places_every_occurrence_in_the_real_tree() -> None:
    """The green case that runs INSIDE the container, against the tree the runner copied in.

    `helper_sources()` walks the filesystem, so this needs no repository metadata. It is the
    half of the drift check the container can carry: when an upstream merge introduces a shape
    no rule covers, this goes red here rather than waiting for somebody to run the host suite.
    """
    occurrences = _classify(rewrite.helper_sources())

    stranded = rewrite.unclassified(occurrences)
    assert stranded == [], f"{len(stranded)} occurrences match no rule: {stranded[:5]}"
    # A floor against "applied to almost nothing", not a census. Upstream sync step 3 (06.1-08)
    # deleted CLI code carrying the old name (runtime convergence rewrite, #1229 adapters), so
    # the tree measured 981 occurrences before its sweep and 968 after; the floor sits below that.
    assert len(occurrences) > 900, "the rule set was applied to almost nothing"


def test_every_keep_the_real_tree_produces_names_a_reason() -> None:
    """A keep bucket with no stated reason is refused before it can reach the apply path."""
    rewrite.validate_vocabulary(_classify(rewrite.helper_sources()))


def test_the_classifier_agrees_with_the_committed_helper_on_the_bare_invocation_set() -> None:
    """Two implementations of one rule, compared. One implementation is a claim, not evidence.

    The regex is read out of `cli-help-brand-contract.test.ts` rather than copied here, so the
    day somebody edits that shape this comparison is what notices.
    """
    sources = rewrite.helper_sources()
    mine = sorted({f"{o.path}:{o.line}" for o in _classify(sources) if o.is_invocation_shape})
    theirs = sorted(rewrite.bare_invocation_lines(sources, _SHAPE))

    assert mine == theirs


def test_the_state_directory_the_sweep_would_write_is_the_one_the_cli_resolves() -> None:
    """The drift check for the printed paths this class exists to repair.

    `iforge auth login` writes to the resolved state root and prints a literal. If the two ever
    disagree the sweep would print a path nothing resolves, which is the defect it is fixing
    wearing the new name.
    """
    assert _STATE_DIR == f".{_BRAND['productName']}"


#: The ten deferred printed state paths plan 03.3-02 left for the scripted sweep, each located
#: by a substring that does NOT contain the legacy token so the anchor survives the sweep.
_DEFERRED_PRINTED_PATHS: tuple[tuple[str, str], ...] = (
    ("packages/cli/src/commands/auth.ts", "Credentials saved to ~/."),
    ("packages/cli/src/commands/serve-cli.ts", "Bearer token (preferred over ~/."),
    ("packages/cli/src/commands/serve-cli.ts", "Override location of queue.jsonl + health"),
    ("packages/cli/src/commands/serve.ts", "(missing ~/."),
    ("packages/cli/src/commands/serve.ts", "` to write ~/."),
    ("packages/cli/src/commands/teardown.ts", "is not registered (no ~/."),
    ("packages/cli/src/index.ts", "Read or write CLI configuration (~/."),
    ("packages/cli/src/lib/sessions-lock.ts", "is corrupted; resetting."),
    ("packages/cli/src/lib/skills-lock.ts", "is corrupted; resetting."),
    ("packages/cli/src/lib/state.ts", "is corrupted; resetting."),
)


def test_every_deferred_printed_state_path_is_owned_by_a_replacement_rule() -> None:
    """The sixteen strings plan 03.3-02 deliberately left for the scripted sweep.

    Ten of them print the legacy state directory; the other six print an example dotenv
    filename and are covered by the test below. This asserts the tool can EXPRESS them -- it
    does not move them, which is 03.3-11's job.

    Written to survive that sweep: the assertion is that a legacy occurrence at one of these
    sites is claimed by a REPLACEMENT rule, and a site with no legacy occurrence left has
    already been swept and passes vacuously. An occurrence here that classified as a KEEP would
    be the tool quietly deciding this defect was not its problem.
    """
    sources = rewrite.helper_sources()
    by_site: dict[tuple[str, int], list[object]] = {}
    for occurrence in _classify(sources):
        by_site.setdefault((occurrence.path, occurrence.line), []).append(occurrence)

    for path, anchor in _DEFERRED_PRINTED_PATHS:
        lines = sources[path].split("\n")
        matches = [index + 1 for index, text in enumerate(lines) if anchor in text]
        assert matches, f"{path} no longer carries the anchor {anchor!r}"
        for line in matches:
            for occurrence in by_site.get((path, line), []):
                assert occurrence.is_replacement, (
                    f"{path}:{line} carries {occurrence.matched!r} under {occurrence.label} -- "
                    "a deferred printed path must be owned by a replacement rule"
                )


def test_the_six_deferred_example_filenames_are_owned_by_a_replacement_rule() -> None:
    """The other six of the sixteen: the example dotenv filename in the help text."""
    owned = [
        occurrence
        for occurrence in _classify(rewrite.helper_sources())
        if occurrence.label == rewrite.REPLACE_EXAMPLE_FILENAME
    ]

    assert all(occurrence.path == "packages/cli/src/index.ts" for occurrence in owned)
    assert len(owned) in (0, 6), (
        "the example-filename class is either the six sites plan 03.3-02 recorded, or empty "
        f"because 03.3-11 has swept them; it is {len(owned)}"
    )


def test_check_refuses_a_tree_that_still_carries_a_pending_rewrite() -> None:
    """Red case 8. The successor to `DEFERRED_INVOCATION_BUDGET`, proven red before it replaced it.

    A ratchet may only be retired by a change that lands a strictly stronger check, and a check
    nobody has seen fail is a claim. Injected contents rather than a mutated worktree, the same
    discipline every other red case in this file follows.
    """
    line = 'console.log("Run `clawdi auth login` first.");\n'
    occurrences = _classify({"packages/cli/src/commands/x.ts": line})

    assert [o.label for o in occurrences] == [rewrite.REPLACE_INVOCATION]
    with pytest.raises(
        rewrite.RewriteError, match="still carry a spelling this script would rewrite"
    ):
        rewrite.assert_sweep_is_complete(occurrences)


def test_check_refuses_a_pending_rewrite_in_every_replacement_class() -> None:
    """The direction the retired ratchet could not see at all.

    `DEFERRED_INVOCATION_BUDGET` counted ONE mechanical shape -- the bare invocation. A printed
    state path or a capitalised product name could be added to the tree without moving that
    number by one. Each of the five classes is refused here on its own, so the replacement is
    demonstrably wider than the thing it replaced rather than merely differently spelled.
    """
    per_class = {
        rewrite.REPLACE_INVOCATION: 'p.log.info("Run `clawdi setup` first.");\n',
        rewrite.REPLACE_PRODUCT_NAME: 'throw new Error("Clawdi could not reach the API.");\n',
        rewrite.REPLACE_PRINTED_STATE_PATH: 'console.log("saved to ~/.clawdi/auth.json");\n',
        rewrite.REPLACE_DOC_STATE_PATH: "// stored under `~/.clawdi/state.json`.\n",
        rewrite.REPLACE_EXAMPLE_FILENAME: 'console.log("  --env-file .env.clawdi");\n',
    }
    for expected_label, line in per_class.items():
        occurrences = [
            occurrence
            for occurrence in _classify({"packages/cli/src/commands/x.ts": line})
            if occurrence.is_replacement
        ]
        assert [o.label for o in occurrences] == [expected_label], line
        with pytest.raises(rewrite.RewriteError):
            rewrite.assert_sweep_is_complete(occurrences)


def test_the_real_tree_carries_no_pending_rewrite() -> None:
    """The green half, run INSIDE the container against the tree the runner copied in.

    `helper_sources()` walks the filesystem and needs no repository metadata, so this half of
    the successor travels with the container exactly as the unclassified assertion above does.
    Host-side, `scripts/test.sh` runs the same refusal through `--check` against the other
    enumeration. Both halves, same reason as the guard family this file already carries.
    """
    rewrite.assert_sweep_is_complete(_classify(rewrite.helper_sources()))


def test_the_brand_string_classifier_is_wired_into_the_canonical_test_run() -> None:
    """The host-side invocation asserted from inside, exactly as the two older guards are.

    The floor and the two-enumeration comparison need `git ls-files`, and the clean runner
    copies the checkout in without repository metadata. So the drift check runs host-side, and
    this assertion is what keeps a later phase from removing it as cleanup.
    """
    runner = (rewrite.ROOT / "scripts/test.sh").read_text(encoding="utf-8")
    uncommented = "\n".join(line for line in runner.split("\n") if not line.strip().startswith("#"))

    assert "scripts/rewrite_brand_strings.py" in uncommented
