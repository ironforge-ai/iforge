#!/usr/bin/env bash
# Presence-only preflight: does a hosted web build have VITE_STRIPE_PUBLISHABLE_KEY?
#
# Usage:
#   bash scripts/check-web-stripe-key.sh [WEB_DIR] [MODE]
#     WEB_DIR  the web app directory (default: <repo>/apps/web)
#     MODE     the Vite mode whose env files apply (default: development, as `vite dev`)
#
# Exit 0  the build is not hosted, or the key resolves non-empty
# Exit 1  the build is hosted (VITE_IFORGE_HOSTED=true) and the key resolves empty or absent
# Exit 2  WEB_DIR is not a directory
#
# Why (G-06.3-3): the 06.3 UAT walk started the web dev server without the key. Wallet top-ups
# are Stripe Elements only, so the payment form could not load, and nothing noticed beforehand:
# the env schema treats the key as optional because an OSS build never needs it. A hosted walk
# does, so this stops one before it starts. scripts/uat-stack.sh runs it in its preflight; run it
# yourself when you start the web server without that script.
#
# Resolution follows Vite for `vite dev`: the process environment wins (an empty value there
# still counts as set), then WEB_DIR/.env.MODE.local, .env.MODE, .env.local, .env -- the first
# file that defines the name decides, and the last definition within that file wins. So an empty
# value at a higher level hides a non-empty one below it, exactly as it does for Vite.
#
# Each line is parsed by dotenv's line rules, as Vite uses them (WR-03): a double-, single- or
# backtick-quoted value is the text inside its quotes and may carry a trailing # comment, and an
# unquoted value ends at the first #. So KEY="" # fill in is empty, not a two-character key.
#
# This script never prints the key's value -- only whether it resolved and from where.
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
WEB_DIR="${1:-$REPO_ROOT/apps/web}"
MODE="${2:-development}"
KEY_NAME="VITE_STRIPE_PUBLISHABLE_KEY"
HOSTED_NAME="VITE_IFORGE_HOSTED"

if [[ ! -d "$WEB_DIR" ]]; then
	{
		echo "usage: bash scripts/check-web-stripe-key.sh [WEB_DIR] [MODE]"
		echo "  WEB_DIR is not a directory: $WEB_DIR"
	} >&2
	exit 2
fi

trim() {
	local s="$1"
	s="${s#"${s%%[![:space:]]*}"}"
	s="${s%"${s##*[![:space:]]}"}"
	printf '%s' "$s"
}

# dotenv's LINE rule for a quoted value: an opening ", ' or `, text without that quote, the
# matching close, optional whitespace, then an optional # comment to the end of the line.
DQ_VALUE='^"([^"]*)"[[:space:]]*(#.*)?$'
SQ_VALUE="^'([^']*)'[[:space:]]*(#.*)?\$"
BQ_VALUE='^`([^`]*)`[[:space:]]*(#.*)?$'

# Set by resolve(): the value (never printed) and where it came from.
RESOLVED_VALUE=""
RESOLVED_SOURCE=""

# resolve NAME -- RESOLVED_SOURCE stays empty when no level defines NAME.
resolve() {
	local name="$1" file line value found
	RESOLVED_VALUE=""
	RESOLVED_SOURCE=""
	if [[ -n "${!name+set}" ]]; then
		RESOLVED_VALUE="${!name}"
		RESOLVED_SOURCE="process environment"
		return 0
	fi
	for file in ".env.$MODE.local" ".env.$MODE" ".env.local" ".env"; do
		[[ -f "$WEB_DIR/$file" ]] || continue
		found=false
		value=""
		while IFS= read -r line || [[ -n "$line" ]]; do
			line="${line%$'\r'}"
			line="$(trim "$line")"
			[[ -z "$line" || "$line" == \#* ]] && continue
			if [[ "$line" =~ ^export[[:space:]]+(.*)$ ]]; then
				line="${BASH_REMATCH[1]}"
			fi
			[[ "$line" =~ ^${name}[[:space:]]*=(.*)$ ]] || continue
			value="$(trim "${BASH_REMATCH[1]}")"
			if [[ "$value" =~ $DQ_VALUE || "$value" =~ $SQ_VALUE || "$value" =~ $BQ_VALUE ]]; then
				# A quoted value is the text inside its quotes; a trailing comment is dropped.
				value="${BASH_REMATCH[1]}"
			else
				# An unquoted value ends at the first #.
				value="$(trim "${value%%#*}")"
			fi
			found=true
		done < "$WEB_DIR/$file"
		if [[ "$found" == true ]]; then
			RESOLVED_VALUE="$value"
			RESOLVED_SOURCE="$file"
			return 0
		fi
	done
	return 0
}

resolve "$HOSTED_NAME"
if [[ "$(trim "$RESOLVED_VALUE")" != "true" ]]; then
	echo "$HOSTED_NAME is not true; $KEY_NAME not required"
	exit 0
fi

resolve "$KEY_NAME"
if [[ -n "$(trim "$RESOLVED_VALUE")" ]]; then
	echo "✓ $KEY_NAME present (source: $RESOLVED_SOURCE)"
	exit 0
fi

{
	echo "✗ $KEY_NAME is not set for this hosted web build"
	echo "  Set it in apps/web/.env.local. For a staging walk use the staging test-mode publishable key (pk_test_…) from the same Stripe account as the staging hosted service."
	echo "  Against the local mock any pk_test_ placeholder satisfies this check (the mock opens no Stripe session for a top-up)."
} >&2
exit 1
