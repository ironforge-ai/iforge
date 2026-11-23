# Hosted-runtime host for the phase-05 companion UAT (tests 11, 16).
#
# `runtime watch` refuses to start unless it is running as the production
# hosted identity — `assertHostedRuntimeContract` requires HOME=/home/iforge
# and a real passwd entry for `iforge` at uid/gid 10001:10001, rejecting root
# outright. That identity is a property of the provisioning image, which is why
# the UAT could not be walked on a developer laptop without either creating
# that user system-wide or standing up an image that already has it. This is
# the image.
#
# It deliberately does NOT bake in a companion. The executor addresses
# companions at a hardcoded loopback host (`COMPANION_HOST = "127.0.0.1"` in
# companion-executor.ts — no URL ever comes off the wire, which is what makes
# SSRF structurally absent rather than filtered), so the container runs with
# `--network host` and reaches the same provisioned filebrowser that UAT test 2
# already settled A2/A3 against. A second, differently-configured companion
# baked in here would be weaker evidence, not stronger.

ARG BUN_VERSION=1.3.14
ARG NODE_VERSION=24.18.0

FROM oven/bun:${BUN_VERSION} AS bun
FROM node:${NODE_VERSION}-bookworm

COPY --from=bun /usr/local/bin/bun /usr/local/bin/bun
COPY --from=bun /usr/local/bin/bunx /usr/local/bin/bunx

# systemd is not optional here. In hosted mode as root, `defaultPlatformRoot`
# resolves to /run/systemd/system and the watch refuses to create its state
# directories without it — and root is itself required, because provisioning a
# companion allocates a dedicated service user (`ensureFileBrowserServiceIsolation`
# checks runningAsRoot() before anything else). The tenant runtime user stays
# non-root; the supervisor is root. Same shape as the existing
# runtime-official-installer-systemd fixture.
# `nftables` is required, not optional: transparent egress is part of the hosted
# contract (dropping the mitmproxy pin fails convergence with "required egress
# engine is not ready"), and the sidecar shells out to `nft -f -`. Under
# --network host its rules land in the HOST's netfilter. They are uid-scoped --
# one `inet clawdi_transparent_egress` table redirecting only uid 10001's
# tcp/80,443 -- and `nft delete table inet clawdi_transparent_egress` removes
# them (the name is TRANSPARENT_EGRESS_TABLE). See the teardown note in
# scripts/uat-hosted-runtime.sh.
RUN apt-get update \
	&& DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
		ca-certificates \
		curl \
		dbus-user-session \
		jq \
		nftables \
		systemd \
		systemd-sysv \
	&& rm -rf /var/lib/apt/lists/*

# The identity the hosted contract pins. uid AND gid must both be 10001:
# `resolveRuntimeUserIdentity` does a real passwd lookup and compares both,
# and refuses a root identity.
RUN groupadd --gid 10001 iforge \
	&& useradd --uid 10001 --gid 10001 --create-home --home-dir /home/iforge --shell /bin/bash iforge

# The paired-fixture CLI bootstrap. The path is a fixed literal in
# manifest-contract.ts (HOSTED_RUNTIME_PAIRED_FIXTURE_CLI_PACKAGE) — no other
# location is accepted — and it is how a hosted runtime runs a LOCALLY BUILT
# CLI instead of a published one.
#
# Using the published `clawdi` package instead would install cleanly and then
# self-reexec into a binary containing neither the companion-executor wiring
# (#40) nor the `source` fix (#42), producing a green run whose executor is
# missing the exact code under test. The tarball is the honest path; the
# published package is the stand-in.
RUN mkdir -p /usr/local/share/clawdi/bootstrap
COPY --chown=root:root clawdi-local.tgz /usr/local/share/clawdi/bootstrap/clawdi-local.tgz
RUN chmod 0444 /usr/local/share/clawdi/bootstrap/clawdi-local.tgz

# Writable state roots. CLAWDI_SERVICE_STATE_DIR derives config/ and cache/
# from the PARENT of the path it is given, so /srv/iforge/state yields
# /srv/iforge/config and /srv/iforge/cache.
# The run root must stay ROOT-OWNED and must not be a symlink: companion
# isolation refuses to reconcile against a runtime directory the tenant could
# have tampered with (`file-browser-isolation.ts` checks lstat uid === 0).
# Everything else is tenant state.
RUN mkdir -p /srv/iforge/state /srv/iforge/config /srv/iforge/cache /srv/iforge/run \
	&& chown -R iforge:iforge /srv/iforge/state /srv/iforge/config /srv/iforge/cache \
	&& chown root:root /srv/iforge/run \
	&& chmod 0755 /srv/iforge/run

# Root, deliberately — see the systemd note above. CLAWDI_RUNTIME_USER names
# the non-root tenant identity the contract pins; it is read from the
# environment, not from the process uid.
ENV HOME=/home/iforge \
	IFORGE_RUNTIME_MODE=hosted \
	CLAWDI_RUNTIME_USER=iforge \
	CLAWDI_SERVICE_STATE_DIR=/srv/iforge/state \
	IFORGE_RUN_DIR=/srv/iforge/run \
	CLAWDI_RUNTIME_ALLOW_TEST_INSTALLERS=1

WORKDIR /repo
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
