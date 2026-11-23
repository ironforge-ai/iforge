#!/usr/bin/env python3
"""Fail closed when the public brand surface still carries an unallowlisted ``clawdi`` hit.

The companion of ``scripts/check_rebrand_carveouts.py``. That guard answers "did the sweep
break something that must not move"; this one answers "is anything left that must move". Both
run host-side from ``scripts/test.sh`` before the Docker compose build, so a corrupted sweep
fails in seconds rather than after a 10-15 minute suite.

The scan covers the *public* surface only -- docs, the web app, the published CLI package, the
installer, deploy configuration and the workflows. Internal source and test symbols stay
upstream-aligned by D-01, so they are outside the pathspec entirely: the pathspec below IS the
machine-checkable record of where the D-02 boundary sits.

Inside its pathspec this guard reads identifiers and strings alike -- it greps text and does
not know the difference. The distinction matters at exactly one tree. ``packages/cli/src`` must
NOT have its identifiers read, because renaming them is permanent upstream merge-conflict
surface (D-01), and yet it is where ``iforge --help`` is written, so excluding it hid the
product's own printed self-description from the guard BRAND-01 names as its proof (verification
root cause 2). The resolution is not to widen this pathspec but to name a SIBLING guard that
reads the strings without touching the identifiers: ``packages/cli/tests/
cli-help-brand-contract.test.ts`` executes the binary across the command tree and asserts on
stdout. Its path is recorded in ``OUT_OF_SCOPE`` and asserted to exist, so the exclusion is
justified by something checkable rather than by a comment somebody has to believe.

``config/rebrand-allowlist.txt`` is the authority. Through the phase it also carried an
INTERIM section naming the plan that removed each of its entries, so the file shrank as the
phase progressed. Plan 08 emptied that section and ``load_allowlist`` now refuses to parse an
entry that sits under a heading marked INTERIM: a temporary exemption that outlives the plan
that added it is indistinguishable from a permanent carve-out nobody decided, and it silently
fences the very identifier a later phase might rename (T-03.1-34).

That refusal is now a property of the FILE rather than of the section that happens to be open
(WR-05). Phase 03.1 verification proved by execution that a blank line followed by a bare
``#`` or a ``# ---`` matched ``_SECTION_BANNER``, opened a fresh section and discarded the
marker, after which the entry beneath it parsed as an ordinary permanent carve-out. A tripwire
that moving a banner disarms is advisory against a careless editor, not a tripwire. A heading
kept with NO entries beneath it stays legal, which is exactly the shape plan 08 left the file
in: its retirement record is the audit trail of what was retired and by which plan, and a
refusal that also refused that record would force a later editor to delete history in order to
make the guard green.

The tripwire answers "is this fence temporary"; ``unclassified_sections`` answers the question
phase 03.1 verification found unanswered, which is "is this fence a carve-out or an unfixed
defect". Three entries in the allowlist were measured to be fencing DEFECTS -- documented
commands that no longer resolve -- and nothing in the grammar could tell them from the
carve-outs beside them. Every scope that fences an entry must now declare
``CLASS: PERMANENT`` or ``CLASS: DEFERRED -> <phase>``. Fencing an unfixed defect is legal;
fencing one SILENTLY is not, and a deferral that names nobody is reported for the same reason
as no declaration at all: it reads as a decision while committing nobody to carry it out.

A green run is read as evidence about the whole surface, so the guard must be unable to report
a result it did not establish. Two enumerations back that claim and both are now floored: the
scan refuses a ``SCAN_PATHSPEC`` that matches fewer than ``_MINIMUM_SCANNED_FILES`` files, and
``unclassified_entries`` refuses a repository enumeration that yields nothing at all. Neither
is a hypothetical -- a typo in a pathspec entry, a moved directory or an ``:(exclude)`` that
swallows its siblings all produce a clean line over an unread tree, which is the same class of
silent pass as reading a broken git as a clean one, one level up.

The pathspec is an opt-in list, so a directory added later is invisible to this guard by
default. Twice in phase 03.1 a directory inside the brand surface turned out to sit outside it
and was found by hand rather than by the scan (WINDOWS #29 ``scripts/``, #32 ``apps/web/e2e``).
``unclassified_entries`` closes that class: every entry under a coverage root from which this
guard reads *nothing at all* must be named in ``OUT_OF_SCOPE`` with its reason, so the omission
is a recorded decision rather than an oversight. It catches total blindness, not partial
blindness -- a directory the pathspec reaches into for some of its files (``docs`` through
``*.md``, say) counts as covered even where other file types in it are unread.

Structure copied verbatim from ``scripts/check_postgres_image_parity.py``: no arguments, no
argparse, standard library only, paths resolved from ``ROOT``, a domain ``RuntimeError``
subclass internally, ``SystemExit`` with a readable message from ``main()``, and exactly one
line of stdout on success.

Red and green behaviour is proven by ``backend/tests/test_rebrand_guards.py``.
"""

from __future__ import annotations

import fnmatch
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST = ROOT / "config/rebrand-allowlist.txt"

# The public brand surface, as a git pathspec. `.planning` is excluded because planning
# artifacts describe the rename rather than carrying it. `config/rebrand-allowlist.txt` is
# excluded for the same reason one step further in: it is the specification of the tokens, so
# scanning it would make every one of its own entries a hit that it then covers by itself.
# docs/runbooks/iforge-env-migration.md is the third of that kind and the only one a user
# reads: its whole job is to put the old and new spelling of every public name side by side,
# so a developer can rename their own untracked `.env`. A rename table that did not name the
# old spelling would be useless, so the file is not scanned -- and because an unscanned file
# could then drift, `test_the_migration_runbook_still_matches_the_env_contract` pins every
# pair in it to config/rebrand-env-contract.txt instead. That test is the control this
# exclusion is safe under; do not remove one without the other.
#
# `*.env.example` is membership source 1 of config/rebrand-env-contract.txt (WINDOWS #40): the
# guard was policing a contract derived from files it did not read. Widening it revealed
# exactly the two hits that entry predicted -- a prose defect naming the old product in a
# user-facing sentence, and a persisted C-7 issuer value -- which are now respectively fixed
# and carved out.
#
# `apps/web/vercel.json` is here because its ABSENCE is the structural reason WINDOWS #30
# survived the whole phase: this fork's own domain served upstream's installer from a file no
# guard read. Plan 12 removed that redirect and the file measures 0 hits, so including it costs
# nothing and stops the regression rather than re-finding it by hand next time.
SCAN_PATHSPEC: tuple[str, ...] = (
    "*.md",
    "*.env.example",
    "llms.txt",
    "apps/web/src",
    "apps/web/public",
    "apps/web/vercel.json",
    "packages/cli/package.json",
    "packages/cli/bin",
    # D-23 (06.1-14): the desktop app is rebranded in full, so its manifest, source, build
    # resources and scripts are scanned like the web app. Its README is already `*.md`.
    "apps/desktop/package.json",
    "apps/desktop/src",
    "apps/desktop/build",
    "apps/desktop/scripts",
    "install.sh",
    "config",
    "docker-compose*.yml",
    "deploy",
    ".github/workflows",
    ":(exclude).planning",
    ":(exclude)config/rebrand-allowlist.txt",
    ":(exclude)docs/runbooks/iforge-env-migration.md",
)

# The roots whose immediate entries are enumerated when the guard checks its own coverage:
# the repository root, plus the two package roots and the workflow root that SCAN_PATHSPEC
# only reaches into partially. Those are exactly the places where a directory inside the brand
# surface turned out to sit outside the pathspec.
COVERAGE_ROOTS: tuple[str, ...] = (
    "",
    "apps",
    "apps/web",
    "apps/desktop",
    "packages",
    "packages/cli",
    ".github",
)

# Every entry under a coverage root that this guard reads nothing from, with the reason it is
# out of scope. A new directory that no pathspec entry reaches fails `unclassified_entries`
# until it is either scanned or recorded here -- the boundary is a decision, and an undecided
# one is the defect.
OUT_OF_SCOPE: dict[str, str] = {
    # D-01 / C-13: internal source and test symbols stay upstream-aligned so the fork can keep
    # merging from upstream. Roughly 8,900 occurrences across these trees; the pathspec
    # excluding them IS the D-02 boundary record.
    "backend": "D-01: backend source, tests and fixtures stay upstream-aligned (C-13).",
    "packages/cli/src": (
        "D-01 / C-13: CLI source stays outside this pathspec for IDENTIFIERS, so the fork can "
        "keep merging from upstream. That exclusion also hid the CLI's own printed "
        "self-description, which is verification root cause 2: `packages/cli/src` is where "
        "`iforge --help` is written, and excluding it made the product's self-description "
        "invisible to the guard BRAND-01 names as its proof. The printed-string surface is now "
        "covered by packages/cli/tests/cli-help-brand-contract.test.ts, which EXECUTES the "
        "binary across the whole command tree and asserts on stdout rather than on source "
        "text, so D-01's identifier fence is preserved by construction. That path is asserted "
        "to exist by test_the_excluded_cli_source_tree_names_the_control_that_covers_its_"
        "strings -- the record cannot rot into a reference to a deleted file."
    ),
    "packages/cli/tests": "D-01: CLI tests stay upstream-aligned (C-13).",
    "packages/cli/scripts": "D-01: CLI build scripts stay upstream-aligned (C-13).",
    "packages/cli/egress-addon": "D-01: vendored egress addon, upstream-aligned (C-13).",
    "packages/shared": "D-01: the shared package keeps its `@clawdi/shared` name (C-13).",
    "packages/whatsapp-baileys-sidecar": "D-01: sidecar source stays upstream-aligned (C-13).",
    "apps/web/e2e": (
        "WINDOWS #32, decided by plan 08. Measured: 66 hits, every one a stub or fixture "
        "payload mirroring the hosted API's wire values (C-4 media types, C-6 `clawdi_` key "
        "prefixes, C-7 provider ids, X-6 `clawdi.ai` problem URIs, W-1 `on-clawdi`) or a "
        "test-local symbol. The UI copy these specs assert lives in apps/web/src, which IS "
        "scanned, so a residual brand string in copy fails the guard at its source."
    ),
    "test-fixtures": "D-01: recorded fixtures reproduce upstream payloads verbatim (C-13).",
    "apps/desktop/tsconfig.json": "TypeScript compiler settings; measured 0 hits.",
    # Migration-not-sweep identities and internal machinery.
    "scripts": (
        "WINDOWS #29, decided by plan 08. Internal repository machinery, not published "
        "surface: no user reads it and nothing in it ships. Two of its files ARE the rename "
        "specification -- check_rebrand_carveouts.py enumerates 80 do-not-rename literals and "
        "this file enumerates the tokens -- so scanning them would make the guard's own "
        "authority its largest finding, the same reason config/rebrand-allowlist.txt is "
        "excluded one directory over. Measured: 242 hits, all X-2 unit names, X-3 database "
        "identity, A-P1 Kamal service and host paths, or C-13 test-runner plumbing "
        "(CLAWDI_REPO_SOURCE, CLAWDI_TEST_WORKDIR, CLAWDI_TEST_RUNNER_SKIP_BUILD), which the "
        "public env contract records as exclusions rather than pairs."
    ),
    "docker": "Build context for the clean test runner; internal machinery, not shipped.",
    "packages/cli/skills": (
        "D-07 / X-5: the bundled skill ids are written into third-party agent config on user "
        "machines and install.sh validates the exact member paths inside the tarball."
    ),
    # Workspace tooling and generated files: no brand surface, or C-12.
    "bun.lock": "C-12: regenerated by `bun install`; not authored.",
    "package.json": "Workspace root manifest; `private: true`, never published.",
    "turbo.json": "Task graph configuration; carries no brand token.",
    "biome.json": "Formatter configuration; carries no brand token.",
    "lefthook.yml": "Git hook configuration; carries no brand token.",
    "paseo.json": "Local dev-stack configuration; carries no brand token.",
    "tsconfig.base.json": "Compiler configuration; carries no brand token.",
    "packages/cli/tsconfig.json": "Compiler configuration; carries no brand token.",
    "tools": "Developer tooling; carries no brand token.",
    "patches": "Upstream package patches (libsignal logging fix #994, D-06); no brand surface.",
    ".gitignore": "X-1: ignores `.clawdi.env` and the CLI's local broker directory.",
    ".dockerignore": "Build context filter; carries no brand token.",
    "LICENSE": "Upstream copyright notice; a fork does not rewrite it.",
    "packages/cli/LICENSE": "Upstream copyright notice; a fork does not rewrite it.",
    ".github/actions": "Composite CI actions; carry no brand token.",
    ".github/dependabot.yml": "Dependency update configuration; carries no brand token.",
    ".github/release.yml": "Release-note categorisation; carries no brand token.",
    ".planning": (
        "Planning artifacts describe the rename rather than carrying it, and the pathspec "
        "already excludes them explicitly."
    ),
    # apps/web: the app's own source and public assets are scanned; everything beside them is
    # build, test or deployment configuration. Each was measured rather than assumed.
    # apps/web/.env.example and apps/web/vercel.json used to sit here. Both are SCANNED now:
    # plan 11 widened the pathspec to `*.env.example` (WINDOWS #40) and added vercel.json after
    # plan 12 removed the upstream installer redirect that was its only hit (WINDOWS #30). A
    # recorded exclusion over a file the guard now reads is a fence around nothing.
    "apps/web/package.json": (
        "The app manifest is private and never published; its two hits are an X-6 "
        "`api.clawdi.ai` generator source and the `@clawdi/shared` workspace dependency."
    ),
    "apps/web/Dockerfile": "Container build for the web app; measured 0 hits.",
    "apps/web/.gitignore": "Ignore rules; measured 0 hits.",
    "apps/web/bunfig.toml": "Bun configuration; measured 0 hits.",
    "apps/web/components.json": "shadcn generator configuration; measured 0 hits.",
    "apps/web/postcss.config.mjs": "PostCSS configuration; measured 0 hits.",
    "apps/web/tsconfig.json": "Compiler configuration; measured 0 hits.",
    "apps/web/tsr.config.json": "Router codegen configuration; measured 0 hits.",
    "apps/web/turbo.json": "Task graph configuration; measured 0 hits.",
    "apps/web/vite.config.ts": "Bundler configuration; measured 0 hits.",
    "apps/web/test-setup.ts": "Test bootstrap; measured 0 hits.",
    "apps/web/playwright.config.ts": "Playwright configuration; measured 0 hits.",
    "apps/web/playwright.hosted.config.ts": "Playwright configuration; measured 0 hits.",
    "apps/web/playwright.auth.config.ts": (
        "Playwright configuration (upstream sync step 4, 06.1-11); measured 0 hits once its "
        "Vite keys read VITE_IFORGE_*."
    ),
    "apps/web/playwright.openclaw.config.ts": (
        "Playwright configuration (upstream sync step 4, 06.1-11); measured 0 hits."
    ),
    "apps/web/scripts": (
        "Production SSR smoke test (upstream sync step 4, 06.1-11), the apps/web/e2e "
        "precedent (WINDOWS #32): it asserts copy that apps/web/src renders, and apps/web/src "
        "is scanned. Measured 1 hit, the `<title>` assertion, rewritten to iforge; now 0."
    ),
    "apps/web/instrument.server.mjs": "Sentry bootstrap; measured 0 hits.",
    "apps/web/instrumentation-client.ts": "Sentry bootstrap; measured 0 hits.",
}

_PATH_PREFIX = "path:"
_REGEX_PREFIX = "re:"
_TEMPORARY_MARKER = "interim"
_SECTION_BANNER = re.compile(r"^#[\s=-]*$")

# D-04's lowercase rule, as three patterns rather than as a remembered command. `_CASING_TOKEN`
# takes the whole identifier around an occurrence so the register exception can be judged on
# the identifier rather than on the six letters in the middle of it.
_CASING_TOKEN = re.compile(r"[A-Za-z0-9_]*[Ii][Ff][Oo][Rr][Gg][Ee][A-Za-z0-9_]*")
_CASING_CORE = re.compile(r"[Ii][Ff][Oo][Rr][Gg][Ee]")
_REGISTER_NAME = re.compile(r"[A-Z0-9_]+")

# The two legal classifications a fence may declare. `CLASS: PERMANENT` is a carve-out that is
# never expected to move; `CLASS: DEFERRED -> <phase>` is a fence over something that IS
# expected to move, with the phase that will move it named after the arrow. The tokens are
# deliberately greppable and deliberately unlike `_TEMPORARY_MARKER`, which is a different
# claim: the marker says "this section is scaffolding", these say "this fence is (or is not)
# permanent". A declaration governs the entries beneath it until the next blank-line-separated
# comment run or the next section banner, whichever comes first.
_CLASSIFICATION_MARKERS: tuple[str, str] = ("CLASS: PERMANENT", "CLASS: DEFERRED ->")

# The floor beneath which a run is not a result. MEASURED at the commit that introduced it:
# `git ls-files --cached --others --exclude-standard -- <SCAN_PATHSPEC>` reported 733 files
# (735 once `*.env.example` joined the pathspec). The floor is deliberately far below that, so
# deleting a whole documentation directory does not trip it and only a pathspec that has
# stopped resolving does. It is a smoke alarm, not a ratchet: raising it to hug the real count
# would make every ordinary deletion a guard failure, and a guard that cries wolf gets its
# floor lowered by the next person in a hurry rather than being fixed.
_MINIMUM_SCANNED_FILES = 600


class ResidualError(RuntimeError):
    """The residual scan could not be completed, or its authority could not be parsed."""


def iter_allowlist_entries(text: str) -> list[tuple[str, str]]:
    """Return ``(section_comments, entry)`` for every entry, in file order.

    A section opens at a banner comment -- a line of nothing but ``#``, ``-`` and ``=`` -- that
    follows a blank line or the start of the file, and runs until the next one. Every comment
    inside it accumulates, so an entry carries the whole of its section's prose, not merely the
    line above it: the file writes a heading, a rule of dashes, several paragraphs of reasoning
    and then a run of entries, and the citation an entry answers to is usually in the heading.
    """
    entries: list[tuple[str, str]] = []
    section: list[str] = []
    previous_blank = True
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            previous_blank = True
            continue
        if line.startswith("#"):
            if previous_blank and _SECTION_BANNER.match(line):
                section = []
            else:
                section.append(line)
            previous_blank = False
            continue
        previous_blank = False
        entries.append(("\n".join(section), line))
    return entries


def classify_entries(text: str) -> list[tuple[str, str, str, str, str]]:
    """Return ``(scope, entry, kind, receiving, declaration)`` for every entry, in file order.

    ``kind`` is ``"permanent"``, ``"deferred"`` or ``""`` when no declaration governs the
    entry. ``receiving`` is the phase named after the arrow of a deferral, and is ``""`` for a
    permanent entry or a deferral that named nobody. ``scope`` is the first substantive comment
    line of the run that carries the governing declaration -- or, for an undeclared entry, of
    the run immediately above it -- which is what a reader needs in order to find the fence.

    The scope is a comment RUN rather than a banner-delimited section on purpose, and the
    difference is load-bearing rather than stylistic. The two largest sections in the file are
    umbrellas: one holds fifteen sub-blocks and the other ten, each with its own citation and
    its own class, so a per-section declaration would have to make a single claim about a set
    that provably mixes -- which is the ambiguity this rule exists to remove, reintroduced one
    level up.

    A declaration's reach ends at the first blank line that follows the run it governs,
    whichever kind of run comes next. That second half was missing until CR-01: the reset fired
    only when the next run opened with a COMMENT, so a blank line followed by a bare ENTRY --
    the cheapest edit there is, and the shape an append to the foot of the file naturally takes
    -- carried the previous run's declaration forward and parsed as a declared carve-out it
    never declared. Appending ``re:.`` that way was measured to report zero uncovered hits over
    an entirely unswept tree while ``unclassified_sections`` reported nothing. Both run kinds
    now reset, so a sub-block added later genuinely cannot inherit its neighbour's
    classification.
    """
    classified: list[tuple[str, str, str, str, str]] = []
    scope = ""
    declaration = ""
    declaration_scope = ""
    previous_blank = True
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            previous_blank = True
            continue
        if line.startswith("#"):
            if previous_blank:
                # A new run, and with it a new scope: a declaration never reaches past the
                # blank line beneath it.
                scope = ""
                declaration = ""
                declaration_scope = ""
            if not scope and not _SECTION_BANNER.match(line):
                scope = line
            if any(marker in line for marker in _CLASSIFICATION_MARKERS):
                declaration = line
                declaration_scope = scope or line
            previous_blank = False
            continue
        if previous_blank:
            # An ENTRY run that opens after a blank line ends the previous declaration's
            # reach exactly as a comment run does. Without this the reset fired only on the
            # comment branch, and a blank line plus a bare entry -- the shape any append to
            # the foot of the file takes -- inherited the neighbouring declaration and was
            # reported as a declared carve-out it never declared (CR-01).
            scope = ""
            declaration = ""
            declaration_scope = ""
        previous_blank = False
        if not declaration:
            classified.append((scope or line, line, "", "", ""))
            continue
        if _CLASSIFICATION_MARKERS[0] in declaration:
            classified.append((declaration_scope, line, "permanent", "", declaration))
            continue
        receiving = declaration.split(_CLASSIFICATION_MARKERS[1], 1)[1].strip()
        classified.append((declaration_scope, line, "deferred", receiving, declaration))
    return classified


def unclassified_sections(text: str) -> list[tuple[str, str]]:
    """Return ``(scope, first_entry)`` for every scope that fences without declaring what.

    Two states are reported, and they are the same defect wearing different clothes: a scope
    with entries and no declaration at all, and a deferral with nothing after its arrow. The
    second is the one worth spelling out -- it reads as a decision ("this moves later") while
    committing nobody to move it, which is exactly how a fence over an unfixed defect survives
    a phase looking like a carve-out. Phase 03.1 verification measured three entries in this
    file doing precisely that, and nothing in the grammar could tell them from their
    neighbours.

    Fencing an unfixed defect is legal. Fencing one *silently* is not: a deferral must say it
    is a deferral and name who receives it, so the fence carries its own retraction plan.
    """
    reported: list[tuple[str, str]] = []
    seen: set[str] = set()
    for scope, entry, kind, receiving, _ in classify_entries(text):
        if kind == "permanent" or (kind == "deferred" and receiving):
            continue
        if scope in seen:
            continue
        seen.add(scope)
        reported.append((scope, entry))
    return reported


def load_allowlist(text: str) -> list[tuple[str, str]]:
    """Parse ``config/rebrand-allowlist.txt`` into ``(kind, value)`` pairs.

    Kinds are ``path`` (fnmatch glob against the hit's path segment), ``re`` (regex searched
    against the whole ``<path>:<line>:<text>`` string) and ``sub`` (fixed substring, same).
    Blank lines and lines whose first non-space character is ``#`` are skipped.

    An entry whose section is marked INTERIM is refused outright. Every plan in phase 03.1
    leaned on that section while its own surface was mid-rename, which is a legitimate staging
    device exactly once; an exemption nobody removes afterwards fences the identifier it was
    protecting against every later rename, and reads as a decision that was never made
    (T-03.1-34). The check is case-insensitive and runs TWICE, over two different scopes:

    * file-wide, before any entry is read. If the marker appears anywhere in the text and the
      file carries at least one entry, the file is refused. This is the check that cannot be
      argued around, because it does not care where the marker sits relative to the entry. It
      exists because the per-section check below was proven evadable in-process: a blank line
      and a bare ``#`` open a fresh section and the marker is forgotten (WR-05).
    * per-section, as before. It is kept because it produces the better message -- it can name
      the offending entry -- and a guard that says which line to fix is worth two checks.

    A file that carries the marker and NO entries parses. That is deliberate and is the shape
    the real allowlist is in: plan 08's retirement record names the marker while fencing
    nothing, and refusing it would make deleting the audit trail the way to get a green run.

    Every ``re:`` entry is compiled here rather than at match time. An unparseable pattern that
    was only compiled lazily would cover nothing and the scan would report clean against a
    dirty tree -- a guard that reports success it has not established.
    """
    parsed = iter_allowlist_entries(text)
    if parsed and _TEMPORARY_MARKER in text.lower():
        raise ResidualError(
            f"temporary allowlist exemption outlived its phase: the file carries {len(parsed)} "
            f"entries and the marker appears somewhere in it (first entry {parsed[0][1]!r}). "
            "Retire the entries with the plan that added them, or move them into a durable "
            "section citing the carve-out id that keeps them. The marker is refused file-wide, "
            "not per section, so moving a banner does not re-admit an entry."
        )
    entries: list[tuple[str, str]] = []
    for section, line in parsed:
        if _TEMPORARY_MARKER in section.lower():
            raise ResidualError(
                f"temporary allowlist exemption outlived its phase: {line!r} sits in a section "
                "marked INTERIM. Retire the entry with the plan that added it, or move it into "
                "a durable section citing the carve-out id that keeps it."
            )
        if line.startswith(_PATH_PREFIX):
            entries.append(("path", line[len(_PATH_PREFIX) :]))
        elif line.startswith(_REGEX_PREFIX):
            pattern = line[len(_REGEX_PREFIX) :]
            try:
                re.compile(pattern)
            except re.error as error:
                raise ResidualError(
                    f"unparseable regex allowlist entry {line!r}: {error}"
                ) from error
            entries.append(("re", pattern))
        else:
            entries.append(("sub", line))
    return entries


def _allows(kind: str, value: str, hit: str, path: str) -> bool:
    if kind == "path":
        return fnmatch.fnmatch(path, value)
    if kind == "re":
        return re.search(value, hit) is not None
    return value in hit


def unfenceable(hit: str) -> str:
    """Return the reason a hit may never be allowlisted, or ``""`` when it may.

    The allowlist answers "is this occurrence acceptable". A handful of shapes are not
    acceptable at ANY breadth, and for those the allowlist must not get a vote -- otherwise the
    fence that legitimately covers a hostname also covers a command that executes code fetched
    from that hostname, and the guard reports clean over the very defect it was written for.

    The one shape here is CR-02: a ``curl``/``wget`` on the same line piped into a shell, where
    the fetched URL carries the upstream brand. Three one-line variations on it --
    ``curl -sSL … | sh``, ``curl -fsSL … | bash``, ``wget -qO- … | sh``, all pointing at
    ``https://clawdi.ai/install.sh`` -- were measured to pass BOTH this scanner and
    ``packages/cli/tests/install-docs-contract.test.ts``. That is this fork's own front page
    piping upstream's installer into a shell (WINDOWS #30), fenced by the two guards written in
    the same phase to make it impossible.

    Narrowing the entry that covered it was not enough and was tried first: ``sub:clawdi.ai``
    and the C-4 wire-key regex ``(?<!@)clawdi\\.[a-z][A-Za-z0-9-]*`` each cover the domain
    inside a URL independently, so the pipeline stayed allowlisted with either one narrowed.
    Stating the rule once, here, makes it hold for all 137 current entries and for every entry
    added later -- including a future ``path:`` glob that fences a whole directory.

    Measured against the tree at the commit this was added: 0 of 1,091 hits match, so the rule
    refuses nothing that exists. It is a tripwire, not a sweep.
    """
    for pattern, reason in _UNFENCEABLE:
        if pattern.search(hit):
            return reason
    return ""


_UNFENCEABLE: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"(?:curl|wget)[^\n|]*clawdi[^\n|]*\|\s*(?:\w+=\S+\s+)*(?:ba|z|da)?sh\b"
        ),
        (
            "fetches from an upstream-branded URL and pipes it into a shell; no allowlist "
            "entry may fence a command that executes upstream code (CR-02, WINDOWS #30)"
        ),
    ),
]


def find_residual(hits: list[str], allowlist: list[tuple[str, str]]) -> list[str]:
    """Return the hits no allowlist entry covers, in input order.

    A hit matching ``unfenceable`` is returned even when an entry covers it: see that function
    for why the allowlist does not get a vote on those shapes.
    """
    uncovered: list[str] = []
    for hit in hits:
        path = hit.split(":", 1)[0]
        if unfenceable(hit):
            uncovered.append(hit)
            continue
        if any(_allows(kind, value, hit, path) for kind, value in allowlist):
            continue
        uncovered.append(hit)
    return uncovered


def collect_hits(
    run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> list[str]:
    """Return every ``<path>:<line>:<text>`` hit for ``clawdi`` across ``SCAN_PATHSPEC``.

    ``run`` is the ``subprocess.run`` seam, injected by tests so exit-code handling can be
    exercised without a tree in the corresponding state.

    ``git grep`` exits 1 when it matches nothing. That is the state this phase is driving
    towards, so it is mapped to an empty result rather than to an error -- but only exit code
    1. Anything else is git failing, and reading a broken git as a clean tree is precisely the
    silent pass this guard exists to prevent.

    ``--untracked`` is deliberate (WR-06). ``git grep`` is tracked-only by construction, and
    the moment this guard is most wanted is the moment a sweep is being judged -- which is
    before it is committed, which is exactly when its files are invisible to git. Exposure at
    the commit this flag was added was zero; the hole was structural, not observed, and a hole
    that is only closed by the tree happening to be clean is not closed.
    """
    runner = subprocess.run if run is None else run
    completed = runner(
        ["git", "grep", "-Iin", "--untracked", "clawdi", "--", *SCAN_PATHSPEC],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode == 1:
        return []
    if completed.returncode != 0:
        raise ResidualError(
            f"git grep failed during the residual scan: {completed.stderr.strip()}"
        )
    return [line for line in completed.stdout.splitlines() if line]


def casing_violations(
    run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> list[str]:
    """Return the hits that spell the NEW product name in anything but its two legal cases.

    D-04 fixes the prose, UI and documentation casing as ``iforge``, lowercase, everywhere. The
    single exception is the environment register, whose names are SCREAMING_SNAKE by the
    convention every shell in the world shares -- ``IFORGE_API_URL``, ``VITE_IFORGE_HOSTED``.
    So an occurrence is legal when it is lowercase ``iforge``, or when it is uppercase
    ``IFORGE`` inside an all-caps identifier that carries an underscore. Everything else --
    ``Iforge``, ``IForge``, ``iForge``, or a bare shouted ``IFORGE`` in a sentence -- is a
    violation.

    The allow rule is MEASURED rather than guessed: across the scanned surface at the commit
    that added this there were 1,566 lowercase occurrences and 241 uppercase ones, every one of
    the latter inside a register name with an underscore, and zero mixed-case spellings. A rule
    that flagged the register would have had to be switched off the day it was written.

    D-04 was previously enforced by a verifier happening to run a grep by hand and reporting it
    clean. A rule nothing executes is a convention, and a convention lasts exactly as long as
    the next person remembers it; this is the same reason the carve-out literals are asserted
    rather than trusted.
    """
    runner = subprocess.run if run is None else run
    completed = runner(
        ["git", "grep", "-Iin", "--untracked", "iforge", "--", *SCAN_PATHSPEC],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode == 1:
        return []
    if completed.returncode != 0:
        raise ResidualError(
            f"git grep failed during the casing scan: {completed.stderr.strip()}"
        )
    violations: list[str] = []
    for hit in completed.stdout.splitlines():
        if not hit:
            continue
        text = hit.split(":", 2)[2] if hit.count(":") >= 2 else hit
        for token in _CASING_TOKEN.findall(text):
            core = _CASING_CORE.search(token)
            if core is None:  # pragma: no cover - findall guarantees a core
                continue
            if core.group(0) == "iforge":
                continue
            if (
                core.group(0) == "IFORGE"
                and "_" in token
                and _REGISTER_NAME.fullmatch(token)
            ):
                continue
            violations.append(hit)
            break
    return violations


def _ls_files(
    pathspec: list[str],
    run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> list[str]:
    """Return the tracked paths matching ``pathspec``, letting git evaluate the pathspec.

    The pathspec is handed to git rather than re-implemented with ``fnmatch`` so that
    ``*.md`` matching at every depth and the ``:(exclude)`` magic mean here exactly what they
    mean in ``collect_hits``. A coverage claim computed by an approximation of the pathspec
    would be a claim about the approximation.

    ``--cached --others --exclude-standard`` matches the ``--untracked`` that ``collect_hits``
    passes to ``git grep`` (WR-06). The two must enumerate the SAME set or the coverage claim
    is about a different tree than the hit scan: a file the grep reads but the enumeration
    cannot see would be judged, while a directory the enumeration reports as unscanned might
    in fact have been read. ``--exclude-standard`` keeps ignored paths out, so ``node_modules``
    and build output do not become entries somebody has to classify.
    """
    runner = subprocess.run if run is None else run
    completed = runner(
        [
            "git",
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
            "--",
            *pathspec,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise ResidualError(
            f"git ls-files failed while checking pathspec coverage: {completed.stderr.strip()}"
        )
    return [path for path in completed.stdout.split("\0") if path]


def assert_scan_is_not_vacuous(
    run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> int:
    """Return the number of files ``SCAN_PATHSPEC`` resolves to, refusing a vacuous run.

    ``git grep`` exiting 1 means "matched nothing", and ``collect_hits`` maps that to a clean
    result -- correct when the pathspec really did reach the surface, and a false all-clear
    when it reached nothing. A typo in one pathspec entry, a directory that moved, or an
    ``:(exclude)`` that swallowed its own siblings all produce that state, and the run prints
    the success line having read no file at all. This is the same class as reading a broken
    git as a clean tree, one level up, and it is the class the success line is most often
    cited for.
    """
    scanned = _ls_files(list(SCAN_PATHSPEC), run=run)
    if len(scanned) < _MINIMUM_SCANNED_FILES:
        raise ResidualError(
            f"the scan pathspec resolved to {len(scanned)} files, below the floor of "
            f"{_MINIMUM_SCANNED_FILES}: the guard refuses to report a result it did not "
            "establish. Either SCAN_PATHSPEC has stopped resolving against this tree, or the "
            "brand surface really did shrink by that much and the floor is what needs editing."
        )
    return len(scanned)


def unclassified_entries(
    run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> list[str]:
    """Return the entries under a coverage root that are neither scanned nor recorded.

    "Scanned" means this guard reads at least one file under the entry. An entry it reads
    nothing from is a directory a green run says nothing about, so it must appear in
    ``OUT_OF_SCOPE`` with the reason -- the omission recorded as a decision rather than left as
    an oversight to be rediscovered by hand.

    An empty repository enumeration is refused rather than returned. ``[]`` out of this
    function means "every entry is classified", and against a git that reported no files at
    all it would mean "there was nothing to classify" -- two different claims that
    ``main()`` cannot tell apart, one of which the guard has not established.
    """
    tracked = _ls_files([], run=run)
    if not tracked:
        raise ResidualError(
            "the repository enumeration returned no files at all, so pathspec coverage could "
            "not be computed: the guard refuses to report a result it did not establish"
        )
    scanned = _ls_files(list(SCAN_PATHSPEC), run=run)

    def entries_under(root: str) -> set[str]:
        prefix = f"{root}/" if root else ""
        found: set[str] = set()
        for path in tracked:
            if not path.startswith(prefix):
                continue
            remainder = path[len(prefix) :]
            if remainder:
                found.add(prefix + remainder.split("/", 1)[0])
        return found

    covered = {
        entry
        for root in COVERAGE_ROOTS
        for entry in entries_under(root)
        if any(path == entry or path.startswith(f"{entry}/") for path in scanned)
    }
    return sorted(
        entry
        for root in COVERAGE_ROOTS
        for entry in entries_under(root)
        if entry not in covered and entry not in OUT_OF_SCOPE
    )


def scan() -> list[str]:
    """Load the authority, collect the hits, and return the ones it does not cover."""
    allowlist = load_allowlist(ALLOWLIST.read_text(encoding="utf-8"))
    return find_residual(collect_hits(), allowlist)


def main() -> None:
    try:
        assert_scan_is_not_vacuous()
        undeclared = unclassified_sections(ALLOWLIST.read_text(encoding="utf-8"))
        unclassified = unclassified_entries()
        miscased = casing_violations()
        uncovered = scan()
    except ResidualError as error:
        raise SystemExit(f"rebrand residual failed: {error}") from error
    if undeclared:
        for scope, entry in undeclared:
            print(f"unclassified section: {entry}  [{scope}]", file=sys.stderr)
        raise SystemExit(
            f"rebrand residual failed: {len(undeclared)} allowlist sections fence entries "
            "without declaring whether they are permanent carve-outs or deferred fixes. Add "
            f"'{_CLASSIFICATION_MARKERS[0]}' or '{_CLASSIFICATION_MARKERS[1]} <phase>' to each "
            "-- fencing an unfixed defect is legal, fencing one silently is not"
        )
    if unclassified:
        for entry in unclassified:
            print(f"unclassified: {entry}", file=sys.stderr)
        raise SystemExit(
            f"rebrand residual failed: {len(unclassified)} repository entries are neither "
            "scanned nor recorded in OUT_OF_SCOPE -- decide which side of the boundary they "
            "sit on before a green run is read as evidence about them"
        )
    if miscased:
        for hit in miscased:
            print(f"casing: {hit}", file=sys.stderr)
        raise SystemExit(
            f"rebrand residual failed: {len(miscased)} hits spell the product name in a case "
            "D-04 does not allow. Prose, UI and documentation use `iforge`, lowercase; only "
            "the SCREAMING_SNAKE environment register is uppercase"
        )
    if uncovered:
        for hit in uncovered:
            # An unfenceable hit names its reason. Without it the operator reads "unallowlisted"
            # and reaches for the allowlist, which is the one repair that cannot work here.
            reason = unfenceable(hit)
            if reason:
                print(
                    f"residual (NOT allowlistable -- {reason}): {hit}", file=sys.stderr
                )
            else:
                print(f"residual: {hit}", file=sys.stderr)
        raise SystemExit(
            f"rebrand residual failed: {len(uncovered)} unallowlisted hits"
        )
    print("rebrand residual passed: 0 unallowlisted hits")


if __name__ == "__main__":
    main()
