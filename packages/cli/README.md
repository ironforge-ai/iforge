# iforge CLI

This package publishes the `iforge` command-line interface. The CLI registers
local AI agents, syncs sessions and skills, manages memory and vault workflows,
installs the MCP server, and controls the background sync daemon.

For product usage, start with the
[Connected Agent quickstart](https://docs.clawdi.ai/getting-started/quickstart).
For Cloud context and remote Skill commands, testing, and contributor workflows,
read [`docs/cli-development.md`](../../docs/cli-development.md). For a full local
backend + dashboard + CLI stack, use the canonical runbook in
[`AGENTS.md`](../../AGENTS.md#local-end-to-end).

## Quickstart

> **Note:** iforge is not published to a public registry yet, so there is no one-line install today: build the CLI from source with the steps below.

From a checkout of the repository, with Bun installed:

```bash
bun install
bun run packages/cli/src/index.ts --help
```

Once the CLI is on your PATH:

```bash
iforge auth login
iforge setup
iforge doctor
```

Self-update is unavailable for the same reason: `iforge update` resolves its
native install route under the GitHub Release path recorded in
[`config/distribution-state.txt`](../../config/distribution-state.txt), which
declares `public_install_routes=unavailable`. Once publishing happens, native
installs will update from checksum-verified exact GitHub Release assets and
npm/Bun installs from exact npm versions. Hosted version transactions are
separate and never invoke native self-update.

## Development

From the repository root:

```bash
bun install
bun run packages/cli/src/index.ts --help
bun run --cwd packages/cli typecheck
bun run --cwd packages/cli test
bun run --cwd packages/cli build
```

The package manager is Bun; do not use `pdm` for CLI dependencies or tests.
