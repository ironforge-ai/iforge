#!/usr/bin/env bash
# Stand up a hosted-runtime host for the phase-05 companion UAT (tests 11, 16).
#
# WHY THIS EXISTS
#
# `runtime watch` will not start outside the production hosted identity:
# `assertHostedRuntimeContract` requires HOME=/home/iforge and a real passwd
# entry for `iforge` at uid/gid 10001:10001, and refuses a root identity. On a
# developer machine that means either creating that user system-wide or running
# somewhere that already has it. This builds the somewhere.
#
# It also solves the half that has no tooling in this repository: the hosted CLI
# bootstrap. `runtime watch` installs its own CLI before converging, and the
# seeded `cli_package_spec` (clawdi@1.2.3-test) is unpublished. Pointing it at
# the PUBLISHED `clawdi` package would install — and then self-reexec into a
# binary containing neither the companion-executor wiring (#40) nor the `source`
# fix (#42), i.e. a green run whose executor lacks the code under test. So this
# packs THIS checkout and stages it at the paired-fixture path, which is a fixed
# literal in manifest-contract.ts and the supported way to run a locally built
# CLI in hosted mode.
#
# NETWORKING
#
# `--network host`, deliberately. The executor addresses companions at a
# hardcoded loopback (`COMPANION_HOST = "127.0.0.1"`); no URL ever arrives off
# the wire, which is what makes SSRF structurally absent rather than filtered.
# Sharing the host network means the container reaches the SAME provisioned
# filebrowser that UAT test 2 already settled A2/A3 against, and the same
# backend on :8000. A second companion baked into the image would be weaker
# evidence, not stronger.
#
# Usage:
#   bash scripts/uat-hosted-runtime.sh build      # pack the CLI + build the image
#   bash scripts/uat-hosted-runtime.sh up         # start the container
#   bash scripts/uat-hosted-runtime.sh context <runtime-context.json>
#                                                 # install the apply context
#   bash scripts/uat-hosted-runtime.sh bootstrap   # pre-install the CLI (see below)
#   bash scripts/uat-hosted-runtime.sh watch      # run the watch in the foreground
#   bash scripts/uat-hosted-runtime.sh shell      # a shell as iforge
#   bash scripts/uat-hosted-runtime.sh down       # remove the container AND its
#                                                 # host nft table (see `down`)
#
# The context file carries a bearer token, so `context` installs it 0600 owned
# by iforge and never echoes it.
#
# WHY `bootstrap` RATHER THAN LETTING THE WATCH INSTALL
#
# `applyRuntimeCliDesiredState` takes its package spec from the MANIFEST, and
# the backend's validator admits only `clawdi@<exact-semver>` — it cannot emit
# the bootstrap tarball path at all. The fixture is not wired in by naming it
# in the manifest; it is wired in by the CLI being ALREADY INSTALLED. The
# `pairedFixtureMatches` branch of `isCurrentCliInstall` reports "current", and
# no npm install runs, when all of these hold:
#
#   - CLAWDI_RUNTIME_ALLOW_TEST_INSTALLERS=1 (set in the image)
#   - the bootstrap status records packageSpec = the fixture literal
#   - its recorded version equals the manifest spec's exact semver
#   - cliManagedBin is a symlink to the installed bin, and both are executable
#
# So `bootstrap` installs this checkout's tarball into the version-specific
# prefix, points cliManagedBin at it, and writes that status. The manifest then
# keeps an ordinary `clawdi@<semver>` spec and the binary that runs is the one
# built from this tree — which is the whole point, since the published package
# carries neither the companion-executor wiring nor the `source` fix.
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${UAT_HOSTED_IMAGE:-iforge-hosted-runtime-uat:local}"
CONTAINER="${UAT_HOSTED_CONTAINER:-iforge-hosted-runtime-uat}"
DOCKERFILE="$REPO_ROOT/docker/hosted-runtime-uat.Dockerfile"

if [[ $# -lt 1 || "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
	awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "${BASH_SOURCE[0]}"
	exit 0
fi

need_container() {
	docker inspect "$CONTAINER" >/dev/null 2>&1 || {
		echo "container '$CONTAINER' is not running — run: bash scripts/uat-hosted-runtime.sh up" >&2
		exit 1
	}
}

case "$1" in
build)
	BUILD_DIR="$(mktemp -d)"
	trap 'rm -rf "$BUILD_DIR"' EXIT
	echo "==> building the CLI"
	(cd "$REPO_ROOT/packages/cli" && bun run build >/dev/null)
	echo "==> packing it to clawdi-local.tgz"
	TARBALL="$(cd "$REPO_ROOT/packages/cli" && npm pack --pack-destination "$BUILD_DIR" --silent | tail -1)"
	mv "$BUILD_DIR/$TARBALL" "$BUILD_DIR/clawdi-local.tgz"
	# The bin name is load-bearing: the installer resolves bin/iforge only. A
	# tarball without it installs and is then rejected by the installer's own check.
	cp "$DOCKERFILE" "$BUILD_DIR/Dockerfile"
	echo "==> building $IMAGE"
	docker build --tag "$IMAGE" "$BUILD_DIR"
	;;
up)
	docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
	# --privileged + tmpfs /run because the container runs systemd as pid 1;
	# --network host because the executor addresses companions at a hardcoded
	# loopback, so runtime and companion must share one.
	docker run --detach --privileged \
		--name "$CONTAINER" \
		--network host \
		--tmpfs /run \
		--tmpfs /run/lock \
		--volume "$REPO_ROOT:/repo:ro" \
		"$IMAGE" >/dev/null
	for _attempt in $(seq 1 150); do
		if docker exec "$CONTAINER" systemctl is-system-running 2>/dev/null \
			| grep -Eq '^(running|degraded)$'; then
			break
		fi
		sleep 0.1
	done
	docker exec "$CONTAINER" test -d /run/systemd/system
	# The runtime issues `systemctl --user` calls for the tenant's units, which
	# need that user's session bus to exist — without linger they fail with
	# "Failed to connect to bus".
	docker exec "$CONTAINER" loginctl enable-linger iforge >/dev/null 2>&1 || true
	docker exec "$CONTAINER" systemctl start user@10001.service >/dev/null 2>&1 || true
	for _attempt in $(seq 1 150); do
		if docker exec "$CONTAINER" test -S /run/user/10001/bus 2>/dev/null; then break; fi
		sleep 0.1
	done
	docker exec "$CONTAINER" test -S /run/user/10001/bus \
		|| echo "warning: tenant session bus is absent; systemctl --user calls will fail" >&2
	echo "started $CONTAINER (systemd up, --network host, repo at /repo:ro)"
	;;
context)
	need_container
	SRC="${2:?need a path to runtime-context.json}"
	[[ -r "$SRC" ]] || { echo "cannot read $SRC" >&2; exit 1; }
	# Never echoed: this file carries a runtime bearer token.
	docker exec -i --user root "$CONTAINER" \
		bash -c 'cat > /srv/iforge/config/runtime-context.json \
			&& chown iforge:iforge /srv/iforge/config/runtime-context.json \
			&& chmod 600 /srv/iforge/config/runtime-context.json' < "$SRC"
	echo "installed runtime-context.json (0600 iforge:iforge)"
	;;
bootstrap)
	need_container
	VERSION="$(python3 -c 'import json;print(json.load(open("'"$REPO_ROOT"'/packages/cli/package.json"))["version"])')"
	REGISTRY="${UAT_HOSTED_REGISTRY:-https://registry.npmjs.org}"
	echo "==> pre-installing the local CLI as version $VERSION"
	docker exec --env CLI_VERSION="$VERSION" --env CLI_REGISTRY="$REGISTRY" "$CONTAINER" bash -euc '
		STATE=/srv/iforge/state/maintained/clawdi
		PREFIX="$STATE/npm/packages/$CLI_VERSION"
		MANAGED_BIN="$STATE/bin/clawdi"
		TARBALL=/usr/local/share/clawdi/bootstrap/clawdi-local.tgz

		mkdir -p "$PREFIX" "$STATE/bin" "$STATE/status" /srv/iforge/cache/npm
		npm install -g --prefix "$PREFIX" "$TARBALL" 			--cache /srv/iforge/cache/npm --ignore-scripts --no-audit --no-fund >/dev/null

		# The installer resolves bin/iforge only; a package without it is rejected.
		ACTIVE=""
		if [ -x "$PREFIX/bin/iforge" ]; then ACTIVE="$PREFIX/bin/iforge"; fi
		[ -n "$ACTIVE" ] || { echo "installed package provides no CLI bin under $PREFIX/bin" >&2; exit 1; }

		REPORTED="$("$ACTIVE" --version | tr -d "[:space:]")"
		[ "$REPORTED" = "$CLI_VERSION" ] || {
			echo "installed CLI reports $REPORTED, expected $CLI_VERSION" >&2; exit 1; }

		ln -sfn "$ACTIVE" "$MANAGED_BIN.next" && mv -T "$MANAGED_BIN.next" "$MANAGED_BIN"

		# schemaVersion and field names mirror writeCliBootstrapStatus, which is
		# module-private. `verification` is omitted on purpose: leaving it out
		# makes isCurrentCliInstall re-smoke and re-verify the binary rather
		# than trust a value this script asserted.
		umask 077
		cat > "$STATE/status/cli-bootstrap.json" <<JSON
{
  "schemaVersion": "clawdi.cliNpmBootstrapStatus.v1",
  "generatedAt": "$(date -u +%Y-%m-%dT%H:%M:%S.000Z)",
  "status": "installed",
  "source": "npm",
  "packageSpec": "/usr/local/share/clawdi/bootstrap/clawdi-local.tgz",
  "registry": "$CLI_REGISTRY",
  "npmPrefix": "$PREFIX",
  "npmCache": "/srv/iforge/cache/npm",
  "activePath": "$MANAGED_BIN",
  "activeTarget": "$ACTIVE",
  "version": "$CLI_VERSION",
  "error": null
}
JSON
		echo "installed $ACTIVE -> $MANAGED_BIN (version $REPORTED)"
	'
	echo
	echo "Set the runtime state cli_package_spec to iforge://$VERSION-shaped spec:"
	echo "  clawdi@$VERSION"
	;;
watch)
	need_container
	shift
	# Root: companion provisioning allocates a service user and needs it. The
	# tenant identity travels in CLAWDI_RUNTIME_USER, already set in the image.
	# -t only when stdout is a terminal: the watch is equally a thing you pipe
	# into jq or a log, and -it against a non-tty fails outright.
	TTY_FLAGS=()
	[ -t 1 ] && TTY_FLAGS=(-it)
	docker exec "${TTY_FLAGS[@]}" "$CONTAINER" \
		bun run /repo/packages/cli/src/index.ts runtime watch "$@"
	;;
shell)
	need_container
	docker exec -it "$CONTAINER" bash
	;;
down)
	# The egress sidecar applies its nft rules with --network host, so the
	# `inet clawdi_transparent_egress` table lives in the HOST's netfilter and outlives the
	# container. Left behind with no sidecar listening, its redirect blackholes
	# uid 10001's tcp/80,443. Remove it here, before the container goes, and
	# tolerate every way it can be already-absent.
	# The name MUST match TRANSPARENT_EGRESS_TABLE in
	# packages/cli/src/runtime/transparent-egress.ts. Deleting a table that was
	# never created is silent, so a wrong name here fails invisibly and leaves a
	# live redirect blackholing the tenant uid's HTTP/HTTPS. Verify with
	# `nft list tables | grep clawdi`, never by probing one guessed name.
	if docker inspect "$CONTAINER" >/dev/null 2>&1; then
		docker exec "$CONTAINER" nft delete table inet clawdi_transparent_egress >/dev/null 2>&1 \
			&& echo "removed host nft table inet clawdi_transparent_egress" || true
	fi
	docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
	echo "removed $CONTAINER"
	;;
*)
	echo "unknown verb '$1' (expected build, up, context, bootstrap, watch, shell or down)" >&2
	exit 2
	;;
esac
