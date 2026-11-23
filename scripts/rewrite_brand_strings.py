#!/usr/bin/env python3
"""Label every legacy-brand occurrence in the CLI source, and rewrite the ones that are ours.

The durable owner of MIGR-03: the class of user-visible strings printed from source that stays
upstream-aligned under D-01. Upstream merges keep re-introducing that class, so the class needs
a script rather than a sweep -- and a script needs a check, or it rots silently between merges
and the next reader trusts a tool that stopped being true.

WHY EVERY OCCURRENCE IS LABELLED, not only the ones that move. A rule set that matched only
what it intended to rewrite would be a blanket substitution with a narrower regex: everything
it failed to think of would fall to whichever side its pattern happened to land on. Here every
occurrence gets exactly one label, a replacement or a NAMED keep class, and an occurrence the
rule set cannot place is reported as unclassified and BLOCKS the apply path. This repository
has already rewritten thirty historical release URLs with a blanket substitution once. The
rule set is where that cannot happen again, and the unclassified bucket is the part that makes
it true rather than merely intended.

TWO DECISIONS PER LINE WHERE A LINE CARRIES TWO CLASSES. `index.ts:73` prints an example
dotenv filename AND names the secret-reference URI scheme; the two move under different classes
(until 06.1-11 the second was a keep). A line-level classifier would have to make one decision
about two facts, and the
invocation count would not fall the way the ratchet expects. Records are per OCCURRENCE, keyed
by path, line and column.

WHAT THE CHECK MODE ESTABLISHES. Five things: the scanned set has not shrunk below its floor;
every occurrence still classifies; the bare-invocation occurrences this script finds are the
SAME set the committed test helper finds (two implementations of one rule that agree are
evidence, one implementation is a claim); the state-directory name this script would write
matches the one `state-root.ts` declares; and NO occurrence is still pending a rewrite.

The fifth arrived with plan 03.3-11 and the fourth mode paragraph explains why it could not
have arrived earlier. It is also why the CLI help contract no longer carries
`DEFERRED_INVOCATION_BUDGET`: that constant was an equality ratchet over one mechanical shape
with a ceiling of 272, and this refusal covers all five replacement classes with a ceiling of
zero. A ratchet retired by deletion would have been a weakening; retired by THIS it is not.

WHERE THE COMMITTED REPORT LIVES. A classify run over the tree is committed at
``.planning/phases/03.3-brand-migration-deferred-identities-cli-strings-artwork-inse/
03.3-09-classify-report.txt`` so the keep buckets can be reviewed without re-running anything.
It is under ``.planning`` rather than beside this file because the report quotes every
occurrence verbatim, including all 55 wire-protocol schema identifiers -- and
``check_rebrand_carveouts.py`` proves those identifiers still RESOLVE by grepping the tree. A
report committed anywhere that guard reads would keep answering "yes" for an identifier that
had been deleted from the source, which is that guard made vacuous by this one's evidence.
``.planning`` is already excluded by both guards, so no guard needed editing to make this safe.
The report's own ``tree`` line says which tree it describes; a stale one is visible rather than
misleading, and ``--apply`` refuses it.

Structure copied from ``scripts/check_rebrand_residual.py`` and
``scripts/check_rebrand_carveouts.py``: standard library only (so it runs under bare
``python3``, not only under ``uv``), paths resolved from ``ROOT``, mode dispatch by explicit
``sys.argv`` inspection rather than argparse, a domain ``RuntimeError`` subclass internally,
and ``SystemExit`` with a readable message from ``main()``.

Red and green behaviour is proven by ``backend/tests/test_rebrand_guards.py``.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUTHORITY = ROOT / "config/brand-strings.txt"

# The committed shape this script must agree with, and the file that owns it. The regex is
# EXTRACTED from that file rather than retyped here: a copy would agree with the original only
# until somebody edited one of them, which is the drift this mode exists to catch.
INVOCATION_SHAPE_SOURCE = "packages/cli/tests/cli-help-brand-contract.test.ts"
_INVOCATION_SHAPE_DECLARATION = re.compile(r"const BARE_INVOCATION_SHAPE = /(.+)/;")

# The state root, as `state-root.ts` declares it. The printed-state-path class rewrites paths
# the CLI prints; if the printed name and the resolved name disagree the CLI lies to the user,
# which is the exact defect this class exists to repair.
STATE_ROOT_SOURCE = "packages/cli/src/lib/state-root.ts"
_STATE_DIR_DECLARATION = re.compile(r'const STATE_DIR_NAME = "([^"]+)";')

# The legacy product name, in any casing. This is the ONLY token the script looks for; the new
# spellings come from the authority file, never from a literal in here.
_LEGACY = re.compile(r"[Cc][Ll][Aa][Ww][Dd][Ii]")
_LEGACY_LOWER = "clawdi"

# An identifier character, for deciding whether an occurrence is part of a larger symbol.
_IDENTIFIER_CHAR = re.compile(r"[A-Za-z0-9_$]")
# The characters an occurrence's *extended* token may reach across: enough to capture a dotted
# wire key, a scoped package name, a hyphenated unit name, a URI scheme and a filesystem path.
_EXTENDED_CHAR = re.compile(r"[A-Za-z0-9_$.@:/~-]")

# THIS SCRIPT'S OWN PATHSPEC. `packages/cli/src` sits entirely outside
# `check_rebrand_residual.py`'s SCAN_PATHSPEC -- deliberately, because that guard reads
# identifiers and strings alike and renaming CLI identifiers is permanent upstream
# merge-conflict surface (D-01). A green residual run therefore says NOTHING about this tree,
# and has never said anything about it. That is why this script carries its own pathspec and
# its own floor instead of borrowing either.
SCAN_PATHSPEC: tuple[str, ...] = (
    "packages/cli/src",
    # Self-exclusions. None of the three is under `packages/cli/src`, so these entries change
    # no result today; they are here so that widening the pathspec later cannot silently make
    # this script's assertion vacuous by letting it read its own rule set, its own substitution
    # source, or the guard suite's red-case fixture data as if they were tree occurrences.
    ":(exclude)scripts/rewrite_brand_strings.py",
    ":(exclude)config/brand-strings.txt",
    ":(exclude)backend/tests/test_rebrand_guards.py",
)

# The file filter, identical to `listNonTestSources` in the committed helper: `.ts`, not
# `.test.ts`. It has to be identical or the two counts would differ over a difference in which
# files were read rather than over a difference in the rule, which is not the drift worth
# catching.
_SCANNED_SUFFIX = ".ts"
_EXCLUDED_SUFFIX = ".test.ts"

# The floor beneath which a run is not a result. MEASURED at the commit that introduced it:
# the pathspec above resolved to 163 non-test TypeScript files. The floor sits well below that
# so ordinary deletions do not trip it and only a pathspec that has stopped resolving does --
# the same smoke-alarm-not-ratchet reasoning `check_rebrand_residual.py` records for its own.
_MINIMUM_SCANNED_FILES = 140

# ---------------------------------------------------------------------------
# The label vocabulary.
# ---------------------------------------------------------------------------
# Two kinds of label and no third. A REPLACE label carries a rewrite; a KEEP label carries a
# named reason. An occurrence that matches neither is UNCLASSIFIED, which is not a label -- it
# is the absence of one, and it blocks the apply path.
#
# The six keep classes phase research enumerated are marked (RESEARCH). The rest were added by
# MEASUREMENT against the tree, because the enumerated six cover 690 of the 1,387 occurrences
# and the remaining 697 are real things with real owners. Every added class names the thing it
# protects and, where the thing is expected to move, the migration that owns moving it -- a
# keep here means "not this class", not "never".

REPLACE_INVOCATION = "REPLACE:invocation"
REPLACE_PRODUCT_NAME = "REPLACE:product-name"
REPLACE_PRINTED_STATE_PATH = "REPLACE:printed-state-path"
REPLACE_DOC_STATE_PATH = "REPLACE:doc-state-path"
REPLACE_EXAMPLE_FILENAME = "REPLACE:example-filename"
# The secret-reference URI scheme and the prose naming it. Until upstream sync step 4 these were
# keeps (the scheme was a 03.1 PERMANENT carve-out); 06.1-11 found the fork's own backend writes
# and its own CLI and backend read every reference, with no outside party, so under the
# phase-wide iforge-only rule (06.1 ledger (i)) the scheme is `iforge://` and an upstream
# `clawdi://` is re-spelled on arrival.
REPLACE_REF_SCHEME = "REPLACE:ref-scheme"

KEEP_WIRE_HEADER = "KEEP:C-5-wire-header"  # (RESEARCH)
KEEP_IDENTIFIER = "KEEP:D-01-identifier"  # (RESEARCH)
KEEP_UPSTREAM_CONTRACT = "KEEP:upstream-contract"  # (RESEARCH)
KEEP_PERSISTED_MARKER = "KEEP:persisted-marker"  # (RESEARCH)
KEEP_GENERATED_FILE_HEADER = "KEEP:generated-file-header"  # (RESEARCH)
KEEP_CARVE_OUT_FIXTURE = "KEEP:carve-out-fixture"  # (RESEARCH)
KEEP_WIRE_SCHEMA_IDENTIFIER = "KEEP:wire-schema-identifier"
KEEP_WIRE_ENDPOINT = "KEEP:wire-endpoint"
KEEP_WIRE_MEDIA_TYPE = "KEEP:wire-media-type"
KEEP_URL_PATH = "KEEP:url-path"
KEEP_PACKAGE_SPEC = "KEEP:package-spec"
KEEP_AGENT_CONFIG_ID = "KEEP:agent-config-id"
KEEP_SERVICE_IDENTITY = "KEEP:service-identity"
KEEP_HOSTED_PLATFORM_ROOT = "KEEP:hosted-platform-root"
KEEP_HOSTED_RUNTIME_USER = "KEEP:hosted-runtime-user"
KEEP_COEXISTENCE_CARVE_OUT = "KEEP:coexistence-carve-out"
KEEP_HOSTED_DOMAIN = "KEEP:hosted-domain"
KEEP_STATE_ROOT_LITERAL = "KEEP:state-root-literal"
KEEP_LEGACY_MIGRATION_TARGET = "KEEP:legacy-migration-target"
KEEP_ON_DISK_ARTIFACT = "KEEP:on-disk-artifact"
KEEP_LOCAL_BINDING = "KEEP:local-binding"

#: Why each keep class is a keep, in one sentence, keyed by label. A keep bucket with no name
#: here is a bucket nobody decided, so ``validate_vocabulary`` refuses it and the apply path
#: refuses with it -- that is the machine form of "the script must not decide a keep silently".
KEEP_REASONS: dict[str, str] = {
    KEEP_WIRE_HEADER: (
        "the skill-sync wire header, matched byte-for-byte by the backend and pinned by "
        "check_rebrand_carveouts.py"
    ),
    KEEP_IDENTIFIER: (
        "a source symbol; D-01 keeps identifiers upstream-aligned because renaming them is "
        "permanent merge-conflict surface"
    ),
    KEEP_UPSTREAM_CONTRACT: (
        "one half of a generated patch string and its validator, which are matched by text and "
        "move together or not at all"
    ),
    KEEP_PERSISTED_MARKER: (
        "a marker written onto user machines and read back; it moves only behind a "
        "legacy-accepting reader"
    ),
    KEEP_GENERATED_FILE_HEADER: (
        "text written into a file on a live host, some of it read back to find the block again"
    ),
    KEEP_CARVE_OUT_FIXTURE: "a guard's own red-case input, which must keep saying what it says",
    KEEP_WIRE_SCHEMA_IDENTIFIER: (
        "a versioned wire-protocol schema id; check_rebrand_carveouts.py asserts all 55 still "
        "resolve"
    ),
    KEEP_WIRE_ENDPOINT: "a served HTTP path; renaming it breaks clients that already call it",
    KEEP_WIRE_MEDIA_TYPE: "a vendor media type carried on the wire",
    KEEP_URL_PATH: (
        "a URL whose host or path carries the name on a server this fork does not control "
        "(for example the upstream repository), so rewriting it would point at nothing"
    ),
    KEEP_PACKAGE_SPEC: (
        "a published package name, scope or install spec that resolves against a registry this "
        "fork does not control"
    ),
    KEEP_AGENT_CONFIG_ID: (
        "the MCP server id and bundled-skill id already written into third-party agent config "
        "on user machines; owned by the agent-identity migration, not by this class"
    ),
    KEEP_SERVICE_IDENTITY: (
        "a systemd unit name, launchd label or unit-name prefix; owned by the service-identity "
        "migration, which needs a legacy list rather than a substitution"
    ),
    KEEP_HOSTED_PLATFORM_ROOT: (
        "a hosted platform root that exists only on a provisioned server, never beside an "
        "upstream install"
    ),
    KEEP_HOSTED_RUNTIME_USER: (
        "the runtime OS user and its home; the value lives in the provisioned image, not in "
        "this repository"
    ),
    KEEP_COEXISTENCE_CARVE_OUT: (
        "a literal check_rebrand_coexistence.py pins as a permanent carve-out; moving it here "
        "would turn that guard red"
    ),
    KEEP_HOSTED_DOMAIN: "a hosted domain; the destinations authority owns every domain move",
    KEEP_STATE_ROOT_LITERAL: (
        "the legacy state-root segment as CODE rather than as printed text; the state-root "
        "migration owns it, and this script must not move a resolver by editing a string"
    ),
    KEEP_LEGACY_MIGRATION_TARGET: (
        "a legacy name a migration path deliberately still reads or still names, so that an "
        "existing install is recognised"
    ),
    KEEP_ON_DISK_ARTIFACT: (
        "a filename this code composes and writes onto a filesystem; it is a path rather than "
        "printed text, so it moves with the code that creates it and not with a string sweep"
    ),
    KEEP_LOCAL_BINDING: (
        "a local variable an upstream function names `clawdi`; it is source, never printed, and "
        "D-01 keeps upstream code upstream-aligned"
    ),
}

# ---------------------------------------------------------------------------
# Pinned keeps -- the handful of sites no structural rule should try to describe.
# ---------------------------------------------------------------------------
# Each entry is ``(path, line_literal, label)`` and the literal is quoted VERBATIM from the
# working tree, the same discipline `check_rebrand_carveouts.py` states for CARVE_OUTS: do not
# paraphrase and do not reconstruct one from memory. A pinned entry that stops matching does
# not silently widen into a structural rule -- its occurrence becomes unclassified, the check
# goes red, and somebody decides again. That is the intended behaviour, not a fragility.
PINNED_KEEPS: tuple[tuple[str, str, str], ...] = (
    # Upstream sync step 1 (06.1-03). The hosted tenant CLI is still the npm `clawdi` package
    # (`hostedCliPayloadPolicySchema` pins `npm:clawdi`), so these two errors name the binary on
    # PATH, not the product.
    (
        "packages/cli/src/runtime/cli-update.ts",
        "`hosted tenant PATH contains an unmanaged clawdi entrypoint: ${HOSTED_TENANT_PATH_CLI}`,",
        KEEP_PACKAGE_SPEC,
    ),
    (
        "packages/cli/src/runtime/cli-update.ts",
        "throw new Error(`hosted tenant PATH contains an unmanaged clawdi symlink target: ${target}`);",
        KEEP_PACKAGE_SPEC,
    ),
    # Upstream #1004's local git transport for a verified Agent Plugin: the commit identity, the
    # branch and the message of a repository Hermes clones from. Metadata of an upstream
    # contract, never printed.
    (
        "packages/cli/src/runtime/hosted-agent-plugin-runtime.ts",
        'GIT_AUTHOR_NAME: "Clawdi Runtime",',
        KEEP_UPSTREAM_CONTRACT,
    ),
    (
        "packages/cli/src/runtime/hosted-agent-plugin-runtime.ts",
        'GIT_COMMITTER_NAME: "Clawdi Runtime",',
        KEEP_UPSTREAM_CONTRACT,
    ),
    (
        "packages/cli/src/runtime/hosted-agent-plugin-runtime.ts",
        '["init", "--quiet", "--initial-branch=clawdi", sourceDir],',
        KEEP_UPSTREAM_CONTRACT,
    ),
    (
        "packages/cli/src/runtime/hosted-agent-plugin-runtime.ts",
        '["-C", sourceDir, "commit", "--quiet", "-m", "Clawdi verified Agent Plugin"],',
        KEEP_UPSTREAM_CONTRACT,
    ),
    (
        "packages/cli/src/runtime/paths.ts",
        'process.env.CLAWDI_HOME || join(userHome, ".clawdi")',
        KEEP_COEXISTENCE_CARVE_OUT,
    ),
    # Upstream sync step 3 (06.1-08). The hosted Baileys credential metadata key: the sibling of
    # `clawdi.managedWhatsAppSocket` in the same hosted-to-CLI credential contract
    # (`creds.additionalData[...]`), written by the hosted side and read back here. The versioned
    # schema id on the next line is already a wire-schema keep; the unversioned key is not, so it
    # is pinned rather than described by a wider rule.
    (
        "packages/cli/src/runtime/whatsapp-upstream-contract.ts",
        'export const CLAWDI_MANAGED_WHATSAPP_CREDENTIAL_METADATA_KEY = "clawdi.managedWhatsAppCredential";',
        KEEP_UPSTREAM_CONTRACT,
    ),
    (
        "packages/cli/src/commands/teardown.ts",
        'p.log.info("Hermes: clawdi entry not present in config.yaml");',
        KEEP_AGENT_CONFIG_ID,
    ),
    (
        "packages/cli/src/commands/teardown.ts",
        "Edit ${configPath} manually and remove the clawdi block under mcp_servers",
        KEEP_AGENT_CONFIG_ID,
    ),
    (
        "packages/cli/src/serve/installer.ts",
        "the clawdi unit name shape. Skips agents not in `AGENT_TYPES` so",
        KEEP_SERVICE_IDENTITY,
    ),
    (
        "packages/cli/src/adapters/claude-code.ts",
        '// "clawdi-web" when the target is "clawdi" — those false positives',
        KEEP_AGENT_CONFIG_ID,
    ),
    (
        "packages/cli/src/runtime/cli-update.ts",
        "const tmp = `${dir}/.clawdi.next.${process.pid}.${Date.now()}`;",
        KEEP_ON_DISK_ARTIFACT,
    ),
    # The one line in the tree that names the OTHER PRODUCT as a product, on purpose. The S1
    # divergence note tells a user that the two state trees have diverged and that removing the
    # legacy one is their own step "once you are done with clawdi" -- upstream clawdi, the thing
    # they may still be running. Rule 8 keeps the `~/.clawdi` occurrence on that line (the line
    # says `legacy`), but the trailing bare `clawdi` is a product name by every structural test
    # this rule set has, so rule 9 would rewrite it to the fork's name and the sentence would
    # advise finishing with the product that just printed it. Pinned rather than described,
    # because "a product name that must not move" is exactly the shape no structural rule here
    # should try to express.
    (
        "packages/cli/src/commands/migrate.ts",
        "Delete ~/.clawdi yourself once you are done with clawdi.",
        KEEP_LEGACY_MIGRATION_TARGET,
    ),
    # The S2 teardown scope note: the second line in the tree that names the OTHER PRODUCT on
    # purpose. It tells a user running both installs that iforge removed only its own entries
    # and left upstream's alone, and it is a PUBLISHED ROW in the phase UI-SPEC's Copywriting
    # Contract ("teardown -- scope note"), pinned again by `teardown.test.ts`. Upstream #1229
    # (sync step 3, 06.1-08) moved Hermes MCP removal out of `commands/teardown.ts` into the
    # adapter lifecycle, so the pin follows the line to `adapters/mcp-lifecycle.ts`. Until this entry
    # it was kept only because `_agent_config_context` matched the word `entries`' neighbours --
    # an incidental structural match, which is to say it was kept for a reason that was not the
    # true one and would have stopped holding the moment that rule was narrowed. It is now kept
    # by name. Unlike the other entries above, the guard that pins it is the UI-SPEC copy
    # contract rather than `check_rebrand_coexistence.py`; the label is the same because the
    # fact is the same -- a literal that must not move because another product owns the word.
    (
        "packages/cli/src/adapters/mcp-lifecycle.ts",
        "Any clawdi entries in this file are left alone.",
        KEEP_COEXISTENCE_CARVE_OUT,
    ),
)


class RewriteError(RuntimeError):
    """The scan could not be completed, or one of its authorities could not be read."""


@dataclass(frozen=True)
class Occurrence:
    """One legacy-brand occurrence and the single decision made about it."""

    path: str
    line: int
    column: int
    matched: str
    label: str
    kind: str
    replacement: str
    context: str
    line_text: str

    @property
    def is_replacement(self) -> bool:
        return self.label.startswith("REPLACE:")

    @property
    def is_invocation_shape(self) -> bool:
        """True when the committed bare-invocation shape matches AT this occurrence.

        Separate from the label on purpose: a label refines a shape. Four occurrences carry
        this shape and are still keeps, and the check compares SHAPES so that a refinement
        cannot be mistaken for the two implementations disagreeing.
        """
        return self.kind == _KIND_INVOCATION

    @property
    def sort_key(self) -> tuple[str, int, int]:
        """Path, then line, then column -- the report's stable order, in one place."""
        return (self.path, self.line, self.column)


# ---------------------------------------------------------------------------
# Authorities.
# ---------------------------------------------------------------------------


def parse_authority(text: str) -> dict[str, str]:
    """Parse ``config/brand-strings.txt``: skip blanks and comments, split on the first ``=``.

    A line with no separator raises rather than being skipped, for the same reason the
    TypeScript reader throws on one: a silently skipped entry is an entry the record can then
    drop without either side noticing.
    """
    pairs: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        separator = line.find("=")
        if separator == -1:
            raise RewriteError(f"malformed brand-strings line: {line}")
        pairs[line[:separator]] = line[separator + 1 :]
    return pairs


def load_brand() -> dict[str, str]:
    """The substitution source of truth, read from the authority file."""
    brand = parse_authority(AUTHORITY.read_text(encoding="utf-8"))
    for required in ("productName", "invocation"):
        if not brand.get(required):
            raise RewriteError(f"config/brand-strings.txt carries no {required}")
    return brand


def committed_invocation_shape() -> re.Pattern[str]:
    """Compile the bare-invocation regex OUT OF the committed test file.

    Reading it rather than retyping it is the whole point: two copies of a regex agree only
    until somebody edits one of them, and the edit nobody notices is exactly the drift this
    script is here to fail on.
    """
    source = (ROOT / INVOCATION_SHAPE_SOURCE).read_text(encoding="utf-8")
    found = _INVOCATION_SHAPE_DECLARATION.search(source)
    if found is None:
        raise RewriteError(
            f"no BARE_INVOCATION_SHAPE declaration in {INVOCATION_SHAPE_SOURCE}: this script "
            "measures the same shape that file does, and it cannot measure a shape that is "
            "not there"
        )
    try:
        return re.compile(found.group(1))
    except re.error as error:  # pragma: no cover - defensive
        raise RewriteError(
            f"the BARE_INVOCATION_SHAPE in {INVOCATION_SHAPE_SOURCE} does not compile here: {error}"
        ) from error


def declared_state_dir_name() -> str:
    """The state-directory name ``state-root.ts`` resolves to, read from that file."""
    source = (ROOT / STATE_ROOT_SOURCE).read_text(encoding="utf-8")
    found = _STATE_DIR_DECLARATION.search(source)
    if found is None:
        raise RewriteError(
            f"no STATE_DIR_NAME declaration in {STATE_ROOT_SOURCE}: the printed-state-path "
            "class rewrites a printed path to the directory the CLI actually resolves, and it "
            "cannot do that against a name it cannot read"
        )
    return found.group(1)


# ---------------------------------------------------------------------------
# The scan.
# ---------------------------------------------------------------------------


def _ls_files(
    pathspec: list[str],
    run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> list[str]:
    """Return the paths matching ``pathspec``, letting git evaluate the pathspec itself.

    ``--cached --others --exclude-standard`` so an uncommitted sweep is visible: the moment
    this script is most wanted is while a sweep is being judged, which is before it is
    committed.
    """
    runner = subprocess.run if run is None else run
    completed = runner(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", *pathspec],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RewriteError(
            f"git ls-files failed while resolving the pathspec: {completed.stderr.strip()}"
        )
    return [path for path in completed.stdout.split("\0") if path]


def scanned_files(
    run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> list[str]:
    """Every file this script reads, sorted, refusing a scan that has stopped resolving.

    The floor guards the direction a hit-based guard cannot see. A pathspec typo, a moved
    directory or an ``:(exclude)`` that swallowed its siblings all produce a clean report over
    an unread tree -- so this script fails when its scanned set SHRINKS, not only when it finds
    something it cannot place.
    """
    files = sorted(
        path
        for path in _ls_files(list(SCAN_PATHSPEC), run=run)
        if path.endswith(_SCANNED_SUFFIX) and not path.endswith(_EXCLUDED_SUFFIX)
    )
    if len(files) < _MINIMUM_SCANNED_FILES:
        raise RewriteError(
            f"the scan pathspec resolved to {len(files)} files, below the floor of "
            f"{_MINIMUM_SCANNED_FILES}: this script refuses to report a result it did not "
            "establish. Either SCAN_PATHSPEC has stopped resolving against this tree, or the "
            "CLI source really did shrink by that much and the floor is what needs editing."
        )
    return files


def tree_hash(files: list[str]) -> str:
    """A content hash over exactly the files this script reads, in path order.

    ``git rev-parse HEAD^{tree}`` would name a COMMITTED tree, and the state this script is run
    against is usually the uncommitted one. Hashing the scanned contents names the tree that
    was actually classified, which is what the apply path has to match.
    """
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256((ROOT / path).read_bytes()).digest())
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Lexical context: is this occurrence in code, in a string, or in a comment?
# ---------------------------------------------------------------------------

CODE, STRING, COMMENT = "code", "string", "comment"

#: The two shape families an occurrence can belong to, before any label refines it.
_KIND_INVOCATION, _KIND_TOKEN = "bare-invocation", "token"


def lexical_contexts(text: str) -> list[str]:
    """Return, per character of ``text``, whether it is code, string or comment.

    A single-line reading of this question is not good enough here and the difference is
    load-bearing: the CLI's `Environment:` help blocks are multi-line template literals, so the
    printed lines inside them carry no quote character of their own. A line-local detector
    calls those lines "code" and the printed-state-path class then misses precisely the strings
    a user reads.

    Template substitutions are treated as part of their template rather than as nested code.
    That costs nothing, because an occurrence inside `${...}` is part of an identifier and the
    identifier rule has already claimed it before context is consulted.
    """
    contexts = [CODE] * len(text)
    index = 0
    size = len(text)
    while index < size:
        char = text[index]
        pair = text[index : index + 2]
        if pair == "//":
            end = text.find("\n", index)
            end = size if end == -1 else end
            for position in range(index, end):
                contexts[position] = COMMENT
            index = end
            continue
        if pair == "/*":
            end = text.find("*/", index + 2)
            end = size if end == -1 else end + 2
            for position in range(index, end):
                contexts[position] = COMMENT
            index = end
            continue
        if char in ("'", '"', "`"):
            quote = char
            position = index + 1
            while position < size:
                if text[position] == "\\":
                    position += 2
                    continue
                if text[position] == quote:
                    position += 1
                    break
                if text[position] == "\n" and quote != "`":
                    break  # an unterminated ordinary string cannot span a line
                position += 1
            for offset in range(index, min(position, size)):
                contexts[offset] = STRING
            index = max(position, index + 1)
            continue
        index += 1
    return contexts


def _line_starts(text: str) -> list[int]:
    """Character offset of the first character of each line, 0-based line index."""
    starts = [0]
    for index, char in enumerate(text):
        if char == "\n":
            starts.append(index + 1)
    return starts


# ---------------------------------------------------------------------------
# Classification.
# ---------------------------------------------------------------------------


def _extended_token(line: str, start: int, end: int) -> str:
    """The occurrence with its surrounding path, scope, scheme or dotted-key characters."""
    left = start
    while left > 0 and _EXTENDED_CHAR.match(line[left - 1]):
        left -= 1
    right = end
    while right < len(line) and _EXTENDED_CHAR.match(line[right]):
        right += 1
    return line[left:right]


def _is_identifier_occurrence(line: str, start: int, end: int) -> bool:
    """True when the occurrence is part of a longer symbol, in either direction."""
    before = line[start - 1] if start > 0 else ""
    after = line[end] if end < len(line) else ""
    return bool(_IDENTIFIER_CHAR.match(before or " ") or _IDENTIFIER_CHAR.match(after or " "))


def classify(
    path: str,
    line_text: str,
    start: int,
    end: int,
    context: str,
    invocation_shape: re.Pattern[str],
) -> tuple[str, str]:
    """Return ``(label, kind)`` for one occurrence, or ``("", "")`` when no rule places it.

    ``kind`` names the shape family the occurrence belongs to and is what the check mode
    compares against the committed helper -- deliberately separate from the label, because a
    label REFINES a shape. Some bare-invocation shapes are refined (the secret-reference prose
    to its own replace class, the agent-config ids to keeps), and folding those two facts into
    one field would make the two
    implementations disagree over a refinement rather than over the rule they share.

    The order below IS the rule set. It runs most-specific-first, every branch names its class,
    and the function falls off the end returning no label rather than defaulting to either
    action.
    """
    matched = line_text[start:end]
    extended = _extended_token(line_text, start, end)
    lower = extended.lower()
    kind = _KIND_INVOCATION if _invocation_at(line_text, start, invocation_shape) else _KIND_TOKEN

    # 1. Pinned sites, before any structural rule can reach them.
    for pinned_path, literal, label in PINNED_KEEPS:
        if path == pinned_path and literal in line_text:
            return label, kind

    # 2. The wire header and the matched validator pair, which are text-matched off-tree.
    if "SKILL_SYNC_PROTOCOL_HEADER" in line_text or "Skill-Sync-Protocol" in line_text:
        return KEEP_WIRE_HEADER, kind
    if (
        "managed WhatsApp socket metadata" in line_text
        or "clawdi.managedWhatsAppSocket" in line_text
    ):
        return KEEP_UPSTREAM_CONTRACT, kind

    # 3. Text written INTO a file on a live host: a banner comment, a drop-in header key, a
    #    generated block heading, or a unit Description. Ahead of the identifier rule on
    #    purpose -- `"# ClawdiHostedRuntimeDropIn=v1"` is spelled as an identifier but is a KEY
    #    IN A GENERATED FILE, and the identifier rule would keep it under a reason that is not
    #    the true one. Two of these are read back to find their own block again; the rest are
    #    decorative today and are still keeps, because moving one means moving the test that
    #    pins it verbatim in the same change, and a string sweep cannot do that.
    if _is_generated_file_text(path, line_text, context):
        return KEEP_GENERATED_FILE_HEADER, kind

    # 4. Part of a longer symbol. D-01 keeps every one of these upstream-aligned, and this is
    #    the largest class in the tree by a factor of two.
    if _is_identifier_occurrence(line_text, start, end):
        return KEEP_IDENTIFIER, kind

    # 4. Things carried on a wire or resolved by a registry, where the name IS the contract.
    #    The middle segment of a schema id may itself be hyphenated
    #    (`clawdi.hosted-runtime.bundle.v2`), so the class runs to the version suffix rather
    #    than stopping at the first separator that is not a dot.
    if "vnd.clawdi" in lower or ("application/" in line_text and "clawdi." in lower):
        return KEEP_WIRE_MEDIA_TYPE, kind
    if re.search(r"clawdi\.[A-Za-z0-9_.-]*\.?v[0-9]+", lower) or re.search(
        r"clawdi\.v[0-9]+\.", lower
    ):
        return KEEP_WIRE_SCHEMA_IDENTIFIER, kind
    # The same schema id written as a SHAPE rather than as a value, which is how prose names
    # the class: `clawdi.<name>.vN`. The metavariable brackets are outside the extended-token
    # alphabet, so this reads the line directly. Prose naming a wire schema is a keep because
    # a comment must describe what the wire carries: the one site in the tree is a comment whose very
    # next sentence says the discriminator NEVER moves, so rewriting it would leave a comment
    # describing a schema nothing writes and nothing validates.
    if re.match(r"clawdi\.<[A-Za-z][A-Za-z0-9_-]*>\.v[A-Za-z0-9]", line_text[start:]):
        return KEEP_WIRE_SCHEMA_IDENTIFIER, kind
    if re.search(r"/v[0-9]+/[a-z/]*clawdi", lower):
        return KEEP_WIRE_ENDPOINT, kind
    # The Agent Plugin Store contract (upstream #1004 onward): the `ai.clawdi` manifest extension
    # namespace, the Store path of the first-party plugin, and the first-party package key. Each
    # is a name the Store and its manifests carry; renaming one stops the runtime reading them.
    if re.search(r"\bai\.clawdi\b(?!\.)", lower):
        return KEEP_UPSTREAM_CONTRACT, kind
    if re.search(r"(?:^|[\"'`/])v[0-9]+/plugins/clawdi\b", lower):
        return KEEP_UPSTREAM_CONTRACT, kind
    if "first-party clawdi package" in line_text.lower():
        return KEEP_UPSTREAM_CONTRACT, kind
    if "@clawdi/" in lower:
        return KEEP_PACKAGE_SPEC, kind
    if (
        "clawdi@" in lower
        or "npm:clawdi" in lower
        or lower.endswith("/bin/clawdi")
        or "-g clawdi" in line_text
    ):
        return KEEP_PACKAGE_SPEC, kind
    if re.search(r"clawdi\.(ai|dev)\b", lower):
        return KEEP_HOSTED_DOMAIN, kind

    # 5. The secret-reference scheme, and the prose that names it. Both move together (a message
    #    advertising a scheme the parser does not accept is a defect), and both now REPLACE: the
    #    fork writes and reads `iforge://` (06.1-11). The escaped form is the same scheme inside
    #    a regex literal. A URL that merely carries the name in its host or path is a keep.
    # `clawdi:` quoted alone is the same scheme as a WHATWG `URL.protocol` value.
    if re.match(r"clawdi:(?:\\?/\\?/|[\"'`])", line_text[start:], re.IGNORECASE):
        return REPLACE_REF_SCHEME, kind
    if re.match(r"clawdi[ \t]+(reference|references|ref|refs)\b", line_text[start:], re.IGNORECASE):
        return REPLACE_REF_SCHEME, kind
    if re.search(r"[a-z]+://[a-z0-9._/-]*clawdi", lower):
        return KEEP_URL_PATH, kind

    # 6. An occurrence that constitutes a WHOLE quoted span is a key, never copy. Prose has
    #    other words in it; `id: "clawdi"`, `removeAllYamlBlocks(section, "clawdi")` and the
    #    backticked `clawdi` in a comment about them are all naming the same identifier that
    #    third-party agent config on user machines already holds.
    if _is_whole_quoted_span(line_text, start, end):
        return (
            KEEP_SERVICE_IDENTITY if _service_context(line_text) else KEEP_AGENT_CONFIG_ID
        ), kind
    if (
        re.search(r"(skills|hosted-versions)/[a-z0-9/-]*clawdi", lower)
        or "clawdi/SKILL.md" in line_text
    ):
        return KEEP_AGENT_CONFIG_ID, kind

    # 7. Names that exist on a machine somewhere and are read back off it.
    if re.search(r"clawdi[-.](service|plist|timer|socket)", lower) or "ai.clawdi." in lower:
        return KEEP_SERVICE_IDENTITY, kind
    if re.search(r"[\"'`>< ]\.clawdi-", line_text) or ".clawdi-managed" in lower:
        return KEEP_PERSISTED_MARKER, kind
    if _is_marker_constant(line_text):
        return KEEP_GENERATED_FILE_HEADER, kind
    if re.search(r"^/(etc|var|run|opt|srv|usr)/", extended) or re.search(
        r"/(lib|cache)/clawdi", lower
    ):
        return KEEP_HOSTED_PLATFORM_ROOT, kind
    if "/home/clawdi" in lower:
        return KEEP_HOSTED_RUNTIME_USER, kind
    if re.search(r"clawdi(\.[a-z0-9_-]+)*\.(ya?ml|json|toml|jsonl)", lower):
        return KEEP_PERSISTED_MARKER, kind
    if re.search(r"clawdi[:\-]\$", extended):
        return KEEP_PERSISTED_MARKER, kind
    if _agent_config_context(line_text, extended, kind):
        return KEEP_AGENT_CONFIG_ID, kind
    if re.search(r"clawdi-([a-z]|$)", lower):
        return (
            KEEP_SERVICE_IDENTITY if _service_context(line_text) else KEEP_PERSISTED_MARKER
        ), kind

    # 8. The legacy state root. A printed one is this class's own defect and moves; a resolved
    #    one is the state-root migration's and must not be moved by editing a string.
    if re.search(r"\.clawdi\b", lower):
        if _legacy_migration_context(line_text):
            return KEEP_LEGACY_MIGRATION_TARGET, kind
        if ".env.clawdi" in lower:
            return REPLACE_EXAMPLE_FILENAME, kind
        if context == STRING:
            return REPLACE_PRINTED_STATE_PATH, kind
        if context == COMMENT:
            return REPLACE_DOC_STATE_PATH, kind
        return KEEP_STATE_ROOT_LITERAL, kind

    # 8b. The reader half of a moved writer. When a writer's spelling moves, the legacy one has
    #     to stay READABLE until every installed host has converged and drained it, and this
    #     repository already spells that as a module-private `LEGACY_*` declaration
    #     (`LEGACY_RESERVED_SKILL_ARCHIVE_MARKER_PREFIX`, `LEGACY_STATE_DIR_NAME`). Sweeping one
    #     of those would delete the dual read while leaving the code that looks like it has one.
    #     Placed last among the keeps on purpose: it can only rescue an occurrence rule 9 would
    #     otherwise rewrite, so it cannot silently relabel a keep some earlier rule already owns.
    if _is_legacy_dual_read_declaration(line_text):
        return KEEP_LEGACY_MIGRATION_TARGET, kind

    # 8c. A local binding named `clawdi` in upstream code (upstream #1004's
    #     `const clawdi = clawdiExtensionSchema.safeParse(extension)` and its member reads). The
    #     lexer already placed it in CODE, outside every string and comment, so a bare `clawdi`
    #     that is declared or dereferenced there is a variable, not the product naming itself.
    #     Placed after every other keep so it can only place an occurrence nothing else did.
    if context == CODE and kind == _KIND_TOKEN and _is_local_binding(line_text, start, end):
        return KEEP_LOCAL_BINDING, kind

    # 9. What is left is the product naming itself: the class this script exists for.
    if kind == "bare-invocation":
        return REPLACE_INVOCATION, kind
    if matched == "Clawdi" or (context in (STRING, COMMENT) and matched == _LEGACY_LOWER):
        return REPLACE_PRODUCT_NAME, kind

    return "", kind


def _is_local_binding(line_text: str, start: int, end: int) -> bool:
    """True for a code token declared as a local (`const clawdi =`) or dereferenced (`clawdi.x`)."""
    if re.search(r"\b(?:const|let|var)\s+$", line_text[:start]) and re.match(
        r"\s*=", line_text[end:]
    ):
        return True
    return bool(re.match(r"\.[A-Za-z_][A-Za-z0-9_]*", line_text[end:]))


def _is_generated_file_text(path: str, line_text: str, context: str) -> bool:
    """True when the occurrence sits in text this code writes into a file somewhere else.

    Three signals, all narrow. A string literal whose content opens with a `#` or an HTML
    comment is a banner, a drop-in key or a generated heading -- printed CLI output does not
    open that way. `Generated by` and `Managed by` are the two banner wordings in the tree. And
    a `description:` PROPERTY in the systemd reconciler is a field of a desired-state record the
    reconciler compares.

    The third signal requires the colon. It used to accept the word `description` anywhere on a
    line in a systemd/launchd/installer path, in any case -- which held a decorative
    `Description=` line inside a unit TEMPLATE, a string this code writes but never reads back
    and never compares. A `=` assignment is a rendered unit body; a `description:` property is a
    record field. Only the second is the shape this signal was written for.
    """
    if context == STRING and re.search(r"[\"'`]\s*(#|<!--)", line_text):
        return True
    if re.search(r"\b(Generated|Managed) by\b", line_text):
        return True
    return bool(
        re.search(r"(systemd|launchd|installer)", path)
        and re.search(r"\bdescription\b[ \t]*:", line_text, re.IGNORECASE)
    )


def _is_marker_constant(line_text: str) -> bool:
    """True for a line declaring a block marker written into a file and matched back out."""
    return bool(re.search(r"\b[A-Z][A-Z0-9_]*(MARKER|BLOCK_START|BLOCK_END|SENTINEL)\b", line_text))


_QUOTES = ("'", '"', "`")


def _is_whole_quoted_span(line_text: str, start: int, end: int) -> bool:
    """True when the occurrence, with at most a trailing ``:`` or ``-``, IS a whole quoted span.

    The discriminator between an identifier and copy, and it needs no vocabulary of its own: a
    sentence has other words in it, a key does not. A trailing separator is allowed because the
    same key appears as a YAML key (``clawdi:``) and as a unit-name prefix (``clawdi-``), and
    both are the identifier rather than a mention of the product.
    """
    if start == 0 or line_text[start - 1] not in _QUOTES:
        return False
    quote = line_text[start - 1]
    tail = end
    if tail < len(line_text) and line_text[tail] in (":", "-"):
        tail += 1
    return tail < len(line_text) and line_text[tail] == quote


def _service_context(line_text: str) -> bool:
    """True when the line is talking about OS service identity rather than about a name."""
    return bool(
        re.search(
            r"(\b(unit|units|service|timer|socket|plist)\b|systemd|launchd|daemon-reload)",
            line_text,
            re.IGNORECASE,
        )
    )


def _agent_config_context(line_text: str, extended: str, kind: str) -> bool:
    """True for the MCP server id / bundled-skill id already written into third-party config.

    ``kind`` is the occurrence's own shape, and it is consulted FIRST. An identifier written
    into a config file is a key: it is never followed by a subcommand word. An invocation
    always is. So a bare-invocation occurrence cannot be one of the ids this rule exists for,
    whatever vocabulary the rest of the line happens to carry -- and the rest of the line is
    exactly what used to decide it. `command: "clawdi mcp"` sat beside `"iforge daemon run"` in
    one object literal and was kept because the substring `mcp` appeared elsewhere on the line;
    `clawdi setup` was kept by `adapterRegistry` matching `adapter`; `clawdi skill install` was
    kept by the word `skill` in the surrounding sentence. The vocabulary below is not the
    defect and is unchanged: the defect was consulting it about the wrong span.
    """
    if kind == _KIND_INVOCATION:
        return False
    if not re.fullmatch(r"clawdi(-mcp)?[:\-]?", extended, re.IGNORECASE):
        return False
    return bool(
        re.search(
            r"\b(mcp|mcp_servers|skill|skills|skillsDir|targetDir|registeredMcp|adapter)",
            line_text,
            re.IGNORECASE,
        )
    )


def _is_legacy_dual_read_declaration(line_text: str) -> bool:
    """True for a declaration whose own identifier says it holds the LEGACY spelling.

    The identifier is the evidence, not a keyword in a comment: a constant named `LEGACY_...`
    exists so a reader can still recognise what an older writer wrote, and its value is the old
    spelling BY DEFINITION. Rewriting it would leave the reader matching a string nothing on any
    disk carries, which reads as a working dual read and is not one.
    """
    return bool(re.search(r"\bLEGACY_[A-Z0-9_]+\b[^=]*=", line_text))


def _legacy_migration_context(line_text: str) -> bool:
    """True when the line deliberately names the legacy state root AS legacy."""
    return bool(re.search(r"\b(legacy|LEGACY|migrat|Migrat)", line_text))


def _invocation_at(line_text: str, start: int, shape: re.Pattern[str]) -> bool:
    """True when a bare-invocation match of the committed shape begins at ``start``."""
    return any(found.start() == start for found in shape.finditer(line_text))


def rewrite_for(label: str, matched: str, brand: dict[str, str], state_dir: str) -> str:
    """The text one replacement label writes in place of ``matched``.

    Every value comes from the authority file. The state-directory case is derived from the
    product name and then CHECKED against what `state-root.ts` resolves, rather than being
    written out again here -- a second literal is a second thing to keep in step.
    """
    if label in (REPLACE_INVOCATION, REPLACE_REF_SCHEME):
        return brand["invocation"]
    if label == REPLACE_PRODUCT_NAME:
        return brand["productName"]
    if label in (REPLACE_PRINTED_STATE_PATH, REPLACE_DOC_STATE_PATH, REPLACE_EXAMPLE_FILENAME):
        if not state_dir.startswith("."):  # pragma: no cover - defensive
            raise RewriteError(f"the declared state directory {state_dir!r} is not a dot directory")
        return state_dir[1:]
    raise RewriteError(f"no rewrite is defined for {label}")  # pragma: no cover - defensive


def classify_contents(
    contents: dict[str, str],
    brand: dict[str, str],
    invocation_shape: re.Pattern[str],
    state_dir: str,
) -> list[Occurrence]:
    """Classify every occurrence in ``contents``, in stable path/line/column order.

    ``contents`` maps repository-relative paths to file text. Tests inject a mapping so a red
    case never has to write a corrupted file to the working tree, exactly as
    ``check_rebrand_carveouts.py`` already does -- a guard proven by mutating the tree is
    unrunnable in parallel and is one crashed test away from a real edit.
    """
    occurrences: list[Occurrence] = []
    for path in sorted(contents):
        text = contents[path]
        contexts = lexical_contexts(text)
        starts = _line_starts(text)
        for index, line_text in enumerate(text.split("\n")):
            offset = starts[index] if index < len(starts) else len(text)
            for found in _LEGACY.finditer(line_text):
                position = offset + found.start()
                context = contexts[position] if position < len(contexts) else CODE
                label, kind = classify(
                    path, line_text, found.start(), found.end(), context, invocation_shape
                )
                replacement = (
                    rewrite_for(label, found.group(0), brand, state_dir)
                    if label.startswith("REPLACE:")
                    else ""
                )
                occurrences.append(
                    Occurrence(
                        path=path,
                        line=index + 1,
                        column=found.start() + 1,
                        matched=found.group(0),
                        label=label,
                        kind=kind,
                        replacement=replacement,
                        context=context,
                        line_text=line_text,
                    )
                )
    occurrences.sort(key=lambda occurrence: occurrence.sort_key)
    return occurrences


#: The directory `listNonTestSources` walks in the committed test helper.
HELPER_SOURCE_ROOT = "packages/cli/src"


def helper_sources() -> dict[str, str]:
    """`listNonTestSources()` from the committed test, re-implemented over the FILESYSTEM.

    Deliberately not the git enumeration ``scanned_files`` uses. If both sides read the same
    file list, the comparison in check mode only ever exercises the regex, and a pathspec that
    quietly stopped resolving to the same set would pass. Two enumerations that disagree is the
    other half of the drift worth catching.
    """
    root = ROOT / HELPER_SOURCE_ROOT
    sources: dict[str, str] = {}
    for path in sorted(root.rglob(f"*{_SCANNED_SUFFIX}")):
        if path.name.endswith(_EXCLUDED_SUFFIX) or not path.is_file():
            continue
        sources[str(path.relative_to(ROOT))] = path.read_text(encoding="utf-8")
    return sources


def bare_invocation_lines(contents: dict[str, str], shape: re.Pattern[str]) -> list[str]:
    """``<path>:<line>`` for every line carrying at least one bare invocation.

    This is `measureDeferredInvocations()` from the committed test, re-implemented at LINE
    level exactly as that helper counts, so the two can be compared. It is deliberately NOT
    built out of the occurrence records above: two implementations of one rule that agree are
    evidence, and one implementation compared against itself is a tautology.
    """
    hits: list[str] = []
    for path in sorted(contents):
        for index, line_text in enumerate(contents[path].split("\n")):
            if shape.search(line_text):
                hits.append(f"{path}:{index + 1}")
    return hits


def validate_vocabulary(occurrences: list[Occurrence]) -> None:
    """Refuse a keep bucket that carries no name and no stated reason."""
    for occurrence in occurrences:
        if not occurrence.label.startswith("KEEP:"):
            continue
        name = occurrence.label.split(":", 1)[1]
        if not name or occurrence.label not in KEEP_REASONS:
            raise RewriteError(
                f"{occurrence.path}:{occurrence.line} was kept under {occurrence.label!r}, which "
                "names no reason in KEEP_REASONS: a keep class nobody wrote down is a decision "
                "nobody made"
            )


def unclassified(occurrences: list[Occurrence]) -> list[Occurrence]:
    """The occurrences no rule placed. Reported, and blocking for the apply path."""
    return [occurrence for occurrence in occurrences if not occurrence.label]


# ---------------------------------------------------------------------------
# Reporting.
# ---------------------------------------------------------------------------

_REPORT_HEADER = "# rewrite_brand_strings classify report"


def render_report(tree: str, files: list[str], occurrences: list[Occurrence]) -> str:
    """The classify report: a header of totals, then one line per occurrence, stably ordered.

    Byte-identical between two runs at the same tree, so a diff of two reports shows real
    changes and nothing else. That is why the order is path/line/column and why the bucket
    counts are emitted sorted rather than in first-seen order.
    """
    buckets: dict[str, int] = {}
    for occurrence in occurrences:
        label = occurrence.label or "UNCLASSIFIED"
        buckets[label] = buckets.get(label, 0) + 1
    replacements = sum(1 for occurrence in occurrences if occurrence.is_replacement)

    lines = [
        _REPORT_HEADER,
        f"tree {tree}",
        f"scanned-files {len(files)}",
        f"occurrences {len(occurrences)}",
        f"replacements {replacements}",
        f"unclassified {buckets.get('UNCLASSIFIED', 0)}",
    ]
    for label in sorted(buckets):
        lines.append(f"bucket {label} {buckets[label]}")
    lines.append("--")
    for occurrence in occurrences:
        label = occurrence.label or "UNCLASSIFIED"
        lines.append(
            f"{occurrence.path}:{occurrence.line}:{occurrence.column}\t{label}\t"
            f"{occurrence.kind}\t{occurrence.context}\t{occurrence.matched}\t"
            f"{occurrence.replacement}\t{occurrence.line_text.strip()}"
        )
    return "\n".join(lines) + "\n"


def report_tree(report_text: str) -> str:
    """The tree hash a classify report was produced at."""
    for line in report_text.splitlines():
        if line.startswith("tree "):
            return line.split(" ", 1)[1].strip()
    raise RewriteError("the classify report names no tree hash")


def report_unclassified_count(report_text: str) -> int:
    """The unclassified count a classify report recorded."""
    for line in report_text.splitlines():
        if line.startswith("unclassified "):
            return int(line.split(" ", 1)[1].strip())
    raise RewriteError("the classify report names no unclassified count")


# ---------------------------------------------------------------------------
# Applying.
# ---------------------------------------------------------------------------


def apply_to_text(text: str, occurrences: list[Occurrence]) -> str:
    """Return ``text`` with every replacement in ``occurrences`` applied, right to left.

    Right to left so an earlier column's rewrite cannot move a later column's offset, which is
    what makes two decisions on one line safe to apply.
    """
    lines = text.split("\n")
    for occurrence in sorted(occurrences, key=lambda item: item.sort_key, reverse=True):
        if not occurrence.is_replacement:
            continue
        index = occurrence.line - 1
        if index >= len(lines):  # pragma: no cover - defensive
            raise RewriteError(f"{occurrence.path}:{occurrence.line} is past the end of the file")
        line = lines[index]
        start = occurrence.column - 1
        end = start + len(occurrence.matched)
        if line[start:end] != occurrence.matched:
            raise RewriteError(
                f"{occurrence.path}:{occurrence.line}:{occurrence.column} no longer carries "
                f"{occurrence.matched!r}; the tree moved under the report"
            )
        lines[index] = line[:start] + occurrence.replacement + line[end:]
    return "\n".join(lines)


def assert_apply_is_allowed(
    report_text: str, current: str, occurrences: list[Occurrence]
) -> None:
    """The three refusals that gate the apply path. Reads nothing and writes nothing.

    Split out from ``apply_report`` so the refusals can be proven with injected values rather
    than by pointing a writing path at the working tree. A guard whose refusal was demonstrated
    by letting it nearly rewrite the repository is not a demonstration anybody will re-run.

    All three are fail-closed. A report from a different tree describes edits at offsets that
    have moved. An unclassified occurrence means the rule set did not cover this tree, and
    writing anyway would be the blanket substitution wearing the classifier's name. A keep
    bucket with no reason is a decision nobody made.
    """
    recorded = report_tree(report_text)
    if current != recorded:
        raise RewriteError(
            f"the classify report was produced at tree {recorded[:12]} and this tree is "
            f"{current[:12]}: re-run --classify before --apply, because a stale report names "
            "offsets that have moved"
        )
    if report_unclassified_count(report_text) or unclassified(occurrences):
        raise RewriteError(
            "the classify report carries unclassified occurrences: apply is blocked until every "
            "one of them is either given a keep class or covered by a replacement rule"
        )
    validate_vocabulary(occurrences)


def assert_sweep_is_complete(occurrences: list[Occurrence]) -> None:
    """Refuse a tree that still carries an occurrence this script would rewrite.

    THE SUCCESSOR TO `DEFERRED_INVOCATION_BUDGET`. Until plan 03.3-11 ran the sweep, pending
    rewrites were REPORTED rather than refused, for a stated reason: a check that failed on a
    pending rewrite would have been red from the day it was written until the sweep landed,
    which is a check nobody can run. The sweep has landed, the pending count is zero, and that
    reason has expired -- so this is now enforced, and the equality ratchet it replaces was
    removed in the commit that proved this red and green.

    Strictly stronger than the ratchet was, in both directions that matter. The ratchet counted
    a mechanical SHAPE and permitted 272 of them; this covers all five replacement classes --
    the bare invocation, the product name, the printed and documented state paths, and the
    example filename -- and its ceiling is zero. A new occurrence arriving on an upstream merge
    is refused here whichever of the five it belongs to, and the failure names the class, which
    the count never could.

    Pure, like ``assert_apply_is_allowed``: it reads nothing and writes nothing, so its red case
    is provable with injected occurrences rather than by mutating the working tree.
    """
    pending = [occurrence for occurrence in occurrences if occurrence.is_replacement]
    if not pending:
        return
    for occurrence in pending[:20]:
        print(
            f"pending rewrite: {occurrence.path}:{occurrence.line}:{occurrence.column} "
            f"{occurrence.label} -> {occurrence.replacement!r}  {occurrence.line_text.strip()}",
            file=sys.stderr,
        )
    raise RewriteError(
        f"{len(pending)} occurrences still carry a spelling this script would rewrite. Either "
        "run --classify then --apply to sweep them, or -- if one of them must NOT move -- give "
        "it a named keep class in the rule set. Do not leave it pending: pending is what this "
        "check now refuses, because the class it bounds has no plan left to sweep it"
    )


def apply_report(report_text: str, files: list[str], occurrences: list[Occurrence]) -> int:
    """Write the tree once ``assert_apply_is_allowed`` has passed. Returns the files rewritten."""
    assert_apply_is_allowed(report_text, tree_hash(files), occurrences)

    by_path: dict[str, list[Occurrence]] = {}
    for occurrence in occurrences:
        if occurrence.is_replacement:
            by_path.setdefault(occurrence.path, []).append(occurrence)
    written = 0
    for path, items in sorted(by_path.items()):
        target = ROOT / path
        text = target.read_text(encoding="utf-8")
        updated = apply_to_text(text, items)
        if updated == text:  # pragma: no cover - defensive
            continue
        target.write_text(updated, encoding="utf-8")
        written += 1
    return written


# ---------------------------------------------------------------------------
# Modes.
# ---------------------------------------------------------------------------


def read_tree() -> tuple[list[str], dict[str, str]]:
    """The scanned file list and its contents, as one snapshot."""
    files = scanned_files()
    return files, {path: (ROOT / path).read_text(encoding="utf-8") for path in files}


def classify_tree() -> tuple[list[str], dict[str, str], list[Occurrence], re.Pattern[str]]:
    """Everything the three modes share: authorities, scan, and one classification pass."""
    brand = load_brand()
    shape = committed_invocation_shape()
    state_dir = declared_state_dir_name()
    files, contents = read_tree()
    occurrences = classify_contents(contents, brand, shape, state_dir)
    return files, contents, occurrences, shape


def run_classify() -> str:
    files, _contents, occurrences, _shape = classify_tree()
    return render_report(tree_hash(files), files, occurrences)


def run_check() -> str:
    """Fail when this script and the tree disagree, or when the sweep is not complete.

    The second half of that sentence arrived with plan 03.3-11 and is the reason
    `DEFERRED_INVOCATION_BUDGET` could be retired rather than merely deleted. Until the sweep
    ran, pending rewrites were REPORTED and not refused, because a check that failed on one
    would have been red from the day it was written until the sweep landed. The sweep has
    landed; `assert_sweep_is_complete` now enforces zero, and the equality ratchet that stood
    in for it is gone.
    """
    files, _contents, occurrences, shape = classify_tree()
    validate_vocabulary(occurrences)

    stranded = unclassified(occurrences)
    if stranded:
        for occurrence in stranded[:20]:
            print(
                f"unclassified: {occurrence.path}:{occurrence.line}:{occurrence.column} "
                f"{occurrence.line_text.strip()}",
                file=sys.stderr,
            )
        raise RewriteError(
            f"{len(stranded)} occurrences match no rule. Every occurrence must carry a "
            "replacement or a named keep class; an unclassified one blocks the apply path "
            "rather than defaulting to either action"
        )

    mine = sorted({f"{o.path}:{o.line}" for o in occurrences if o.is_invocation_shape})
    theirs = sorted(bare_invocation_lines(helper_sources(), shape))
    if mine != theirs:
        only_mine = sorted(set(mine) - set(theirs))
        only_theirs = sorted(set(theirs) - set(mine))
        for hit in (only_mine + only_theirs)[:20]:
            print(f"invocation drift: {hit}", file=sys.stderr)
        raise RewriteError(
            f"this script finds {len(mine)} bare-invocation lines and the committed helper in "
            f"{INVOCATION_SHAPE_SOURCE} finds {len(theirs)}. Two implementations of one rule "
            "that disagree mean one of them has drifted from the tree"
        )

    state_dir = declared_state_dir_name()
    brand = load_brand()
    if state_dir != f".{brand['productName']}":
        raise RewriteError(
            f"{STATE_ROOT_SOURCE} resolves the state root to {state_dir!r} while the brand "
            f"authority names the product {brand['productName']!r}. The printed-state-path "
            "class rewrites a printed path to the directory the CLI actually uses, so these "
            "two cannot disagree without the sweep printing a path nothing resolves"
        )

    assert_sweep_is_complete(occurrences)

    return (
        f"rewrite brand strings passed: {len(files)} files, {len(occurrences)} occurrences, "
        f"0 unclassified, {len(theirs)} bare-invocation lines, 0 pending rewrites"
    )


# ---------------------------------------------------------------------------
# Self-test.
# ---------------------------------------------------------------------------
# The rule set's own red and green, runnable without a test framework and without writing
# anything. `backend/tests/test_rebrand_guards.py` owns this script's MODE behaviour; these nine
# cases own the two rules plan 03.3-21 narrowed, in both directions, because a narrowing is only
# safe if the class it stops holding and the class it must keep holding are asserted together.

#: How many cases ``--self-test`` reports. A run that reports fewer has LOST a case rather than
#: passed it, and a silently shorter suite is the failure mode a bare exit status cannot show.
_SELF_TEST_CASES = 9

#: The reconciler, whose `description:` PROPERTIES are the keeps the third generated-file-text
#: signal exists for, and the installer, whose rendered `Description=` unit body is the class it
#: used to hold by accident. Case 7 asserts both halves, which is what makes the narrowing's
#: reversion visible: the keep count alone cannot go red on it, because the loose rule kept the
#: properties too.
_RECONCILER_SOURCE = "packages/cli/src/runtime/runtime-systemd-reconciliation.ts"
_INSTALLER_SOURCE = "packages/cli/src/serve/installer.ts"


def _restore_legacy_invocation(line_text: str, invocation: str) -> str:
    """``line_text`` as it read BEFORE the sweep: a bare ``<fork> <verb>`` written back as legacy.

    The four repaired sites are asserted against the REAL files, so each case keeps reading the
    real surrounding vocabulary -- `mcp` beside the capability record, `adapterRegistry` beside
    the printed error, `skill` in the imported-skills sentence. That vocabulary is precisely
    what used to decide these lines, and a synthetic copy of it would drift away from the thing
    being guarded. But once the sweep lands, the legacy spelling is gone from those lines, and a
    case asserting "this classifies as a rewrite" would go red for having succeeded. So the
    probe restores the legacy spelling and asks the durable question instead: if this line said
    ``clawdi <verb>`` again, would the rule set rewrite it NOW? Before the sweep the substitution
    is a no-op and the case reads the line verbatim.
    """
    pattern = rf"(?<![A-Za-z0-9_.\-/]){re.escape(invocation)}(?=[ \t]+[a-z])"
    return re.sub(pattern, _LEGACY_LOWER, line_text)


def _probe_occurrences(
    path: str,
    anchor: str,
    brand: dict[str, str],
    shape: re.Pattern[str],
    state_dir: str,
) -> list[Occurrence]:
    """Every occurrence on the one real line carrying ``anchor``, classified after the probe."""
    lines = (ROOT / path).read_text(encoding="utf-8").split("\n")
    hits = [index for index, line_text in enumerate(lines) if anchor in line_text]
    if len(hits) != 1:
        raise RewriteError(
            f"{path}: the anchor {anchor!r} matches {len(hits)} lines rather than exactly one; "
            "a case that cannot name its own line is not a case"
        )
    index = hits[0]
    lines[index] = _restore_legacy_invocation(lines[index], brand["invocation"])
    occurrences = classify_contents({path: "\n".join(lines)}, brand, shape, state_dir)
    return [item for item in occurrences if item.line == index + 1]


def _synthetic_occurrences(
    path: str,
    line_text: str,
    brand: dict[str, str],
    shape: re.Pattern[str],
    state_dir: str,
) -> list[Occurrence]:
    """Classify one synthetic line AS IF it sat in ``path``, writing nothing to the tree.

    The path matters because two rules read it, so a synthetic case for one of those rules has
    to be asked about a real path. Injected contents rather than a temporary file, the same
    discipline ``classify_contents`` states: a guard proven by mutating the tree is one crashed
    run away from a real edit.
    """
    return classify_contents({path: line_text + "\n"}, brand, shape, state_dir)


def _labels(occurrences: list[Occurrence]) -> list[str]:
    return [occurrence.label for occurrence in occurrences]


def _kinds(occurrences: list[Occurrence]) -> list[str]:
    return [occurrence.kind for occurrence in occurrences]


def _self_test_cases() -> list[tuple[str, bool, str]]:
    """The nine cases, each ``(name, passed, observed)``. Reads the tree; writes nothing."""
    brand = load_brand()
    shape = committed_invocation_shape()
    state_dir = declared_state_dir_name()
    invocation_keep = [REPLACE_INVOCATION]

    def probe(path: str, anchor: str) -> list[Occurrence]:
        return _probe_occurrences(path, anchor, brand, shape, state_dir)

    def synthetic(path: str, line_text: str) -> list[Occurrence]:
        return _synthetic_occurrences(path, line_text, brand, shape, state_dir)

    cases: list[tuple[str, bool, str]] = []

    # 1. The capability record: the type literal and the value, which move together or break the
    #    build. Both were kept because `mcp` appeared elsewhere on their own line.
    declaration = probe(
        "packages/cli/src/commands/capabilities.ts", "mcp: { available: true; command:"
    )
    value = probe("packages/cli/src/commands/capabilities.ts", "mcp: { available: true, command:")
    observed = _labels(declaration) + _labels(value)
    cases.append(
        (
            "1 capabilities.ts mcp.command (type literal and value) is a rewrite",
            observed == invocation_keep * 2,
            f"{observed}",
        )
    )

    # 2. The printed setup error, kept because `adapterRegistry` matched the rule's `adapter`.
    pushed = probe("packages/cli/src/commands/push.ts", "No environment registered for")
    cases.append(
        (
            "2 push.ts printed setup error is a rewrite",
            _labels(pushed) == invocation_keep,
            f"{_labels(pushed)}",
        )
    )

    # 3. Two occurrences on ONE line, kept because the sentence around them says `Skills`. The
    #    second must be classified on its own and not shadowed by the first.
    pulled = probe("packages/cli/src/commands/pull.ts", "Imported Skills remain Project-owned")
    columns = [occurrence.column for occurrence in pulled]
    cases.append(
        (
            "3 pull.ts both skill-subcommand occurrences on one line are rewrites",
            _labels(pulled) == invocation_keep * 2 and columns == sorted(columns),
            f"{_labels(pulled)} at columns {columns}",
        )
    )

    # 4. The `--keep-skill` help text, wrong twice over since 03.3-10 moved the bundled skill id.
    helped = probe("packages/cli/src/index.ts", '"--keep-skill"')
    cases.append(
        (
            "4 index.ts --keep-skill help text is a rewrite",
            _labels(helped) == invocation_keep,
            f"{_labels(helped)}",
        )
    )

    # 5. The deliberate mention, kept by the PIN that names it rather than by a structural rule
    #    that happened to match. The label is the assertion: `KEEP:agent-config-id` here would
    #    mean the line survived for a reason that is not the true one.
    scoped = probe(
        "packages/cli/src/adapters/mcp-lifecycle.ts", "entries in this file are left alone."
    )
    cases.append(
        (
            "5 mcp-lifecycle.ts scope note is kept by the coexistence pin",
            _labels(scoped) == [KEEP_COEXISTENCE_CARVE_OUT],
            f"{_labels(scoped)}",
        )
    )

    # 6. The class narrowing 1 was written for, which it must NOT widen into: an identifier
    #    written into third-party config. A whole quoted span (the bundled-skill id as a bare
    #    key) and the hyphenated MCP server id, both token-shaped, both still keeps.
    key = synthetic(
        "packages/cli/src/commands/setup.ts", '\tconst dir = join(home, "skills", "clawdi");'
    )
    server = synthetic(
        "packages/cli/src/commands/setup.ts", '\tconst registeredMcp = "clawdi-mcp";'
    )
    observed = _labels(key) + _labels(server)
    cases.append(
        (
            "6 agent-config ids (quoted key, hyphenated server id) are still keeps",
            observed == [KEEP_AGENT_CONFIG_ID, KEEP_AGENT_CONFIG_ID]
            and _kinds(key) + _kinds(server) == [_KIND_TOKEN, _KIND_TOKEN],
            f"{observed} as {_kinds(key) + _kinds(server)}",
        )
    )

    # 7. Narrowing 2, both directions. The reconciler's five `description:` PROPERTIES are
    #    desired-state fields it compares and stay kept; a rendered `Description=` unit body in
    #    an installer path is a string nothing reads back and must fall through to the rewrite.
    #    The keep half alone cannot go red on a reverted narrowing -- the loose rule kept these
    #    too -- which is why the red half sits in the same case.
    reconciled = classify_contents(
        {_RECONCILER_SOURCE: (ROOT / _RECONCILER_SOURCE).read_text(encoding="utf-8")},
        brand,
        shape,
        state_dir,
    )
    kept = [item for item in reconciled if item.label == KEEP_GENERATED_FILE_HEADER]
    unit_body = synthetic(_INSTALLER_SOURCE, '\t\t"Description=clawdi daemon",')
    cases.append(
        (
            "7 reconciler description: properties keep, installer Description= body rewrites",
            len(kept) == 5 and _labels(unit_body) == invocation_keep,
            f"{len(kept)} generated-file-header keeps, unit body {_labels(unit_body)}",
        )
    )

    # 8. Adjacency (MIGR-03 edge): two occurrences on one synthetic line are BOTH emitted, in
    #    ascending column order, so the apply path can rewrite both right to left.
    adjacent = synthetic(
        "packages/cli/src/commands/pull.ts",
        '\tconsole.log("Run `clawdi skill install` or `clawdi skill add` first.");',
    )
    columns = [occurrence.column for occurrence in adjacent]
    cases.append(
        (
            "8 two occurrences on one line are both emitted in ascending column order",
            len(adjacent) == 2
            and _labels(adjacent) == invocation_keep * 2
            and columns == sorted(columns)
            and columns[0] != columns[1],
            f"{_labels(adjacent)} at columns {columns}",
        )
    )

    # 9. Ordering (MIGR-03 edge): two classify runs over an unchanged tree are byte-identical, so
    #    a diff of two reports shows real changes and nothing else.
    first, second = run_classify(), run_classify()
    cases.append(
        (
            "9 two classify runs over an unchanged tree are byte-identical",
            first == second,
            f"{len(first)} bytes, identical: {first == second}",
        )
    )

    return cases


def run_self_test() -> int:
    """Print one line per case and return the exit status. Reads the tree; writes nothing."""
    cases = _self_test_cases()
    for name, passed, observed in cases:
        print(f"{'ok      ' if passed else 'MISMATCH'} case {name} -> {observed}")
    failed = [name for name, passed, _ in cases if not passed]
    if len(cases) != _SELF_TEST_CASES:
        print(
            f"MISMATCH case count: {len(cases)} cases reported, expected {_SELF_TEST_CASES}",
            file=sys.stderr,
        )
        return 1
    if failed:
        print(
            f"rewrite brand strings self-test failed: {len(failed)} of {len(cases)} cases",
            file=sys.stderr,
        )
        return 1
    print(f"rewrite brand strings self-test passed: {len(cases)} cases")
    return 0


_USAGE = (
    "usage: rewrite_brand_strings.py --classify | --check | --apply <classify-report>\n"
    "  --classify  write the occurrence report to stdout and change nothing\n"
    "  --check     fail when this script and the tree disagree\n"
    "  --apply     rewrite the tree, refusing without a same-tree-hash classify report\n"
    "  --self-test run the rule set's own red and green cases, writing nothing"
)


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        if mode == "--classify":
            sys.stdout.write(run_classify())
            return
        if mode == "--check":
            print(run_check())
            return
        if mode == "--self-test":
            raise SystemExit(run_self_test())
        if mode == "--apply":
            if len(sys.argv) < 3:
                raise SystemExit(
                    "rewrite brand strings failed: --apply needs the path of a classify report "
                    "produced at this same tree"
                )
            report_text = Path(sys.argv[2]).read_text(encoding="utf-8")
            files, _contents, occurrences, _shape = classify_tree()
            written = apply_report(report_text, files, occurrences)
            print(f"rewrite brand strings applied: {written} files rewritten")
            return
    except RewriteError as error:
        raise SystemExit(f"rewrite brand strings failed: {error}") from error
    raise SystemExit(_USAGE)


if __name__ == "__main__":
    main()
