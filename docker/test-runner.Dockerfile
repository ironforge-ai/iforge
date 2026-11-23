ARG BUN_VERSION=1.4.0
ARG NODE_VERSION=24.19.0
ARG UV_VERSION=0.12.5

FROM oven/bun:${BUN_VERSION} AS bun
FROM node:${NODE_VERSION}-bookworm AS node
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

FROM python:3.12-bookworm

COPY --from=uv /uv /uvx /usr/local/bin/
COPY --from=bun /usr/local/bin/bun /usr/local/bin/bun
COPY --from=bun /usr/local/bin/bunx /usr/local/bin/bunx
COPY --from=node /usr/local/bin/node /usr/local/bin/node

RUN apt-get update \
	&& apt-get install -y --no-install-recommends \
		bash \
		build-essential \
		ca-certificates \
		curl \
		git \
		postgresql-client \
		rsync \
	&& rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 1000 clawdi-test \
	&& useradd --uid 1000 --gid 1000 --home-dir /tmp/clawdi-home --no-create-home clawdi-test \
	&& install -d -m 0755 -o clawdi-test -g clawdi-test /etc/clawdi

# The update suppression is set under BOTH spellings on purpose (WR-03).
#
# IFORGE_* is the public contract name and the only supported way to configure the
# CLI, so it is what every process that crosses the brand-env boundary must see.
# But `update.ts` READS the internal register (`CLAWDI_NO_AUTO_UPDATE`,
# `CLAWDI_NO_UPDATE_CHECK`), and the only thing that maps one onto the other is
# `applyBrandEnvAliases()` in `packages/cli/src/index.ts`. `bun test` imports
# modules directly and never executes index.ts, so under the public spelling alone
# the suppression reaches only the tests that SPAWN the real CLI -- it stopped
# reaching the whole in-process suite, which it did reach before the rename.
#
# Setting both is a supported state, not a workaround, and it was verified by
# executing the boundary rather than by reading it:
# `refuseUserSuppliedInternalNames` deletes an internal name only when its public
# counterpart is absent or empty (brand-env.ts), so with IFORGE_* present it
# refuses NOTHING here; `applyBrandEnvAliases` then writes the same value the
# internal name already carries. A process that runs the boundary therefore ends
# up in an environment identical to the public-only one, and a process that does
# not run it (the in-process suite) gets the suppression back. Measured for both
# names in all three states: both-set -> refused [], applied 2; public-only ->
# refused [], applied 2; internal-only -> both refused and deleted.
#
# The clean fix is the bun `preload` that calls applyBrandEnvAliases() for the
# whole suite (prior-review IN-02); this restores the lost coverage without
# waiting for it.
ENV HOME=/tmp/clawdi-home \
	USER=clawdi-test \
	LOGNAME=clawdi-test \
	IFORGE_NO_AUTO_UPDATE=1 \
	IFORGE_NO_UPDATE_CHECK=1 \
	CLAWDI_NO_AUTO_UPDATE=1 \
	CLAWDI_NO_UPDATE_CHECK=1 \
	BUN_INSTALL_CACHE_DIR=/var/cache/bun \
	BUN_TMPDIR=/tmp/bun \
	TMPDIR=/tmp \
	UV_CACHE_DIR=/var/cache/uv \
	UV_LINK_MODE=copy \
	UV_PYTHON_DOWNLOADS=never \
	PDM_IGNORE_SAVED_PYTHON=1

WORKDIR /work
RUN chown 1000:1000 /work && chmod 755 /work
