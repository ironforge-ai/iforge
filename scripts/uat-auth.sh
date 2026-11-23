#!/usr/bin/env bash
# Mint the test identity every Phase 03 gate test authenticates as.
#
# `GET /v1/access/status` runs on `require_clerk_claims`, which, outside the dev
# bypass, verifies a real RS256 JWT against CLERK_PEM_PUBLIC_KEY — so a UAT
# identity is a keypair plus a signed token, not a bearer string.
# `GET /v1/auth/me` provisions a fresh sub on first touch. This script owns that
# whole setup.
#
# Usage:
#   JWT=$(bash scripts/uat-auth.sh)                    # sub=uat_tester_1
#   JWT=$(bash scripts/uat-auth.sh --sub uat_tester_4a)  # a FRESH, unprovisioned user
#   bash scripts/uat-auth.sh --write-env               # also fix backend/.env, then restart uvicorn
#   bash scripts/uat-auth.sh --check                   # mint + probe the running backend
#
# Options:
#   --sub NAME      identity to mint (default uat_tester_1). A NEW sub is a NEW,
#                   unprovisioned user — that is how tests 3a/4/5 get a fresh one.
#   --email ADDR    email claim (default <sub>@uat.local). auth.py:733 reads it
#                   off the JWT and user_provisioning stores it, so this is what
#                   the operator console's tenant picker searches — it matches on
#                   email ILIKE and nothing else, so an identity minted without
#                   one is invisible in the console no matter how it is spelled.
#                   --email "" mints a token with no email claim.
#   --ttl SECONDS   token lifetime (default 86400)
#   --issuer URL    iss claim; defaults to CLERK_JWT_ISSUER from backend/.env,
#                   else https://uat.local. MUST match or auth.py:528 rejects it.
#   --key PATH      private key (default /tmp/uat.pem); generated if absent
#   --write-env     write CLERK_PEM_PUBLIC_KEY / CLERK_JWT_ISSUER into backend/.env
#   --check         probe GET /v1/access/status with the minted token
#   --quiet         suppress the human summary (stderr) — stdout is the token either way
#
# stdout is ONLY the token. Every message goes to stderr, so command substitution
# is always safe.
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$REPO_ROOT/backend/.env"

SUB="uat_tester_1"
EMAIL=""
EMAIL_SET=false
TTL=86400
ISSUER=""
KEY_PATH="/tmp/uat.pem"
WRITE_ENV=false
CHECK=false
QUIET=false
API_URL="${UAT_API_URL:-http://localhost:8000}"

while [[ $# -gt 0 ]]; do
	case "$1" in
		--sub) SUB="${2:?--sub needs a value}"; shift 2 ;;
		--email) EMAIL="${2-}"; EMAIL_SET=true; shift 2 ;;
		--ttl) TTL="${2:?--ttl needs a value}"; shift 2 ;;
		--issuer) ISSUER="${2:?--issuer needs a value}"; shift 2 ;;
		--key) KEY_PATH="${2:?--key needs a value}"; shift 2 ;;
		--write-env) WRITE_ENV=true; shift ;;
		--check) CHECK=true; shift ;;
		--quiet) QUIET=true; shift ;;
		-h|--help) sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
		*) echo "unknown argument: $1 (try --help)" >&2; exit 2 ;;
	esac
done

say()  { [[ "$QUIET" == true ]] || printf '%s\n' "$*" >&2; }
red()  { printf '\033[31m%s\033[0m\n' "$*" >&2; }
ylw()  { printf '\033[33m%s\033[0m\n' "$*" >&2; }
grn()  { [[ "$QUIET" == true ]] || printf '\033[32m%s\033[0m\n' "$*" >&2; }

command -v openssl >/dev/null || { red "✗ openssl not on PATH"; exit 1; }
command -v uv >/dev/null      || { red "✗ uv not on PATH (needed to run PyJWT from backend/)"; exit 1; }

# ------------------------------------------------------------------ env values

# Reads one KEY from backend/.env, stripping surrounding quotes. Empty when the
# file or the key is absent.
env_value() {
	local key="$1" line
	[[ -f "$ENV_FILE" ]] || return 0
	line="$(grep -E "^${key}=" "$ENV_FILE" | tail -1 || true)"
	line="${line#*=}"
	line="${line%\"}"; line="${line#\"}"
	line="${line%\'}"; line="${line#\'}"
	printf '%s' "$line"
}

# Real Clerk identities always carry an email; only these locally minted ones can
# lack it, so defaulting keeps the fixture faithful to what the product sees.
if [[ "$EMAIL_SET" == false ]]; then
	EMAIL="${SUB}@uat.local"
fi

if [[ -z "$ISSUER" ]]; then
	ISSUER="$(env_value CLERK_JWT_ISSUER)"
	ISSUER="${ISSUER:-https://uat.local}"
fi

# ------------------------------------------------------------------ keypair

if [[ ! -f "$KEY_PATH" ]]; then
	say "▶ generating keypair at $KEY_PATH"
	openssl genrsa -out "$KEY_PATH" 2048 2>/dev/null
	chmod 600 "$KEY_PATH"
fi
PUB_PATH="${KEY_PATH%.pem}.pub"
# Always re-derive: a stale .pub next to a regenerated .pem mints tokens the
# backend cannot verify, and the 401 that produces looks exactly like the gate
# working correctly.
openssl rsa -in "$KEY_PATH" -pubout -out "$PUB_PATH" 2>/dev/null

# macOS base64 has no -w0; `tr -d` is the portable single-line form.
PUB_B64="$(base64 < "$PUB_PATH" | tr -d '\n')"

# ------------------------------------------------------------------ backend/.env

CONFIGURED_PUB="$(env_value CLERK_PEM_PUBLIC_KEY)"
ENV_WRITTEN=false

if [[ "$WRITE_ENV" == true ]]; then
	if [[ ! -f "$ENV_FILE" ]]; then
		red "✗ $ENV_FILE does not exist — create it first (03-UAT.md step 2)"
		exit 1
	fi
	if [[ "$CONFIGURED_PUB" == "$PUB_B64" && "$(env_value CLERK_JWT_ISSUER)" == "$ISSUER" ]]; then
		grn "✓ backend/.env already matches this keypair and issuer"
	else
		backup="$ENV_FILE.bak.$(date +%s)"
		cp "$ENV_FILE" "$backup"
		PUB_B64="$PUB_B64" ISSUER="$ISSUER" ENV_FILE="$ENV_FILE" python3 - <<'PY'
import os, re
path = os.environ["ENV_FILE"]
values = {
    "CLERK_PEM_PUBLIC_KEY": os.environ["PUB_B64"],
    "CLERK_JWT_ISSUER": os.environ["ISSUER"],
}
lines = open(path).read().splitlines()
for key, value in values.items():
    pattern = re.compile(rf"^{key}=")
    for i, line in enumerate(lines):
        if pattern.match(line):
            lines[i] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
open(path, "w").write("\n".join(lines) + "\n")
PY
		ENV_WRITTEN=true
		grn "✓ backend/.env updated (backup: ${backup#"$REPO_ROOT"/})"
	fi
elif [[ "$CONFIGURED_PUB" != "$PUB_B64" ]]; then
	ylw "⚠ backend/.env CLERK_PEM_PUBLIC_KEY does not match $KEY_PATH."
	ylw "  Tokens minted here will 401 — which is indistinguishable from the gate"
	ylw "  working correctly, so fix it before recording anything:"
	{
		echo "      CLERK_JWT_ISSUER=$ISSUER"
		echo "      CLERK_PEM_PUBLIC_KEY=$PUB_B64"
		echo "  or re-run with --write-env. Restart uvicorn either way."
	} >&2
fi

# ------------------------------------------------------------------ mint

TOKEN="$(cd "$REPO_ROOT/backend" && SUB="$SUB" ISSUER="$ISSUER" TTL="$TTL" KEY_PATH="$KEY_PATH" \
	EMAIL="$EMAIL" uv run --quiet python -c "
import jwt, os, time
claims = {
    'sub': os.environ['SUB'],
    'iss': os.environ['ISSUER'],
    'exp': int(time.time()) + int(os.environ['TTL']),
}
if os.environ.get('EMAIL'):
    claims['email'] = os.environ['EMAIL']
print(jwt.encode(
    claims,
    open(os.environ['KEY_PATH']).read(),
    algorithm='RS256',
))")"

# ------------------------------------------------------------------ probe

if [[ "$CHECK" == true ]]; then
	# `-w %{http_code}` already prints 000 when the connection never lands, and
	# curl still exits non-zero — an `|| echo 000` on top of that concatenates to
	# "000000" and falls through to the unexpected-status branch.
	code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 \
		"$API_URL/v1/access/status" -H "Authorization: Bearer $TOKEN" 2>/dev/null || true)"
	code="${code:-000}"
	case "$code" in
		200)
			state="$(curl -sS --max-time 5 "$API_URL/v1/access/status" \
				-H "Authorization: Bearer $TOKEN" 2>/dev/null || true)"
			grn "✓ backend accepts this identity → $state"
			;;
		401)
			red "✗ 401 — the backend does not verify this token."
			red "  Either CLERK_PEM_PUBLIC_KEY does not match $KEY_PATH, or uvicorn is"
			red "  still running with the old key. Fix the key, RESTART uvicorn, re-probe."
			;;
		503)
			red "✗ 503 — Clerk is not configured; CLERK_PEM_PUBLIC_KEY is unset."
			red "  The gate fails open on transport error, so this renders as a fully"
			red "  working dashboard. Do NOT record tests 3/4/5/52 in this state."
			;;
		000) red "✗ no answer from $API_URL — is the backend up?" ;;
		*)   ylw "⚠ unexpected $code from $API_URL/v1/access/status" ;;
	esac
fi

# ------------------------------------------------------------------ summary

say ""
say "  identity   sub=$SUB  iss=$ISSUER  ttl=${TTL}s"
say "  email      ${EMAIL:-<none — invisible to the console tenant picker>}"
say "  key        $KEY_PATH"
if [[ "$ENV_WRITTEN" == true ]]; then
	say ""
	ylw "  backend/.env changed — RESTART uvicorn before the token will verify."
fi
say ""
say "  Bring the stack up carrying this identity in the BROWSER too:"
say "      UAT_JWT=\"\$JWT\" bash scripts/uat-stack.sh --host 0.0.0.0"
say ""
say "  Probe as this identity (never 'Bearer dev-bypass' — different principal):"
say "      curl -sS $API_URL/v1/access/status -H \"Authorization: Bearer \$JWT\" | jq"
say ""

printf '%s\n' "$TOKEN"
