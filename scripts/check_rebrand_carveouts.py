#!/usr/bin/env python3
"""Fail closed when a rebrand sweep touched a do-not-rename site.

Phase 03.1 renames the product's *public* identity from ``clawdi`` to ``iforge`` while leaving
internal source and test symbols upstream-aligned. The boundary between the two is not obvious
from a diff: 227 markdown lines carry both a rename target and a carve-out token, and several
carve-outs are self-consistent pairs -- renaming both halves keeps every existing check green
while the running system breaks. ``scripts/check_postgres_image_parity.py`` is the sharpest
example: rename its ``PIN_PATTERN`` together with the five image pins and parity still passes,
but every image pull 404s.

This script is the external witness those pairs lack. It asserts that specific literals are
still present at the specific paths that own them, that the two Alembic revision files are
still at their original paths, and that all 55 wire-protocol schema identifiers still resolve
somewhere in the tree. It renames nothing and reads nothing outside the repository.

Structure copied verbatim from ``scripts/check_postgres_image_parity.py``: no arguments, no
argparse, standard library only (so it runs under bare ``python3``, not only under ``uv``),
paths resolved from ``ROOT``, a domain ``RuntimeError`` subclass internally, ``SystemExit`` with
a readable message from ``main()``, and exactly one line of stdout on success.

Red and green behaviour is proven by ``backend/tests/test_rebrand_guards.py``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# The upstream image address. ``clawdi-ai`` here is a real GitHub organisation this fork does
# not control, and the digest is the only integrity control on the database image.
_PIN_IMAGE = "ghcr.io/clawdi-ai/postgres-pgbackrest"
_PIN_DIGEST = (
    f"{_PIN_IMAGE}:pg18@sha256:db8d1af12ebac7fe10c57730502a31e9b34586ac1ed32852639007dd77eda449"
)
# The enforcing regex, quoted as it appears in the parity checker's source. Split only to stay
# inside the project's 100-column limit; the concatenation is byte-identical to the file line.
_PIN_PATTERN_LINE = (
    r'PIN_PATTERN = re.compile(r"ghcr\.io/clawdi-ai/postgres-pgbackrest'
    r':pg18@sha256:[0-9a-f]{64}")'
)


class CarveOutError(RuntimeError):
    """A do-not-rename site named in phase 03.1 no longer carries its literal."""


# Every literal below was quoted verbatim from the working tree. Do not paraphrase and do not
# reconstruct one from memory -- open the file and copy the line. A carve-out that no longer
# matches its own source is a guard that fails for the wrong reason.
CARVE_OUTS: tuple[tuple[str, str], ...] = (
    # C-1 -- the pinned PostgreSQL image, at all five sites.
    ("config/postgres-image.txt", _PIN_DIGEST),
    ("docker-compose.test.yml", f"image: {_PIN_DIGEST}"),
    ("config/deploy.yml", f"image: &postgres-image {_PIN_DIGEST}"),
    (".github/workflows/backend-ci.yml", f"image: {_PIN_DIGEST}"),
    # C-2 -- the regex that enforces C-1. Renaming it *with* the pins keeps parity green.
    ("scripts/check_postgres_image_parity.py", _PIN_PATTERN_LINE),
    # C-6 -- the API key prefix: minted with it, and validated against it on every request.
    ("backend/app/core/auth.py", 'API_KEY_PREFIX = "clawdi_"'),
    ("backend/app/services/api_key.py", 'raw = "clawdi_" + secrets.token_urlsafe(32)'),
    # C-5 -- the X-Clawdi-* wire headers, and the Sentry scrub entry that must move with them.
    (
        "backend/app/core/skill_sync_protocol.py",
        'SKILL_SYNC_PROTOCOL_HEADER = "X-Clawdi-Skill-Sync-Protocol"',
    ),
    (
        "packages/cli/src/lib/skill-sync-protocol.ts",
        'export const SKILL_SYNC_PROTOCOL_HEADER = "X-Clawdi-Skill-Sync-Protocol";',
    ),
    ("backend/app/core/sentry.py", '"x_clawdi_token",'),
    # C-7 -- the persisted managed-provider id, present in ai_provider rows.
    ("backend/app/services/managed_ai_provider.py", 'CLAWDI_MANAGED_PROVIDER_ID = "clawdi"'),
    # C-7 -- upstream #973's reserved placeholder env NAME for a managed AI provider, emitted by the
    # backend as the manifest's `runtimeEnvName` and written by the runtime into an agent's own
    # config. Not user-set, so it is not an `IFORGE_*` alias member; 06.1-04 retired the alias pair
    # that pretended otherwise (user directive 2026-09-30) and pins both halves here instead.
    (
        "backend/app/services/managed_ai_provider.py",
        'MANAGED_AI_PROVIDER_RUNTIME_ENV = "CLAWDI_AI_API_KEY"',
    ),
    (
        "packages/shared/src/ai-provider.ts",
        'export const MANAGED_AI_PROVIDER_RUNTIME_ENV = "CLAWDI_AI_API_KEY";',
    ),
    # C-8 -- the JSONB payload marker that drives channel-message dedup against data at rest.
    (
        "backend/app/routes/channel_routers/telegram.py",
        'ChannelMessage.payload.contains({"clawdi_system": marker})',
    ),
    (
        "backend/app/routes/channel_routers/telegram.py",
        'tutorial_payload["clawdi_system"] = marker',
    ),
    # C-9 -- the sub/aud pair carried by gateway tokens that have already been issued.
    (
        "backend/app/routes/channel_routers/discord.py",
        '_DISCORD_GATEWAY_CAPABILITY_SUBJECT = "clawdi_discord_gateway"',
    ),
    (
        "backend/app/routes/channel_routers/discord.py",
        '_DISCORD_GATEWAY_CAPABILITY_AUDIENCE = "clawdi_discord_gateway"',
    ),
    # C-4 -- one representative Literal[...] discriminator. The full 55 are checked separately
    # by validate_schema_identifiers; this entry pins the shape at a named path as well.
    ("backend/app/schemas/runtime_observation.py", 'Literal["clawdi.hostedRuntimeObserved.v2"]'),
    # Upstream sync step 3 (06.1-08): two wire literals that arrived with ffffaa02 (#1334). The
    # hosted Baileys credential metadata key is read back from `creds.additionalData` (sibling of
    # the socket key above), and the account-suspended problem type is the `type` the backend
    # emits and the dashboard matches byte for byte (apps/web/src/lib/account-suspension.ts).
    (
        "packages/cli/src/runtime/whatsapp-upstream-contract.ts",
        'CLAWDI_MANAGED_WHATSAPP_CREDENTIAL_METADATA_KEY = "clawdi.managedWhatsAppCredential"',
    ),
    (
        "backend/app/schemas/problem.py",
        'ACCOUNT_SUSPENDED_PROBLEM_TYPE = "urn:clawdi:problem:account-suspended"',
    ),
    # The hosted bundled-skill marker pins (`HOSTED_BUNDLED_SKILL_MARKER_SCHEMA` in
    # packages/cli/src/runtime/hosted-bundled-skill.ts) were retired at upstream sync step 3
    # (06.1-08): upstream's #1117 follow-ups deleted the marker writer and its one-time adoption,
    # so no production code reads or writes the marker any more. Its schema id still resolves
    # (an upstream test fixture) and stays in SCHEMA_IDENTIFIERS below.
    # X-5 (D-07) -- the bundled skill ids are written into third-party agent config files on user
    # machines, and install.sh validates the skill member paths inside the release tarball.
    # RESEARCH's Wave-2 row listed the MCP server NAME as a rename target; D-07 deferred it
    # rather than refusing it. See planner deviation 2 in 03.1-01-PLAN.md.
    #
    # THE MCP SERVER NAME MOVED IN PHASE 03.3 (plan 03.3-01) and its entry was removed from here
    # in the same commit that moved it. It is a machine-local id -- upstream `clawdi` registers a
    # server under that same key inside a third-party agent's config, so leaving both products on
    # one name is the collision D-07 deferred rather than the compatibility it protects. Its
    # absence is now asserted, in the opposite direction, by
    # `scripts/check_rebrand_coexistence.py`; it is not unguarded, it is guarded elsewhere.
    #
    # `MCP_ENDPOINT_PATH` did NOT move and stays pinned below. It is the backend route served by
    # `backend/app/routes/mcp_bridge.py` and called by this CLI: a matched client/server wire
    # path, `CLASS: PERMANENT`. Two products never collide over it because each only ever calls
    # its own backend, so renaming it buys no coexistence and breaks every released client.
    ("packages/cli/src/mcp/server.ts", 'const MCP_ENDPOINT_PATH = "/v1/mcp/clawdi";'),
    #
    # THE HOSTED BUNDLED-SKILL ASSET DIRECTORY MOVED IN PHASE 03.3 (plan 03.3-10) and its entry
    # was removed from here in the same commit that moved it, alongside the `git mv` of
    # `packages/cli/skills/hosted-versions/1/clawdi` and both halves of the release-tarball
    # member contract (`packages/cli/src/lib/native-activation.ts` and `install.sh`). It is a
    # machine-local id in the same class as the MCP server name above: both products write a
    # `SKILL.md` into one path under a third-party agent's skill tree, so the last sync wins and
    # the loser's document is gone. Its absence is now asserted, in the opposite direction, by
    # `scripts/check_rebrand_coexistence.py`; it is not unguarded, it is guarded elsewhere.
    # X-2 / X-4 (D-07) -- the pre-rename Files service identity. RETIRED 2026-09-30 by user
    # directive (iforge names only): upstream #939 took Files off a service account, and there is
    # no production host carrying `clawdi-files` to recognise, so the `LEGACY_*` readers these
    # entries pinned were removed. Their absence is asserted, in the opposite direction, by
    # `packages/cli/tests/hosted-identity-move.test.ts` (no `LEGACY_*` export, no retired spelling
    # in non-test CLI source).
)

# C-10 -- Alembic resolves migrations by revision id, never by filename, so renaming these buys
# nothing and guarantees a permanent conflict every time this fork tracks upstream.
ALEMBIC_REVISION_FILES: tuple[str, ...] = (
    "backend/alembic/versions/2d4c8e1b7a90_clawdi_native_channels.py",
    "backend/alembic/versions/c1d99ac4f9e6_clawdi_serve_v1_schema.py",
)

# C-4 -- the 55 distinct wire-protocol schema identifiers, enumerated verbatim. These are
# pydantic ``Literal[...]`` discriminators persisted in Postgres JSONB and in on-disk runtime
# state, so a rename makes already-written rows fail their own validation on read.
SCHEMA_IDENTIFIERS: tuple[str, ...] = (
    "clawdi.authStatus.v1",
    "clawdi.capabilities.v1",
    "clawdi.cliNpmBootstrapStatus.v1",
    "clawdi.cliUpgradeState.v2",
    "clawdi.configPaths.v1",
    "clawdi.daemonAuthTokenRevision.v1",
    "clawdi.deploy.v1",
    "clawdi.egressProfiles.v1",
    "clawdi.hermesManifestSkillReceipt.v2",
    "clawdi.hostedBundledSkillMarker.v1",
    "clawdi.hostedCatalogSkillArchive.v1",
    "clawdi.hostedManagedConfig.v1",
    "clawdi.hostedManagedMcpServers.v1",
    "clawdi.hostedManagedMcpServers.v2",
    "clawdi.hostedManagedMcpServers.v3",
    "clawdi.hostedRuntimeObserved.v2",
    "clawdi.hostedRuntimeProviderHealth.v1",
    "clawdi.hostPolicy.v1",
    "clawdi.liveSyncEnvironments.v1",
    "clawdi.managedBaileysCompat.v3",
    "clawdi.managedBaileysPatchReceipt.v4",
    "clawdi.managedBaileysPatchReceipt.v999",
    "clawdi.managedSkillReservations.v1",
    "clawdi.managedWhatsAppAuth.v1",
    "clawdi.managedWhatsAppSocket.v1",
    "clawdi.nativeInstall.v1",
    "clawdi.nativeOAuthCredentialEvidence.v1",
    "clawdi.nativeRelease.v1",
    "clawdi.oauthCredentialOwnership.v2",
    "clawdi.openclawManifestSkillReceipt.v2",
    "clawdi.privateDirectoryLockOwner.v1",
    "clawdi.runtimeAppliedState.v2",
    "clawdi.runtimeApplyIdentity.v1",
    "clawdi.runtimeBootStatus.v1",
    "clawdi.runtimeContext.v2",
    "clawdi.runtimeConvergeLockOwner.v1",
    "clawdi.runtimeDesiredState.v1",
    "clawdi.runtimeEgressSidecarSecrets.v1",
    "clawdi.runtimeHeartbeatObservation.v1",
    "clawdi.runtimeInstallInventory.v1",
    "clawdi.runtimeInstallReceipts.v1",
    "clawdi.runtimeInstanceData.v1",
    "clawdi.runtimeOAuthCredential.v1",
    "clawdi.runtimeProjection.v1",
    "clawdi.runtimeRunConfig.v1",
    "clawdi.runtimeSensitiveInstanceData.v1",
    "clawdi.runtimeStatus.v1",
    "clawdi.runtimeSyncState.v1",
    "clawdi.runtimeVerify.v1",
    "clawdi.runtimeWatchEvent.v1",
    "clawdi.runtimeWatchStatus.v1",
    "clawdi.share.v1",
    "clawdi.whatsappBaileysAuthState.v1",
    "clawdi.whatsappBaileysProviderEvent.v1",
    "clawdi.whatsappBaileysProviderMessage.v1",
)

# R-4: identifiers that ARE allowed to move, because nothing persists or validates them. The
# native release protocol is read only by the release tooling in this repository (plan 04
# renames it to ``iforge.nativeRelease.v1``). ``authStatus`` and ``configPaths`` are the
# human/scripting output of ``iforge auth status --json`` and ``iforge config paths --json``:
# never stored, never parsed back (BRAND-02, 2026-10-22 user directive).
MOVABLE_SCHEMA_IDENTIFIERS = frozenset(
    {"clawdi.nativeRelease.v1", "clawdi.authStatus.v1", "clawdi.configPaths.v1"}
)

# Identifiers UPSTREAM retired, not ones this fork renamed. Upstream sync step 3 (06.1-08,
# ffffaa02 #1334): the runtime convergence rewrite (#1165-#1203), the retired instance-data
# replica (#1189) and the managed MCP / Baileys receipt moves removed every literal of these
# 18 from upstream's own tree (verified with `git grep -F` at ffffaa02: zero hits each, where
# a26c3fb3 had them). The C-4 check exists to catch a fork-side rename; an identifier upstream
# no longer writes or reads cannot be renamed by anyone, so it is excluded from the
# must-resolve set and kept in SCHEMA_IDENTIFIERS as the historical record of the 55.
RETIRED_UPSTREAM_SCHEMA_IDENTIFIERS = frozenset(
    {
        "clawdi.cliUpgradeState.v2",
        "clawdi.daemonAuthTokenRevision.v1",
        "clawdi.hostedManagedConfig.v1",
        "clawdi.hostedManagedMcpServers.v1",
        "clawdi.hostedManagedMcpServers.v2",
        "clawdi.hostedManagedMcpServers.v3",
        "clawdi.hostedRuntimeProviderHealth.v1",
        "clawdi.liveSyncEnvironments.v1",
        "clawdi.managedBaileysPatchReceipt.v4",
        "clawdi.managedBaileysPatchReceipt.v999",
        "clawdi.managedWhatsAppAuth.v1",
        "clawdi.runtimeHeartbeatObservation.v1",
        "clawdi.runtimeInstallInventory.v1",
        "clawdi.runtimeInstallReceipts.v1",
        "clawdi.runtimeInstanceData.v1",
        "clawdi.runtimeProjection.v1",
        "clawdi.runtimeSensitiveInstanceData.v1",
        "clawdi.runtimeSyncState.v1",
    }
)

_SCHEMA_IDENTIFIER_SHAPE = r"clawdi\.[A-Za-z][A-Za-z0-9]*\.v[0-9]+"

# Files that enumerate the identifiers rather than using them. Scanning them would make the
# C-4 check vacuous: this script alone would keep every name "resolving" forever.
_SCHEMA_SCAN_EXCLUDES: tuple[str, ...] = (
    ":(exclude).planning",
    ":(exclude)scripts/check_rebrand_carveouts.py",
    ":(exclude)backend/tests/test_rebrand_guards.py",
    ":(exclude)config/rebrand-allowlist.txt",
)


def validate_carve_outs(contents: dict[str, str] | None = None) -> int:
    """Assert every ``CARVE_OUTS`` literal is still present at its own path.

    ``contents`` maps repository-relative paths to file text. When it is ``None`` the mapping is
    read from ``ROOT``; tests inject a mapping instead so a red case never has to write a
    corrupted pin to disk.

    Returns the number of literals verified. Raises ``CarveOutError`` on the first miss, naming
    the path so the failure is actionable without reading this file.
    """
    if contents is None:
        contents = {path: (ROOT / path).read_text(encoding="utf-8") for path, _ in CARVE_OUTS}

    for path, literal in CARVE_OUTS:
        text = contents.get(path)
        if text is None:
            raise CarveOutError(f"{path} is absent from the contents given to the carve-out guard")
        if literal not in text:
            raise CarveOutError(f"{path} no longer contains the carve-out literal {literal!r}")

    return len(CARVE_OUTS)


def validate_alembic_revisions() -> int:
    """Assert both C-10 migration files are still at their original paths."""
    for path in ALEMBIC_REVISION_FILES:
        if not (ROOT / path).is_file():
            raise CarveOutError(f"{path} was renamed or removed; Alembic resolves by revision id")

    return len(ALEMBIC_REVISION_FILES)


def _scan_schema_identifiers() -> set[str]:
    """Every ``clawdi.<name>.vN`` literal that still resolves somewhere in the tree."""
    completed = subprocess.run(
        ["git", "grep", "-Iho", "-E", _SCHEMA_IDENTIFIER_SHAPE, "--", *_SCHEMA_SCAN_EXCLUDES],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode == 1:
        # git grep exits 1 when nothing matched. Every identifier is then missing, which the
        # caller reports far more usefully than an empty-output exception would.
        return set()
    if completed.returncode != 0:
        raise CarveOutError(
            f"git grep failed while scanning schema identifiers: {completed.stderr.strip()}"
        )

    return {line.strip() for line in completed.stdout.splitlines() if line.strip()}


def validate_schema_identifiers() -> int:
    """Assert all 55 C-4 identifiers still resolve, except the one R-4 allows to move and the
    ones upstream retired."""
    found = _scan_schema_identifiers()
    missing = sorted(
        identifier
        for identifier in SCHEMA_IDENTIFIERS
        if identifier not in found
        and identifier not in MOVABLE_SCHEMA_IDENTIFIERS
        and identifier not in RETIRED_UPSTREAM_SCHEMA_IDENTIFIERS
    )
    if missing:
        raise CarveOutError(
            f"{len(missing)} wire-protocol schema identifiers no longer resolve: "
            + ", ".join(missing)
        )

    return len(SCHEMA_IDENTIFIERS)


def main() -> None:
    try:
        count = validate_carve_outs()
        count += validate_alembic_revisions()
        count += validate_schema_identifiers()
    except CarveOutError as error:
        raise SystemExit(f"rebrand carve-outs failed: {error}") from error
    print(f"rebrand carve-outs passed: {count} literals verified")


if __name__ == "__main__":
    main()
