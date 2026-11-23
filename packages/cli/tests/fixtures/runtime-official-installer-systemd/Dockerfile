ARG BUN_VERSION=1.4.0
ARG NODE_VERSION=24.19.0
ARG UV_VERSION=0.12.5

FROM oven/bun:${BUN_VERSION} AS bun
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv
FROM node:${NODE_VERSION}-trixie AS systemd

COPY --from=bun /usr/local/bin/bun /usr/local/bin/bun
COPY --from=bun /usr/local/bin/bunx /usr/local/bin/bunx
COPY --from=uv /uv /usr/local/bin/uv

RUN apt-get update \
	&& DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
		ca-certificates \
		dbus-user-session \
		nftables \
		python3 \
		systemd \
		systemd-sysv \
	&& rm -rf /var/lib/apt/lists/*

# The disposable container must not configure its host's shared kernel.
RUN systemctl mask systemd-sysctl.service systemd-modules-load.service

RUN groupadd --gid 10001 iforge \
	&& useradd --uid 10001 --gid 10001 --create-home --home-dir /home/iforge --shell /bin/bash iforge

ENV container=docker
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]

FROM systemd AS runtimes

# `openclaw --version` identifies this published package as official source
# commit ea806575e6450e4d1efdfc72c19f04be982a1b9b. npm omits gitHead, so the
# registry integrity and the CLI's own commit identity are both verified.
ARG OPENCLAW_VERSION=2026.8.1
ARG OPENCLAW_COMMIT=ea806575e6450e4d1efdfc72c19f04be982a1b9b
ARG OPENCLAW_INTEGRITY=sha512-bSaFeaDFnQH/bU1vgKMac6eHkHHPHG0C/uwduXGI3eIS3lyiYSwmDU5ehhBUUhlPeV85tL5/KVwmoH48nX1tWw==
ARG OPENCLAW_NODE_VERSION=24.15.0
RUN npm pack "openclaw@${OPENCLAW_VERSION}" --pack-destination /tmp --silent >/dev/null \
	&& OPENCLAW_TARBALL="/tmp/openclaw-${OPENCLAW_VERSION}.tgz" \
		OPENCLAW_EXPECTED_INTEGRITY="${OPENCLAW_INTEGRITY}" \
		node --input-type=module --eval \
			'import { createHash } from "node:crypto"; import { readFileSync } from "node:fs"; const actual = `sha512-${createHash("sha512").update(readFileSync(process.env.OPENCLAW_TARBALL)).digest("base64")}`; if (actual !== process.env.OPENCLAW_EXPECTED_INTEGRITY) { throw new Error(`OpenClaw package integrity mismatch: ${actual}`); }' \
	&& OPENCLAW_NODE_DIR="/home/iforge/.local/tools/node-v${OPENCLAW_NODE_VERSION}" \
	&& mkdir -p "${OPENCLAW_NODE_DIR}" /home/iforge/.local/bin \
	&& chown -R iforge:iforge /home/iforge/.local \
	&& runuser -u iforge -- env HOME=/home/iforge npm install --global \
		--prefix "${OPENCLAW_NODE_DIR}" \
		"/tmp/openclaw-${OPENCLAW_VERSION}.tgz" \
		--ignore-scripts --no-audit --no-fund \
	&& rm "/tmp/openclaw-${OPENCLAW_VERSION}.tgz" \
	&& ln -s /usr/local/bin/node "${OPENCLAW_NODE_DIR}/bin/node" \
	&& ln -s "${OPENCLAW_NODE_DIR}" /home/iforge/.local/tools/node \
	&& printf '%s\n' \
		'#!/usr/bin/env bash' \
		'set -euo pipefail' \
		"exec \"/home/iforge/.local/tools/node/bin/node\" \"${OPENCLAW_NODE_DIR}/lib/node_modules/openclaw/dist/entry.js\" \"\$@\"" \
		> /home/iforge/.local/bin/openclaw \
	&& chown -h iforge:iforge \
		/home/iforge/.local/bin/openclaw \
		/home/iforge/.local/tools/node \
		"${OPENCLAW_NODE_DIR}/bin/node" \
	&& chmod 0755 /home/iforge/.local/bin/openclaw \
	&& OPENCLAW_VERSION_OUTPUT="$(runuser -u iforge -- env HOME=/home/iforge \
		/home/iforge/.local/bin/openclaw --version)" \
	&& printf '%s\n' "$OPENCLAW_VERSION_OUTPUT" | grep -F "OpenClaw ${OPENCLAW_VERSION}" \
	&& printf '%s\n' "$OPENCLAW_VERSION_OUTPUT" \
		| grep -F "$(printf '%s' "$OPENCLAW_COMMIT" | cut -c1-7)"

RUN mkdir -p /opt/stock/base-home /opt/stock/openclaw-home \
	&& cp -a /home/iforge/.bash_logout /home/iforge/.bashrc /home/iforge/.profile \
		/opt/stock/base-home/ \
	&& cp -a /home/iforge/.local /opt/stock/openclaw-home/.local \
	&& chown -R iforge:iforge /opt/stock/base-home /opt/stock/openclaw-home

ARG HERMES_COMMIT=cc4cab2f592e60a197e796506de9168f74baf3ea
ARG HERMES_VERSION=0.19.1
ADD --checksum=sha256:e75ad450f3e0932737bbfff3a0a997d4ed532dd8b5ece024072274791a648679 \
	https://github.com/NousResearch/hermes-agent/archive/cc4cab2f592e60a197e796506de9168f74baf3ea.tar.gz \
	/tmp/hermes-agent.tar.gz
RUN mkdir -p /home/iforge/.hermes/hermes-agent \
	&& tar --extract --gzip --file /tmp/hermes-agent.tar.gz \
		--directory /home/iforge/.hermes/hermes-agent --strip-components=1 \
	&& chown -R iforge:iforge /home/iforge/.hermes \
	&& runuser -u iforge -- env \
		HOME=/home/iforge \
		UV_PROJECT_ENVIRONMENT=/home/iforge/.hermes/hermes-agent/venv \
		uv sync \
			--project /home/iforge/.hermes/hermes-agent \
			--python /usr/bin/python3 \
			--frozen --extra messaging --no-dev \
	&& printf '%s\n' \
		'#!/usr/bin/env bash' \
		'set -euo pipefail' \
		'exec "$HOME/.hermes/hermes-agent/venv/bin/python" -m hermes_cli.main "$@"' \
		> /home/iforge/.local/bin/hermes \
	&& chmod 0755 /home/iforge/.local/bin/hermes \
	&& chown iforge:iforge /home/iforge/.local/bin/hermes \
	&& runuser -u iforge -- env HOME=/home/iforge \
		/home/iforge/.local/bin/hermes --version \
		| grep -F "Hermes Agent v${HERMES_VERSION}" \
	&& mkdir -p /opt/stock/hermes-home/.local/bin \
	&& cp -a /home/iforge/.hermes /opt/stock/hermes-home/.hermes \
	&& cp -a /home/iforge/.local/bin/hermes /opt/stock/hermes-home/.local/bin/hermes \
	&& chown -R iforge:iforge /opt/stock/hermes-home \
	&& rm -rf /home/iforge/.hermes /home/iforge/.local/bin/hermes \
		/tmp/hermes-agent.tar.gz

COPY install-stock-runtime.sh /opt/fixture/install-stock-runtime.sh
RUN chmod 0755 /opt/fixture/install-stock-runtime.sh

ENV CLAWDI_TEST_OPENCLAW_VERSION=${OPENCLAW_VERSION} \
	CLAWDI_TEST_OPENCLAW_COMMIT=${OPENCLAW_COMMIT} \
	CLAWDI_TEST_OPENCLAW_NODE_VERSION=${OPENCLAW_NODE_VERSION} \
	CLAWDI_TEST_HERMES_VERSION=${HERMES_VERSION} \
	CLAWDI_TEST_HERMES_COMMIT=${HERMES_COMMIT}

STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
