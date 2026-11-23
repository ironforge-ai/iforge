#!/bin/sh
# Proves that Kamal 2.12.0 itself renders kamal-proxy on loopback for config/deploy.yml.
#
# Docker's published ports bypass the host firewall, so the addresses kamal-proxy publishes
# 80 and 443 on decide what is reachable from the internet. This script renders kamal-proxy's
# `docker run` with Kamal's own classes and passes only when it publishes exactly
# 127.0.0.1:80:80 and 127.0.0.1:443:443 and keeps the `none` log driver. Its last line on
# success is:
#
#   kamal-proxy bind: --publish 127.0.0.1:80:80 --publish 127.0.0.1:443:443
#
# On a drift it exits nonzero with one line naming what it found (`published: ...` or
# `log driver: ...`).
#
# The render is local: a copy of the config, a throwaway secrets file holding fake values,
# a documentation address as DEPLOY_HOST. No host is contacted and no real secret is read.
#
# Run it in CI's Kamal render image (backend-ci.yml, `Kamal 2.12 render contract`), or locally
# in Kamal's own image with the repository mounted read-only:
#
#   docker run --rm -v "$PWD:/repo:ro" -w /repo --entrypoint sh \
#     ghcr.io/basecamp/kamal@sha256:7b5be276aa17bbe122887f6a1ff12865f4848111989699b9786083be95d98415 \
#     scripts/test-kamal-proxy-bind-render.sh
#
# POSIX sh on purpose: Kamal's image has no bash.
set -eu

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
render_root="$(mktemp -d)"
expected_version="0123456789abcdef0123456789abcdef01234567"

cleanup() {
	rm -rf -- "${render_root}"
}
trap cleanup EXIT INT TERM

test "$(kamal version)" = "2.12.0"
mkdir -p "${render_root}/config" "${render_root}/.kamal"
cp "${repo_root}/config/deploy.yml" "${render_root}/config/deploy.yml"

# Every secret name the config loads, each with a fake value. The proxy's TLS pair is read when
# kamal-proxy's run command is rendered.
umask 077
for key in \
	KAMAL_REGISTRY_USERNAME \
	KAMAL_REGISTRY_PASSWORD \
	CLOUDFLARE_ORIGIN_CERT \
	CLOUDFLARE_ORIGIN_KEY \
	CHANNEL_WHATSAPP_BAILEYS_SIDECAR_TOKEN \
	TS_AUTHKEY \
	POSTGRES_PASSWORD \
	PGBACKREST_REPO1_PATH \
	PGBACKREST_REPO1_S3_BUCKET \
	PGBACKREST_REPO1_S3_ENDPOINT \
	PGBACKREST_REPO1_S3_KEY \
	PGBACKREST_REPO1_S3_KEY_SECRET \
	PGBACKREST_REPO1_CIPHER_PASS \
	ADMIN_API_KEY \
	CLERK_JWT_ISSUER \
	SENTRY_DSN \
	CLERK_SECRET_KEY \
	CLERK_WEBHOOK_SIGNING_SECRET \
	COMPOSIO_API_KEY \
	DATABASE_URL \
	ENCRYPTION_KEY \
	FILE_STORE_S3_ACCESS_KEY_ID \
	FILE_STORE_S3_ENDPOINT_URL \
	FILE_STORE_S3_SECRET_ACCESS_KEY \
	LLM_API_KEY \
	MEMORY_EMBEDDING_API_KEY \
	MEMORY_EMBEDDING_BASE_URL \
	VAULT_ENCRYPTION_KEY \
	DEPLOY_API_KEY; do
	printf '%s=fake-render-value\n' "${key}" >> "${render_root}/.kamal/secrets"
done
chmod 600 "${render_root}/.kamal/secrets"

cd "${render_root}"
DEPLOY_HOST=192.0.2.10 \
	DEPLOY_IMAGE_VERSION="${expected_version}" \
	ruby - "${render_root}/config/deploy.yml" "${expected_version}" <<'RUBY'
require "kamal"
require "pathname"
require "shellwords"

config_path, expected_version = ARGV
config = Kamal::Configuration.create_from(
  config_file: Pathname.new(config_path),
  version: expected_version
)
run = Kamal::Commands::Proxy.new(config, host: config.proxy_hosts.first).run
# Kamal joins some option groups into one element (`"--publish 80:80 --publish 443:443"`), so read
# the command as the shell will: one word per argument, quotes removed.
command = Shellwords.split(run.flatten.map(&:to_s).join(" "))

def values_after(command, flag)
  command.each_cons(2).select { |pair| pair[0] == flag }.map { |pair| pair[1] }
end

published = values_after(command, "--publish")
expected_published = [ "127.0.0.1:80:80", "127.0.0.1:443:443" ]
unless published == expected_published
  $stderr.puts "published: #{published.join(' ')}"
  exit 1
end

log_drivers = values_after(command, "--log-driver")
unless log_drivers == [ "none" ]
  $stderr.puts "log driver: #{log_drivers.join(' ')}"
  exit 1
end

puts "kamal-proxy bind: #{published.map { |value| "--publish #{value}" }.join(' ')}"
RUBY
