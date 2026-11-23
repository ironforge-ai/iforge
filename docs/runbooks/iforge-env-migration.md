# Migrating Your `.env` Files to `IFORGE_*`

Phase 03.1 moved this project's public environment contract from the old
`CLAWDI_*` prefix to `IFORGE_*`. Every tracked file moved with it, but `.env`
files are untracked by design, so yours did not: nothing in the repository can
read or edit them.

Use this runbook once, per machine, after pulling the rename. Until you do, a
local stack started from an un-migrated `.env` loses that configuration
silently rather than loudly -- the process starts, and the settings you thought
you had are simply absent.

## Which files

Most developers have some of:

- `apps/web/.env` -- the web app's build-time keys
- `backend/.env` -- the backend service
- `deploy/self-host/.env` -- the self-hosted Compose stack
- a shell profile (`~/.bashrc`, `~/.zshrc`, `~/.profile`) that exports any of
  these names
- anything else you keep out of the tree: a direnv `.envrc`, a secret manager
  entry, a CI or hosting-provider variable set

That list is a starting point, not an authority. The authority for *which
names* is [`config/rebrand-env-contract.txt`](../../config/rebrand-env-contract.txt),
and the recipe below reads it rather than a copy of it, so it stays correct if
the contract ever grows.

## What changed

57 names in total: 53 runtime variables and 4 web
build-time keys. Every one keeps its meaning and its value; only the prefix
moves.

### CLI and Cloud API

Read by the `iforge` CLI. The first eight are the ones `iforge --help` lists under `Environment:`; their wording here is that help text.

| Old name | New name | Purpose |
| --- | --- | --- |
| `CLAWDI_API_URL` | `IFORGE_API_URL` | Override the Cloud API endpoint |
| `CLAWDI_DEPLOY_API_URL` | `IFORGE_DEPLOY_API_URL` | Override the Hosted deploy API endpoint |
| `CLAWDI_AUTH_TOKEN` | `IFORGE_AUTH_TOKEN` | Authenticate non-interactive Cloud API requests |
| `CLAWDI_AUTH_TOKEN_ORIGIN` | `IFORGE_AUTH_TOKEN_ORIGIN` | Explicit Cloud origin binding for `IFORGE_AUTH_TOKEN` |
| `CLAWDI_DEBUG` | `IFORGE_DEBUG` | Print stack traces on error |
| `CLAWDI_NO_UPDATE_CHECK` | `IFORGE_NO_UPDATE_CHECK` | Suppress the non-blocking update check |
| `CLAWDI_NO_AUTO_UPDATE` | `IFORGE_NO_AUTO_UPDATE` | Skip CLI/daemon background auto-update |
| `CLAWDI_RUNTIME_MODE` | `IFORGE_RUNTIME_MODE` | Explicit runtime mode override for hosted tests and operators |
| `CLAWDI_SECRET_EXPORT_PASSPHRASE` | `IFORGE_SECRET_EXPORT_PASSPHRASE` | Passphrase for encrypted secret export (`docs/ai-providers.md`) |

### Installer

Read by `install.sh` when you install or pin the CLI.

| Old name | New name | Purpose |
| --- | --- | --- |
| `CLAWDI_CHANNEL` | `IFORGE_CHANNEL` | Release channel to install from |
| `CLAWDI_VERSION` | `IFORGE_VERSION` | Exact version to install instead of the latest |
| `CLAWDI_INSTALL_PREFIX` | `IFORGE_INSTALL_PREFIX` | Install root, instead of the default prefix |

### Local daemon

Read by `iforge daemon`. Documented in `docs/iforge-daemon-test-guide.md`.

| Old name | New name | Purpose |
| --- | --- | --- |
| `CLAWDI_AGENT_TYPE` | `IFORGE_AGENT_TYPE` | Agent the daemon runs against |
| `CLAWDI_ENVIRONMENT_ID` | `IFORGE_ENVIRONMENT_ID` | Environment the daemon binds its sessions to |
| `CLAWDI_SERVE_MODE` | `IFORGE_SERVE_MODE` | Daemon serve mode |
| `CLAWDI_SERVE_DEBUG` | `IFORGE_SERVE_DEBUG` | Verbose daemon logging |
| `CLAWDI_STATE_DIR` | `IFORGE_STATE_DIR` | Override location of the daemon's `queue.jsonl` + health state (default `~/.iforge/serve`) |
| `CLAWDI_DAEMON_RPC_HOST` | `IFORGE_DAEMON_RPC_HOST` | Address the daemon RPC listener binds |
| `CLAWDI_DAEMON_RPC_PORT` | `IFORGE_DAEMON_RPC_PORT` | Port the daemon RPC listener binds |
| `CLAWDI_DAEMON_RPC_TOKEN` | `IFORGE_DAEMON_RPC_TOKEN` | Bearer token the daemon RPC listener requires |
| `CLAWDI_DAEMON_RPC_ALLOW_REMOTE` | `IFORGE_DAEMON_RPC_ALLOW_REMOTE` | Allow non-loopback daemon RPC connections |

### Managed runtime

Read inside the managed runtime image. Documented in `docs/managed-runtime.md` and ADR-0002.

| Old name | New name | Purpose |
| --- | --- | --- |
| `CLAWDI_RUNTIME_HOME` | `IFORGE_RUNTIME_HOME` | Home directory of the runtime OS user |
| `CLAWDI_RUNTIME_UID` | `IFORGE_RUNTIME_UID` | UID the runtime process drops to |
| `CLAWDI_RUNTIME_GID` | `IFORGE_RUNTIME_GID` | GID the runtime process drops to |
| `CLAWDI_EGRESS_UID` | `IFORGE_EGRESS_UID` | UID the egress guard matches on |
| `CLAWDI_EGRESS_GID` | `IFORGE_EGRESS_GID` | GID the egress guard matches on |
| `CLAWDI_RUN_DIR` | `IFORGE_RUN_DIR` | Runtime scratch directory |
| `CLAWDI_SYSTEMD_APPLY` | `IFORGE_SYSTEMD_APPLY` | Apply systemd unit changes rather than only rendering them |
| `CLAWDI_PROCESS_ROLE` | `IFORGE_PROCESS_ROLE` | Process role a container announces (`config/deploy.yml`) |

### Managed WhatsApp sidecar

Read by the Baileys sidecar and its egress guard. Declared in `config/deploy.yml`.

| Old name | New name | Purpose |
| --- | --- | --- |
| `CLAWDI_WA_SIDECAR_SOCKET_PATH` | `IFORGE_WA_SIDECAR_SOCKET_PATH` | Unix socket the sidecar listens on |
| `CLAWDI_WA_SIDECAR_STATE_ROOT` | `IFORGE_WA_SIDECAR_STATE_ROOT` | Sidecar state root on the host |
| `CLAWDI_WA_SIDECAR_LOG_LEVEL` | `IFORGE_WA_SIDECAR_LOG_LEVEL` | Sidecar log level |
| `CLAWDI_WA_NETWORK_NAMESPACE_MARKER` | `IFORGE_WA_NETWORK_NAMESPACE_MARKER` | Readiness marker for the shared network namespace |
| `CLAWDI_WA_WEBSOCKET_URL` | `IFORGE_WA_WEBSOCKET_URL` | WebSocket endpoint the sidecar dials |

### Self-hosted stack

Read by `deploy/self-host/docker-compose.yml`; set them in `deploy/self-host/.env`.

| Old name | New name | Purpose |
| --- | --- | --- |
| `CLAWDI_BACKEND_IMAGE` | `IFORGE_BACKEND_IMAGE` | Backend container image |
| `CLAWDI_BACKEND_IMAGE_TAG` | `IFORGE_BACKEND_IMAGE_TAG` | Backend container image tag |
| `CLAWDI_WEB_IMAGE` | `IFORGE_WEB_IMAGE` | Web container image |
| `CLAWDI_WEB_IMAGE_TAG` | `IFORGE_WEB_IMAGE_TAG` | Web container image tag |
| `CLAWDI_CLOUD_ADMIN_API_KEY` | `IFORGE_CLOUD_ADMIN_API_KEY` | Admin API key the backend accepts (`backend/.env`) |

### Clean test runner

Resource limits for `bun run test`. Only set these if you are tuning the runner; documented in `docs/clean-test-runner.md`.

| Old name | New name | Purpose |
| --- | --- | --- |
| `CLAWDI_TEST_RUNNER_CPUS` | `IFORGE_TEST_RUNNER_CPUS` | CPU limit for the test-runner container |
| `CLAWDI_TEST_RUNNER_MEMORY_LIMIT` | `IFORGE_TEST_RUNNER_MEMORY_LIMIT` | Memory limit for the test-runner container |
| `CLAWDI_TEST_RUNNER_PIDS_LIMIT` | `IFORGE_TEST_RUNNER_PIDS_LIMIT` | PID limit for the test-runner container |
| `CLAWDI_TEST_POSTGRES_CPUS` | `IFORGE_TEST_POSTGRES_CPUS` | CPU limit for the test PostgreSQL container |
| `CLAWDI_TEST_POSTGRES_MEMORY_LIMIT` | `IFORGE_TEST_POSTGRES_MEMORY_LIMIT` | Memory limit for the test PostgreSQL container |
| `CLAWDI_TEST_POSTGRES_PIDS_LIMIT` | `IFORGE_TEST_POSTGRES_PIDS_LIMIT` | PID limit for the test PostgreSQL container |

### Added at upstream sync step 4

Named in the AI provider and managed runtime docs.

| Old name | New name | Purpose |
| --- | --- | --- |
| `CLAWDI_CLOUD_WORKLOAD_SIGNING_PRIVATE_KEY` | `IFORGE_CLOUD_WORKLOAD_SIGNING_PRIVATE_KEY` | Private PEM for the optional Cloud workload signer, referenced from `PLATFORM_WORKLOAD_SIGNING_KEY_REFS` |
| `CLAWDI_OPENCLAW_HOSTED_SOURCE` | `IFORGE_OPENCLAW_HOSTED_SOURCE` | Hosted source checkout for the paired OpenClaw native test (`scripts/test-openclaw-native.sh`) |

### Desktop release names (06.1-14)

Read when packaging the desktop app (`apps/desktop`, `bun run package:release`). No iforge
build ever read the old spellings: upstream's desktop arrived at sync step 3 and was
rebranded before anything shipped, so these rows are here only because the table covers the
whole contract.

| Old name | New name | Purpose |
| --- | --- | --- |
| `CLAWDI_DESKTOP_ARCH` | `IFORGE_DESKTOP_ARCH` | Desktop package architecture (`arm64` or `x64`) |
| `CLAWDI_DESKTOP_UPDATE_CHANNEL` | `IFORGE_DESKTOP_UPDATE_CHANNEL` | Desktop update channel (`stable` or `beta`) |
| `CLAWDI_DESKTOP_UPDATE_FEED_URL` | `IFORGE_DESKTOP_UPDATE_FEED_URL` | Strict HTTPS generic update feed baked into a release |
| `CLAWDI_DESKTOP_VERSION` | `IFORGE_DESKTOP_VERSION` | Explicit desktop release version |
| `CLAWDI_WINDOWS_PUBLISHER` | `IFORGE_WINDOWS_PUBLISHER` | Full Authenticode certificate Subject DN for signed Windows builds |
| `CLAWDI_WINDOWS_TASK_TEST` | `IFORGE_WINDOWS_TASK_TEST` | Opt-in for the native Windows Task Scheduler lifecycle test |

### Web build-time keys

Substituted into the bundle by Vite at BUILD time, so no runtime alias can reach them: a stale name here is a silently wrong build, not a warning. They are written as comments in the contract file for that reason.

| Old name | New name | Purpose |
| --- | --- | --- |
| `VITE_CLAWDI_API_URL` | `VITE_IFORGE_API_URL` | Cloud API endpoint the web app calls |
| `VITE_CLAWDI_DEPLOY_API_URL` | `VITE_IFORGE_DEPLOY_API_URL` | Hosted deploy API endpoint the web app calls |
| `VITE_CLAWDI_HOSTED` | `VITE_IFORGE_HOSTED` | Compile-time hosted-mode gate |
| `VITE_CLAWDI_LEGACY_DASHBOARD_URL` | `VITE_IFORGE_LEGACY_DASHBOARD_URL` | Legacy dashboard the web app links to |

## Rename them

**This edits files in place.** Back up or commit your `.env` files first --
they are untracked, so an unwanted edit is not recoverable from git.

The command builds its rename rules from the contract file, so it renames
exactly the public names and nothing else:

```bash
# From the repository root. Repeat the last two lines for each file you have.
sed -E 's/^#[[:space:]]*//' config/rebrand-env-contract.txt \
  | grep -E '^(VITE_)?IFORGE_[A-Z0-9_]+=(VITE_)?CLAWDI_[A-Z0-9_]+$' \
  | awk -F= '{ printf "s/^([[:space:]]*(export[[:space:]]+)?)%s=/\\1%s=/;\n", $2, $1 }' \
  > /tmp/iforge-env-rename.sed

file=apps/web/.env                          # then backend/.env, deploy/self-host/.env, ...
cp -- "$file" "$file.pre-iforge.bak"
sed -E -f /tmp/iforge-env-rename.sed -- "$file" > "$file.iforge" \
  && mv -- "$file.iforge" "$file"
```

It matches an assignment at the start of a line, with or without a leading
`export`, so it covers both `.env` files and shell profiles. It rewrites names
only, never values.

Worked example. Given:

```
CLAWDI_API_URL=http://localhost:8000
  export CLAWDI_AUTH_TOKEN=...
VITE_CLAWDI_HOSTED=true
CLAWDI_HOME=/srv/iforge-state
```

you get:

```
IFORGE_API_URL=http://localhost:8000
  export IFORGE_AUTH_TOKEN=...
VITE_IFORGE_HOSTED=true
CLAWDI_HOME=/srv/iforge-state
```

`CLAWDI_HOME` is untouched on purpose. It is the state-root override, an
internal name with no `IFORGE_*` spelling (D-07 / X-1), and it only redirects
the CLI away from its default state root, `~/.iforge`. iforge never reads or
migrates the `clawdi` state tree (no Clawdi-name aliases, user directive
2026-09-30). The contract file lists it and the other exclusions with their
reasons.

## Verify

```bash
bun run --cwd apps/web build:oss
cd backend && uv run pytest -q -k config
```

Both exit 0. The backend check needs a migrated throwaway PostgreSQL; see
[`docs/backend-development.md#verification`](../backend-development.md#verification)
for the setup.

To confirm nothing was missed, grep your own files for the old prefix:

```bash
grep -nE '^[[:space:]]*(export[[:space:]]+)?(VITE_)?CLAWDI_' apps/web/.env backend/.env deploy/self-host/.env 2>/dev/null
```

Anything it still prints is either an exclusion the contract file names with a
reason, or a name this project does not define.

## If you find `CLAWDI_` inside the source

You have not found a bug. The rename is deliberately asymmetric: `iforge` is
the product name and `CLAWDI_*` survives as the *internal register* the alias
copies your `IFORGE_*` value into. Both halves of the boundary --
`packages/cli/src/lib/brand-env.ts` and `backend/app/core/brand_env.py` -- are
asserted equal to the contract file by tests.

So `IFORGE_<NAME>` is the only spelling you ever set, and `CLAWDI_<NAME>` is
the only spelling the internal code reads. Internal source and test symbols
stay on the old name on purpose, so this fork can keep merging from upstream
cheaply. `scripts/check_rebrand_residual.py` enforces which side of that
boundary each file sits on.
