#!/usr/bin/env python3
"""Fail closed when this fork and upstream ``clawdi`` would collide on one machine.

P33-02. This fork is not a replacement for upstream ``clawdi`` -- both may be installed on the
same laptop at the same time, by the same person. Every id this product writes into the *shared
machine-local namespace* is therefore a potential collision: two products claiming one path, one
systemd unit, one launchd label, one port, or one key inside a third-party agent's config file.
Whichever converged last wins, and the loser's install is silently broken by software that never
mentioned it.

SCOPE FENCE -- machine-local namespace only:

  * filesystem paths (state roots, cache and data directories, bundled-skill directories, on-disk
    ownership receipts);
  * systemd unit names, drop-in filenames and generated file headers, and launchd label names;
  * bound ports;
  * keys this product writes into third-party config trees on a user's machine (an agent's
    ``mcp_servers`` block, an MCP server's registered name);
  * container-orchestration identities that materialise on a user's machine (compose project
    name, database and role defaults).

DELIBERATELY OUT OF SCOPE -- the wire-protocol carve-outs enforced by
``scripts/check_rebrand_carveouts.py`` (C-1 and C-4..C-7: the pinned pgbackrest image digest, the
``clawdi.<name>.vN`` schema identifiers, the ``clawdi_`` API-key prefix, the ``X-Clawdi-*``
headers, the persisted managed-provider id, ``MCP_ENDPOINT_PATH``). A matched client and its own
backend never collide on a shared machine: they only ever speak to each other, so renaming their
shared vocabulary buys no coexistence and costs every row already written. Widening this ledger
onto those literals would reopen 03.1's D-01 under a coexistence label, which is a scope breach
wearing a safety badge -- ``backend/tests/test_rebrand_coexistence.py`` asserts the fence
structurally so the breach cannot land quietly.

THE LEDGER IS ASSERTED IN BOTH DIRECTIONS. A ``MOVED`` entry asserts its literal is ABSENT from
the path that used to carry it; a ``PENDING`` entry asserts it is still PRESENT. The second half
is the half that matters: a ``PENDING`` id whose literal has already gone fails this guard, so
the ledger cannot decay into a memory of what the tree once was instead of a measurement of what
it is now. Move an id and flip its entry in the same change.

Structure copied from ``scripts/check_rebrand_carveouts.py``, which copied it verbatim from
``scripts/check_postgres_image_parity.py``: no arguments, no argparse, standard library only (so
it runs under bare ``python3``, not only under ``uv``), paths resolved from ``ROOT``, a domain
``RuntimeError`` subclass internally, ``SystemExit`` with a readable message from ``main()``, and
exactly one line of stdout on success.

Red and green behaviour is proven by ``backend/tests/test_rebrand_coexistence.py``.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# The two legal ledger statuses. They are plain strings rather than an enum because this file
# must stay importable under bare ``python3`` with no package around it, and because every
# downstream plan in phase 03.3 edits these entries by hand.
MOVED = "MOVED"
PENDING = "PENDING"
_STATUSES: tuple[str, ...] = (MOVED, PENDING)


class CoexistenceError(RuntimeError):
    """A machine-local id is not where the coexistence ledger says it is."""


# Every entry is ``(path, literal, status, owner)``. ``owner`` names the plan that moves the id
# -- for a ``PENDING`` entry that plan is still to come, for a ``MOVED`` entry it is the plan that
# already did it. Quote each literal verbatim from the working tree; do not paraphrase and do not
# reconstruct one from memory. A ledger entry that no longer matches its own source is a guard
# that fails for the wrong reason.
COEXISTENCE_IDS: tuple[tuple[str, str, str, str], ...] = (
    # ---- MCP server registration name -- MOVED by 03.3-01 -------------------------------------
    # X-5 (03.1 D-07, released by 03.3-01). This string is written into third-party agent config
    # files on a user's machine, where upstream `clawdi` registers a server under the same name;
    # two entries with one key is a straight collision. Its sibling `MCP_ENDPOINT_PATH` is NOT
    # here: that is the backend route this CLI calls on its own backend, a matched client/server
    # wire path and `CLASS: PERMANENT`.
    ("packages/cli/src/mcp/server.ts", 'name: "clawdi",', MOVED, "03.3-01"),
    # ---- state root -- MOVED by 03.3-02 --------------------------------------------------------
    # Two products sharing one credential, token and skills tree. Plan 03.3-01 recorded FIVE
    # resolvers here; 03.3-02 collapsed FOUR of them onto one authority
    # (`packages/cli/src/lib/state-root.ts`) and the four entries below are the flip.
    #
    # The fifth -- runtime/paths.ts's HOSTED branch -- was RECLASSIFIED rather than flipped, and
    # now lives in COEXISTENCE_CARVE_OUTS. 03.3-01 read the file-level `.clawdi` literal in that
    # file as a fifth resolver of the same kind; measured against the code it is the hosted branch
    # alone (the local branch carries no literal, it delegates). Hosted state resolves on a
    # provisioned server, under a home that is ITSELF a recorded carve-out -- so it sits outside
    # the P33-01 rationale, exactly like the platform roots two blocks down, and pinning it as
    # PENDING promised a move that P33-21 forbids. Same correction shape 03.3-01 applied to the
    # file-browser service user and group, and for the same reason: a ledger entry must not
    # contradict a recorded decision.
    (
        "packages/cli/src/lib/config.ts",
        'join(process.env.HOME || homedir(), ".clawdi")',
        MOVED,
        "03.3-02",
    ),
    ("packages/cli/src/serve/paths.ts", 'return join(home, ".clawdi");', MOVED, "03.3-02"),
    ("packages/cli/src/serve/installer.ts", 'return join(home(), ".clawdi");', MOVED, "03.3-02"),
    ("packages/cli/src/share/tokens.ts", 'return join(userHome(), ".clawdi");', MOVED, "03.3-02"),
    # The authority itself, pinned as never naming the legacy root. This is P33-04 and the plan's
    # own prohibition (T-03.3-05) expressed as an assertion rather than as prose: the resolver is
    # unconditional, so a compatibility read reintroduced here would silently point this product
    # and upstream clawdi at ONE credential tree -- the precise failure the whole ledger exists to
    # prevent, at the one file where a single line could cause it.
    ("packages/cli/src/lib/state-root.ts", '".clawdi"', MOVED, "03.3-02"),
    # ---- systemd unit names and the hosted drop-in filename -- MOVED by 03.3-05 ----------------
    # `~/.config/systemd/user` and the system unit root are shared namespaces: two daemons cannot
    # both own `clawdi-serve.service`. The names are DERIVED, so the id lives in the template
    # segment at each construction site rather than in a table of finished names.
    #
    # Every name below now derives from `RUNTIME_SYSTEMD_OWNED_NAME_PREFIX`, which is the SAME
    # constant `isOwnedRuntimeSystemdEntry` tests against. That is not tidiness: 03.3-04 shipped an
    # ownership predicate whose cheap branch is "the name carries the current prefix", and it had
    # nothing to match until this plan moved the names. One constant makes the writer and the
    # ownership test unable to disagree.
    (
        "packages/cli/src/runtime/runtime-systemd-reconciliation.ts",
        'return "clawdi-files";',
        MOVED,
        "03.3-05",
    ),
    (
        "packages/cli/src/runtime/runtime-systemd-reconciliation.ts",
        "return `clawdi-${systemdUnitNameSegment(program.runtime)}`;",
        MOVED,
        "03.3-05",
    ),
    (
        "packages/cli/src/runtime/runtime-systemd-reconciliation.ts",
        'return "clawdi-hermes-dashboard";',
        MOVED,
        "03.3-05",
    ),
    (
        "packages/cli/src/runtime/runtime-systemd-reconciliation.ts",
        "return `clawdi-${systemdUnitNameSegment(runtime)}-${systemdUnitNameSegment(service)}`;",
        MOVED,
        "03.3-05",
    ),
    (
        "packages/cli/src/runtime/runtime-systemd-reconciliation.ts",
        'const RUNTIME_SYSTEMD_DROP_IN_FILE = "10-clawdi-hosted.conf";',
        MOVED,
        "03.3-05",
    ),
    # The user-scope daemon unit filename. CONTEXT's P33-01 table names `serve/installer.ts` among
    # the unit sites; the plan's task enumeration named only the reconciler. Measured and kept --
    # a guard whose pathspec reaches one file too few is this repository's recorded failure mode.
    (
        "packages/cli/src/serve/installer.ts",
        'return agent ? `clawdi-serve-${agent}.service` : "clawdi-serve.service";',
        MOVED,
        "03.3-05",
    ),
    # ---- launchd label, both sites plus the per-agent form -- MOVED by 03.3-05 -----------------
    # launchd labels are unique per user: this one is a hard conflict, not a last-writer-wins.
    #
    # The label is now the reverse-DNS form of the domain this fork actually owns (P33-20:
    # `iforge.dev` -> `dev.iforge.serve`), not `ai.iforge.serve` by analogy with upstream's -- a
    # reverse-DNS label naming a domain the project does not control is wrong by construction.
    # The three sites derive from one exported constant; the sync-engine's local copy is gone.
    ("packages/cli/src/serve/installer.ts", 'return "ai.clawdi.serve";', MOVED, "03.3-05"),
    (
        "packages/cli/src/serve/installer.ts",
        "return `ai.clawdi.serve.${agent}`;",
        MOVED,
        "03.3-05",
    ),
    (
        "packages/cli/src/serve/sync-engine.ts",
        'const LAUNCHD_DAEMON_LABEL = "ai.clawdi.serve";',
        MOVED,
        "03.3-05",
    ),
    # ---- generated systemd file header -- MOVED by 03.3-04 ------------------------------------
    # The reconciler decides which unit files it OWNS by this header. Under coexistence a header
    # both products write is a licence to delete each other's units, which is why it moved behind
    # a dual-read reader rather than by a straight rename.
    #
    # The literal pinned here is the WRITER's declaration, not the bare header string, and that is
    # deliberate. The legacy string itself must REMAIN in this file -- reader-only, module-private,
    # never exported -- for as long as any installed host still carries files stamped with it; the
    # matching carve-out below records why. What had to move is the constant the writer emits, and
    # this entry asserts exactly that: repoint the exported constant back at the legacy spelling
    # and the literal returns, which is the only regression this row exists to catch.
    (
        "packages/cli/src/runtime/systemd-user.ts",
        "export const GENERATED_RUNTIME_SYSTEMD_FILE_HEADER =\n\t"
        '"# Generated by clawdi runtime init.',
        MOVED,
        "03.3-04",
    ),
    # ---- the two adjacent generated-file marker keys -- MOVED by 03.3-04 -----------------------
    # Written into unit files and hosted drop-ins in the same shared systemd namespace as the
    # header above, and in the same class under this guard's scope fence. Backfilled here by
    # 03.3-04, which measured them while widening the header: they carry no dual-read reader
    # because no production code path reads them back (`packages/cli/tests/runtime.test.ts` is the
    # only reader in the tree, and it moved in the same change). A row each, so that a writer
    # regressing to the legacy spelling is caught rather than remembered.
    (
        "packages/cli/src/runtime/runtime-systemd-reconciliation.ts",
        "# ClawdiEnvironmentRevision=",
        MOVED,
        "03.3-04",
    ),
    (
        "packages/cli/src/runtime/runtime-systemd-reconciliation.ts",
        '"# ClawdiHostedRuntimeDropIn=v1",',
        MOVED,
        "03.3-04",
    ),
    # The reservation PREFIX test that used to sit here as PENDING owned by 03.3-04 was
    # RECLASSIFIED into COEXISTENCE_CARVE_OUTS by that plan, beside the four receipt-family
    # entries 03.3-10 reclassified for the same underlying reason. 03.3-04 widened the READER to
    # accept both spellings, which is the precondition `_PERSISTED_RECEIPT_REASON` names; it did
    # not and could not move the receipt names themselves, because their writers are declared by
    # no plan in this phase. See `_RESERVATION_PREFIX_READER_REASON`.
    # The four receipt-family filenames that used to sit here as PENDING owned by 03.3-10 were
    # RECLASSIFIED into COEXISTENCE_CARVE_OUTS by that plan, which measured that it could not keep
    # the promise they made. See `_PERSISTED_RECEIPT_REASON` for the condition under which a
    # future plan may move them; the short version is that each is written onto user machines and
    # READ BACK, and no reader in this tree accepts both spellings yet.
    # ---- bundled-skill directory id -- MOVED by 03.3-10 ---------------------------------------
    # Both products write `SKILL.md` into one path under the user's agent trees; the last sync
    # wins. 03.3-01 recorded SIX entries here -- five adapters plus the registry helper -- on the
    # reading that the twelve measured call sites were the write path. Measured against the code
    # they are not: all eleven adapter sites are `migrateLegacyLocalSetupSkill` calls, and that
    # function refuses any id but `clawdi` AND requires `basename(targetDir) == id`. They name the
    # PRE-RENAME directory on purpose. All six were RECLASSIFIED into COEXISTENCE_CARVE_OUTS by
    # 03.3-10, the same correction shape 03.3-01 applied to the file-browser service user and
    # 03.3-02 applied to the hosted state root: a ledger entry must not promise a move the code
    # forbids.
    #
    # The write path was ONE site, `builtinSkillTargetDir`, and it moved. What follows is the
    # substantive backfill for those six, so the checked count records real assertions rather than
    # dropping by six.
    (
        "packages/cli/src/lib/native-activation.ts",
        '"skills/clawdi/SKILL.md"',
        MOVED,
        "03.3-10",
    ),
    ("install.sh", 'p=="skills/clawdi/SKILL.md"', MOVED, "03.3-10"),
    (
        "packages/cli/src/lib/native-distribution.ts",
        '"skills", "clawdi", "SKILL.md"',
        MOVED,
        "03.3-10",
    ),
    (
        "packages/cli/src/runtime/hosted-bundled-skill.ts",
        'assetDirectory: "hosted-versions/1/clawdi"',
        MOVED,
        "03.3-10",
    ),
    ("packages/cli/src/commands/setup.ts", '"skills", "clawdi"', MOVED, "03.3-10"),
    # ---- third-party agent config keys, writer AND remover -- 03.3-10 --------------------------
    # `teardown.ts` is a LIVE DEFECT under coexistence, not merely a rename: it removes every
    # `clawdi` and `clawdi-mcp` block from Hermes's config.yaml, including upstream's. Both the
    # writer (setup.ts) and the remover (teardown.ts) are pinned -- moving only one of them leaves
    # a product that registers under a new key and still deletes the old owner's entry.
    (
        "packages/cli/src/commands/teardown.ts",
        'removeAllYamlBlocks(removeAllYamlBlocks(content, "clawdi-mcp"), "clawdi")',
        MOVED,
        "03.3-10",
    ),
    # The three CLI-driven removers beside it. 03.3-01 pinned only the Hermes YAML remover, which
    # is the one CONTEXT's P33-07 row named; measured against `teardown.ts` there are four, and
    # `claude mcp remove clawdi` deletes an upstream registration just as surely as the YAML
    # edit does. Moved by 03.3-10 in the same commit.
    ("packages/cli/src/commands/teardown.ts", "claude mcp remove clawdi", MOVED, "03.3-10"),
    ("packages/cli/src/commands/teardown.ts", "codex mcp remove clawdi", MOVED, "03.3-10"),
    ("packages/cli/src/commands/teardown.ts", "openclaw mcp unset clawdi", MOVED, "03.3-10"),
    ("packages/cli/src/commands/setup.ts", r"`  clawdi:\n    command:", MOVED, "03.3-10"),
    ("packages/cli/src/commands/setup.ts", "claude mcp add-json clawdi ", MOVED, "03.3-10"),
    # The other three MCP registration writers. 03.3-01 pinned only the Claude Code and Hermes
    # keys, which CONTEXT's P33-07 row named; measured against `setup.ts` there are four writers,
    # and a product that registers as `iforge` in two agents and `clawdi` in the other two still
    # collides with upstream on the other two. Moved by 03.3-10 together.
    ("packages/cli/src/commands/setup.ts", "codex mcp add clawdi ", MOVED, "03.3-10"),
    ("packages/cli/src/commands/setup.ts", "openclaw mcp set clawdi ", MOVED, "03.3-10"),
    # ---- compose project and Postgres identity defaults -- MOVED by 03.3-08 -------------------
    # Two self-host stacks on one machine fight over the compose project name and its volumes.
    # These are DEFAULTS behind `${VAR:-...}`, so the flip affects fresh deploys only; a live
    # database is moved by `docs/runbooks/iforge-compose-postgres-migration.md`, never by code
    # (P33-06). The MOVED direction is what keeps that promise checkable: it asserts the old
    # default is GONE from this file, so a merge or a revert that reinstates `clawdi` as the
    # value a fresh `docker compose up` initdbs with fails here rather than on a user's disk.
    (
        "deploy/self-host/docker-compose.yml",
        "name: ${COMPOSE_PROJECT_NAME:-clawdi-self-host}",
        MOVED,
        "03.3-08",
    ),
    (
        "deploy/self-host/docker-compose.yml",
        "postgresql+asyncpg://${POSTGRES_USER:-clawdi}:",
        MOVED,
        "03.3-08",
    ),
    (
        "deploy/self-host/docker-compose.yml",
        "POSTGRES_DB: ${POSTGRES_DB:-clawdi}",
        MOVED,
        "03.3-08",
    ),
    (
        "deploy/self-host/docker-compose.yml",
        "POSTGRES_USER: ${POSTGRES_USER:-clawdi}",
        MOVED,
        "03.3-08",
    ),
)

_PERSISTED_RECEIPT_REASON = (
    "An ownership receipt written into a third-party agent's tree on a user's machine and READ "
    "BACK to decide what this product owns. `rewrite_brand_strings.py --classify` labels every "
    "one of these `KEEP:persisted-marker`, whose recorded rule is that it 'moves only behind a "
    "legacy-accepting reader'. No such reader exists in this tree: plan 03.3-04 builds the "
    "dual-read pattern for the systemd header and the reservation PREFIX test, and declares "
    "neither of these files. Moving a writer ahead of its reader orphans every receipt on every "
    "installed copy, and for `.clawdi-managed*` it is worse than orphaning -- that prefix is also "
    "the token two archive validators REJECT to stop an uploaded skill forging an ownership "
    "marker (`isReservedSkillArchivePath` in lib/tar.ts since upstream #1174, and the member "
    "check in backend/app/services/tar_utils.py), and a writer that moved first would emit a name "
    "neither validator refuses. 03.3-10 held them rather than move them blind; a future plan may "
    "move them once BOTH validators and the receipt reader accept the new spelling as well as "
    "the old, and it must declare all of those files. "
    "MEASURED AGAIN, 03.3-04: half of that condition is now met and half is not, so the entry "
    "stays. `isReservedSkillArchivePath` accepts both prefixes as of 03.3-04 -- it is the "
    "legacy-accepting reader the rule asks for, and it is simultaneously the CLI-side archive "
    "validator, so it now refuses strictly more than before. What remains unmet is the rest: "
    "`backend/app/services/tar_utils.py` still refuses only the legacy token, and the receipt "
    "WRITERS (`packages/cli/src/runtime/hosted-hermes-skill.ts` and "
    "`packages/cli/src/runtime/hosted-openclaw-skill.ts`, which spell `.clawdi-manifest-receipts`) "
    "appear in no plan's `files_modified` anywhere in this phase. Until those move together, every "
    "receipt on every installed copy is still spelled the legacy way."
)

_RESERVATION_PREFIX_READER_REASON = (
    "The legacy half of a dual-read ownership-receipt reader, retained on purpose. 03.3-04 widened "
    "`isReservedSkillArchivePath` to accept both marker prefixes; the legacy one cannot leave this "
    "file while any installed copy still carries receipts spelled that way, because dropping it "
    "would both orphan those reservations and narrow an archive validator that exists to stop an "
    "uploaded Skill forging an ownership marker. It is a reader-only literal: nothing in this tree "
    "WRITES it, and the four sibling entries under `_PERSISTED_RECEIPT_REASON` record the writers "
    "that still spell the legacy name and the plan-ownership gap that keeps them there. Recorded "
    "here rather than left PENDING because no plan in this phase can move it, and a PENDING row "
    "promising a move nobody owns is the defect this record exists to prevent."
)

_LEGACY_ADOPTION_REASON = (
    "The PRE-RENAME bundled-skill directory, named so this product can recognise and retire the "
    "directory its own earlier releases wrote. `migrateLegacyLocalSetupSkill` refuses any id but "
    "`clawdi` and additionally requires `basename(targetDir) == id`, so moving either half of "
    "this pair makes the call throw rather than adopt -- and an unadopted directory is left "
    "behind forever and uploaded as if it were the user's own content. Adoption is also what "
    "keeps upstream's directory safe: it succeeds only on bytes a published release of this "
    "lineage actually shipped, so an upstream `clawdi` tree is never claimed (P33-07)."
)

_HOSTED_ROOT_REASON = (
    "A hosted platform root that exists only on a provisioned server, never on an end user's "
    "machine, so no upstream clawdi install is ever present beside it to collide with (P33-21)."
)
_HOSTED_USER_REASON = (
    "An OS account and home fixed by the provisioned runtime image rather than by this "
    "repository; moving it is an image and provisioning change, and it lives on a server where "
    "no upstream clawdi install exists to collide with (P33-21)."
)

# Machine-local-looking ids that deliberately do NOT move, each with the reason that puts it
# outside the P33-01 rationale. These are RECORDS, not assertions: the ledger above is the thing
# this guard checks, and a carve-out is a documented decision rather than a claim about the tree.
# Recording them here is what stops a later reader treating an absence from COEXISTENCE_IDS as an
# oversight and "fixing" it -- MIGR-02's own requirement text permits a carve-out that carries its
# reason, and P33-21 applies the same discipline to these.
COEXISTENCE_CARVE_OUTS: tuple[tuple[str, str, str], ...] = (
    # P33-07 -- the on-disk ownership receipts. Reclassified out of COEXISTENCE_IDS by 03.3-10,
    # which owned all four. The last two were the MEASURED GAP 03.3-01 recorded: they appear in no
    # plan's `files_modified` anywhere in this phase, 03.3-10 was named owner only because it held
    # the rest of the family, and it does not declare them either. That is now a decision with a
    # reason attached rather than a PENDING entry promising a move nobody owns.
    (
        "packages/cli/src/runtime/hosted-bundled-skill.ts",
        'const HOSTED_BUNDLED_SKILL_MARKER = ".clawdi-managed.json";',
        _PERSISTED_RECEIPT_REASON,
    ),
    (
        "packages/cli/src/runtime/manifest.ts",
        'const MANAGED_WHATSAPP_AUTH_MARKER = ".clawdi-managed-whatsapp-auth.json";',
        _PERSISTED_RECEIPT_REASON,
    ),
    (
        "packages/cli/src/runtime/hosted-hermes-skill.ts",
        '".clawdi-manifest-receipts"',
        _PERSISTED_RECEIPT_REASON,
    ),
    (
        "packages/cli/src/runtime/hosted-openclaw-skill.ts",
        '".clawdi-manifest-receipts"',
        _PERSISTED_RECEIPT_REASON,
    ),
    # P33-07 -- the reader half of the same receipt family. Reclassified out of COEXISTENCE_IDS by
    # 03.3-04, which owned it: the plan widened this reader to accept both prefixes, which is
    # progress the ledger has no direction to express. MOVED would assert the receipt names left
    # the shared namespace, and they have not; PENDING would promise a move 03.3-04 could not make
    # and no later plan in this phase owns.
    (
        "packages/cli/src/lib/tar.ts",
        'LEGACY_RESERVED_SKILL_ARCHIVE_MARKER_PREFIX = ".clawdi-managed";',
        _RESERVATION_PREFIX_READER_REASON,
    ),
    # RETIRED 2026-09-30 (user directive: no Clawdi-name aliases, iforge names only). The
    # dual-read legacy readers recorded here -- the generated-file header, the hosted drop-in
    # filename, the Files unit name and the two pre-rename daemon identities -- were deleted: there
    # is no production deployment of iforge, so no pre-rename host to recognise. Their absence is
    # asserted by `packages/cli/tests/hosted-identity-move.test.ts`.
    #
    # P33-07 -- upstream's legacy local-setup-skill adoption sites in the adapters (the registry
    # helper that also named the pre-rename directory was retired 2026-09-30). Reclassified out
    # of COEXISTENCE_IDS by 03.3-10, which owned all six entries and measured that the code
    # forbids the move they promised: every one of these is a `migrateLegacyLocalSetupSkill`
    # argument, not a write path. The WRITE path was a single site, `builtinSkillTargetDir`, and
    # it moved.
    ("packages/cli/src/adapters/claude-code.ts", 'id: "clawdi",', _LEGACY_ADOPTION_REASON),
    ("packages/cli/src/adapters/codex.ts", 'id: "clawdi",', _LEGACY_ADOPTION_REASON),
    ("packages/cli/src/adapters/hermes.ts", 'id: "clawdi",', _LEGACY_ADOPTION_REASON),
    ("packages/cli/src/adapters/ironhermes.ts", 'id: "clawdi",', _LEGACY_ADOPTION_REASON),
    ("packages/cli/src/adapters/openclaw.ts", 'id: "clawdi",', _LEGACY_ADOPTION_REASON),
    # P33-21 -- the hosted USER-STATE tree. Reclassified out of COEXISTENCE_IDS by 03.3-02, which
    # owned the entry and measured that it could not keep the promise the entry made. The local
    # resolvers it was grouped with collapsed onto `lib/state-root.ts`; this one is the hosted
    # branch of `defaultClawdiHome`, and its home resolves to `HOSTED_RUNTIME_HOME` -- a carve-out
    # three entries below. Moving the suffix under an already-carved-out parent buys no
    # coexistence (there is no upstream clawdi on that server) and costs a state relocation on
    # every provisioned image. `runtime/paths.test.ts` asserts the branch resolves independently
    # of the authority, so the separation is checked rather than merely written down.
    (
        "packages/cli/src/runtime/paths.ts",
        'process.env.CLAWDI_HOME || join(userHome, ".clawdi")',
        _HOSTED_USER_REASON,
    ),
    # P33-21 -- hosted platform roots. They exist on a provisioned server, never on an end user's
    # laptop, so no upstream `clawdi` install is ever present to collide with them.
    (
        "packages/cli/src/runtime/paths.ts",
        'DEFAULT_CONFIGURATION_ROOT = "/etc/clawdi"',
        _HOSTED_ROOT_REASON,
    ),
    (
        "packages/cli/src/runtime/paths.ts",
        'DEFAULT_SERVICE_STATE_ROOT = "/var/lib/clawdi"',
        _HOSTED_ROOT_REASON,
    ),
    (
        "packages/cli/src/runtime/paths.ts",
        'DEFAULT_CACHE_ROOT = "/var/cache/clawdi"',
        _HOSTED_ROOT_REASON,
    ),
    ("packages/cli/src/runtime/paths.ts", 'DEFAULT_RUN_ROOT = "/run/clawdi"', _HOSTED_ROOT_REASON),
    (
        "packages/cli/src/runtime/paths.ts",
        'SYSTEMD_PLATFORM_DIRECTORY = "clawdi"',
        _HOSTED_ROOT_REASON,
    ),
    # P33-21 -- the file-browser HOSTED paths and state directory. Same reason; note that the
    # file-browser UNIT NAME is a different object and IS in the ledger above, because a unit name
    # is written into the shared systemd namespace while these paths are not.
    (
        "packages/cli/src/runtime/paths.ts",
        'DEFAULT_FILE_BROWSER_STATE_ROOT = "/var/lib/clawdi-files"',
        _HOSTED_ROOT_REASON,
    ),
    (
        "packages/cli/src/runtime/paths.ts",
        'DEFAULT_FILE_BROWSER_RUNTIME_ROOT = "/run/clawdi-files"',
        _HOSTED_ROOT_REASON,
    ),
    (
        "packages/cli/src/runtime/paths.ts",
        'SYSTEMD_FILE_BROWSER_STATE_DIRECTORY = "clawdi-files"',
        _HOSTED_ROOT_REASON,
    ),
    # P33-21 -- the hosted runtime home and OS user, with the uid and gid the image fixes them to.
    # The VALUE lives in the provisioned image, not in this repository; RESEARCH A5 could not even
    # confirm the image is built from this repo. Moving them is an image and provisioning change.
    (
        "packages/cli/src/runtime/hosted-runtime-contract.ts",
        'HOSTED_RUNTIME_HOME = "/home/clawdi"',
        _HOSTED_USER_REASON,
    ),
    (
        "packages/cli/src/runtime/hosted-runtime-contract.ts",
        'HOSTED_RUNTIME_USER = "clawdi"',
        _HOSTED_USER_REASON,
    ),
    (
        "packages/cli/src/runtime/hosted-runtime-contract.ts",
        "HOSTED_RUNTIME_UID = 10_001",
        _HOSTED_USER_REASON,
    ),
    (
        "packages/cli/src/runtime/hosted-runtime-contract.ts",
        "HOSTED_RUNTIME_GID = 10_001",
        _HOSTED_USER_REASON,
    ),
    # A-P1 -- the iptables chain read back by the sidecar deploy script. A live kernel object on a
    # provisioned host: renaming the string orphans the chain that is already installed, and the
    # rules in it keep filtering under a name nothing looks for any more.
    (
        "config/deploy.yml",
        "chain=CLAWDI_WA_EGRESS;",
        "A live iptables chain on a provisioned host, not a user machine; a rename orphans the "
        "installed chain rather than moving it, and there is no upstream clawdi on that host to "
        "collide with. Handled by the runbook if it is ever moved.",
    ),
    # Test-local isolated-tree names, recorded by 03.3-02's re-measurement so a later reader does
    # not mistake them for state-root sites the collapse missed. Each is a directory created
    # inside a `mkdtemp` sandbox and handed to the CLI as an EXPLICIT whole-tree override; the
    # name is arbitrary and claims nothing in any shared namespace, which is what puts it outside
    # the P33-01 rationale. They are also outside the 03.3-09 classifier's scope, which is
    # non-test source only (P33-11), so no later plan in this phase owns them.
    (
        "packages/cli/src/commands/serve-handlers.test.ts",
        'process.env.CLAWDI_HOME = join(tmpHome, ".clawdi");',
        "A temp-directory name inside a per-case sandbox, passed as an explicit override rather "
        "than resolved as a state root; it names no shared machine-local namespace and there is "
        "no upstream clawdi that could collide with a mkdtemp path.",
    ),
    (
        "packages/cli/src/serve/project-skill-reconcile.test.ts",
        'process.env.CLAWDI_HOME = join(root, ".clawdi");',
        "Same shape as the serve-handlers fixture above: an arbitrary sandbox directory handed to "
        "the CLI as an explicit whole-tree override, not a resolved state root.",
    ),
    # Port 18789 -- CONTEXT's P33-01 table lists it as a collision. That framing does not survive
    # measurement, and the measurement is recorded here rather than the assumption.
    (
        "packages/cli/src/runtime/manifest-contract.ts",
        "OpenClaw v2 gateway must bind directly to the pod network on port 18789",
        "OpenClaw's OWN native gateway port, asserted as a contract here and written into "
        "OpenClaw's config by this CLI -- it is not an iforge identity this fork can give a new "
        "default to. It also binds on the hosted pod network rather than on an end-user laptop. "
        "Two managed OpenClaw gateways on one host is an OpenClaw problem.",
    ),
)

# The guard's own source and its test enumerate these literals rather than using them. A ledger
# entry naming either would measure this file instead of the tree -- the same vacuity
# ``_SCHEMA_SCAN_EXCLUDES`` prevents in ``check_rebrand_carveouts.py``, in the form a
# path-targeted ledger takes.
_LEDGER_SELF_EXCLUDES: tuple[str, ...] = (
    "scripts/check_rebrand_coexistence.py",
    "backend/tests/test_rebrand_coexistence.py",
)


def _validate_ledger_shape() -> None:
    """Reject a ledger that cannot mean anything, before reading a single file.

    An empty ledger passes every assertion it makes, which is the failure mode this whole guard
    exists to prevent one level down. A self-referential entry is the same defect wearing a path.
    """
    if not COEXISTENCE_IDS:
        raise CoexistenceError(
            "the coexistence ledger is empty; a guard with nothing to check is a green light "
            "with no bulb behind it"
        )

    for path, literal, status, owner in COEXISTENCE_IDS:
        if path in _LEDGER_SELF_EXCLUDES:
            raise CoexistenceError(
                f"{path} is the coexistence guard's own source; an entry naming it measures this "
                "file rather than the tree"
            )
        if status not in _STATUSES:
            raise CoexistenceError(
                f"{path} carries unknown ledger status {status!r} for {literal!r}; "
                f"expected one of {_STATUSES}"
            )
        if not owner.strip():
            raise CoexistenceError(f"{path} declares {literal!r} with no owning plan")


def validate_coexistence(contents: dict[str, str] | None = None) -> int:
    """Assert every ledger entry in the direction its own status claims.

    ``contents`` maps repository-relative paths to file text. When it is ``None`` the mapping is
    read from ``ROOT``; tests inject a mapping instead so a red case never has to write a
    half-migrated id to disk.

    Every mismatch is collected and reported together, sorted by path then literal, so a diff of
    two failing runs against the same tree shows only real changes rather than iteration order.

    Returns the number of ids checked. Raises ``CoexistenceError`` naming each offending path, so
    the failure is actionable without reading this file.
    """
    _validate_ledger_shape()

    if contents is None:
        contents = {
            path: (ROOT / path).read_text(encoding="utf-8") for path, _, _, _ in COEXISTENCE_IDS
        }

    failures: list[tuple[str, str, str]] = []
    for path, literal, status, owner in COEXISTENCE_IDS:
        text = contents.get(path)
        if text is None:
            failures.append(
                (
                    path,
                    literal,
                    f"{path} is absent from the contents given to the coexistence guard",
                )
            )
            continue

        present = literal in text
        if status == MOVED and present:
            failures.append(
                (
                    path,
                    literal,
                    f"{path} still spells the machine-local id {literal!r}, which plan {owner} "
                    "recorded as moved; upstream clawdi claims that name on the same machine",
                )
            )
        elif status == PENDING and not present:
            failures.append(
                (
                    path,
                    literal,
                    f"{path} no longer spells {literal!r}, but the ledger still lists it as "
                    f"PENDING for plan {owner}; flip the entry to MOVED in the change that "
                    "moved it",
                )
            )

    if failures:
        raise CoexistenceError("; ".join(message for _, _, message in sorted(failures)))

    return len(COEXISTENCE_IDS)


def assert_no_pending_entries() -> int:
    """Refuse a ledger that still promises a move. Returns the pending count, always ``0``.

    Through phase 03.3 a ``PENDING`` row was a legal, expected state: it named a machine-local id
    whose move an in-flight plan owned, and ``validate_coexistence`` asserted the id was still
    spelled the old way until that plan landed. Phase 03.3 closes with plan 03.3-14, so from here
    the same row means the opposite thing -- a move that was promised and did not happen -- and
    the guard says so instead of printing a count on a success line nobody diffs. That transition
    is what makes the phase's completion claim falsifiable rather than asserted in a summary.

    This is a SEPARATE assertion rather than a branch inside ``validate_coexistence``, and the
    separation is load-bearing. That function proves the ledger in BOTH directions, and the
    PENDING direction -- an id whose literal has already gone while its row still says PENDING --
    is the half that stops the ledger rotting into a memory of the tree rather than a measurement
    of it. Folding this refusal in there would reject every synthetic one-entry ledger before a
    direction was ever asserted on it, making both PENDING red/green cases in
    ``backend/tests/test_rebrand_coexistence.py`` unreachable: the guard would be weakened in the
    very act of being tightened. ``test_the_pending_refusal_leaves_both_directional_assertions_
    intact`` pins the separation so a later edit cannot undo it quietly.

    A future phase that genuinely needs a new deferral does not delete this. It adds the row
    deliberately, with an owning plan, and moves this ceiling in the same change -- the same
    discipline ``config/rebrand-allowlist.txt`` already enforces on a fence. What must not happen
    is the ceiling drifting upward to match whatever the ledger happens to hold, which is the
    budget-shaped ratchet plan 03.3-11 retired by replacement rather than by deletion.

    The message names every offending path, literal and owning plan, sorted, so a reader learns
    which move was owed and by whom without opening this file.
    """
    pending = sorted(
        (path, literal, owner)
        for path, literal, status, owner in COEXISTENCE_IDS
        if status == PENDING
    )
    if pending:
        rows = "; ".join(
            f"{path} still lists {literal!r} as {PENDING} for plan {owner}"
            for path, literal, owner in pending
        )
        raise CoexistenceError(
            f"the coexistence ledger carries {len(pending)} pending "
            f"{'entry' if len(pending) == 1 else 'entries'} after phase 03.3 closed, and a "
            f"pending row is now a defect rather than an in-flight state: {rows}. Either land "
            "the move and flip the row to MOVED, or reclassify it as a carve-out with its "
            "reason attached"
        )
    return 0


def main() -> None:
    try:
        checked = validate_coexistence()
        pending = assert_no_pending_entries()
    except CoexistenceError as error:
        raise SystemExit(f"rebrand coexistence failed: {error}") from error
    print(f"rebrand coexistence passed: {checked} machine-local ids checked, {pending} pending")


if __name__ == "__main__":
    main()
