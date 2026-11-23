#!/usr/bin/env bash
# Call the operator (X-Admin-Key) endpoints without handling the key yourself.
#
# `ADMIN_API_KEY` is a root credential — it bypasses per-account access entirely
# (admin.py:11). Putting it on a curl command line puts it in shell history, in
# `ps` output, and in any transcript of the session. This reads it the way
# uvicorn does — pydantic-settings, in-process, straight from backend/.env — and
# uses it without ever printing it. Nothing but the response reaches stdout.
#
# Usage:
#   bash scripts/uat-admin.sh GET  '/v1/admin/users?q=<email, user id or clerk id>'
#   bash scripts/uat-admin.sh POST /v1/admin/tenants/<user_id>/exports
#   bash scripts/uat-admin.sh GET  /v1/admin/tenants/<user_id>/lifecycle
#   bash scripts/uat-admin.sh POST /v1/admin/tenants/<user_id>/delete '{"reason":"uat"}'
#   bash scripts/uat-admin.sh POST /v1/admin/tenants/<user_id>/purge/cancel
#
# Shorthands:
#   whoami           confirm the key is loaded and accepted, printing neither
#   fingerprint      length + sha256[:12] of ADMIN_API_KEY and DEPLOY_API_KEY
#
# Options (after METHOD and PATH, in place of an inline body):
#   --body-file <path>   read the JSON body from a file instead of argv
#   --header K:V         send an extra header; repeatable
#
# `--body-file` exists for the same reason the admin key is never echoed: a body
# can itself be a credential. The phase-05 runtime-state upsert carries a
# companion's HMAC signing secret, and passing that as `$3` would put it in shell
# history and in `ps` output for every user on the box. A file passes a path.
#
# `--header` covers routes that require one the script cannot infer — the platform
# mutation routes take a mandatory `Idempotency-Key`, for instance:
#
#   bash scripts/uat-admin.sh PUT /v1/platform/agents/<id>/runtime-state \
#     --body-file /path/to/runtime-state.json \
#     --header "Idempotency-Key: uat11-$(date +%s)"
#
# `fingerprint` exists because the operator console asks for the admin key in the
# browser, and backend/.env holds two adjacent 64-character hex secrets that look
# identical to a human. Pasting DEPLOY_API_KEY yields "That key was not accepted"
# with no hint which of the two is wrong. Compare what you are about to paste:
#     printf %s '<pasted>' | sha256sum | cut -c1-12
# The comparison server-side is hmac.compare_digest with no normalisation, so a
# trailing newline, a stray quote, or an included `ADMIN_API_KEY=` prefix all
# fail exactly the same way as the wrong secret entirely.
#
# Env:
#   UAT_API_URL      backend base URL (default http://localhost:8000)
#
# The operator CONSOLE at /admin is a browser surface and asks for the key
# directly — this script cannot help there. Read it out of backend/.env yourself;
# it is your file. Note /admin sits behind the billing gate: a billing-locked
# browser identity sees the locked dashboard panel instead, so open /admin with
# an unlocked or exempt identity.
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
API_URL="${UAT_API_URL:-http://localhost:8000}"
BODY_FILE=""
EXTRA_HEADERS=()

if [[ $# -lt 1 || "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
	# The whole leading comment block, however long it grows — a fixed line
	# range silently truncates the help the moment the header is edited.
	awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "${BASH_SOURCE[0]}"
	exit 0
fi

case "$1" in
	# whoami and fingerprint probe a search term that matches no tenant, so even a
	# successful probe carries no tenant data.
	whoami)  METHOD=GET;  ENDPOINT=/v1/admin/users?q=uat-admin-probe; BODY=""; MODE=whoami ;;
	fingerprint) METHOD=GET; ENDPOINT=/v1/admin/users?q=uat-admin-probe; BODY=""; MODE=fingerprint ;;
	*)       METHOD="$1"; ENDPOINT="${2:?need a path, e.g. /v1/admin/users}"
	         BODY=""; MODE=raw
	         shift 2
	         # A bare third argument is still an inline body, as before.
	         if [[ $# -gt 0 && "$1" != --* ]]; then BODY="$1"; shift; fi
	         while [[ $# -gt 0 ]]; do
	           case "$1" in
	             --body-file)
	               [[ -n "$BODY" ]] && { echo "give either an inline body or --body-file, not both" >&2; exit 2; }
	               BODY_FILE="${2:?--body-file needs a path}"
	               [[ -r "$BODY_FILE" ]] || { echo "cannot read body file: $BODY_FILE" >&2; exit 2; }
	               BODY="$(cat "$BODY_FILE")"; shift 2 ;;
	             --header)
	               EXTRA_HEADERS+=("${2:?--header needs K:V}"); shift 2 ;;
	             *) echo "unknown option '$1'" >&2; exit 2 ;;
	           esac
	         done ;;
esac

cd "$REPO_ROOT/backend"
EXTRA_HEADERS_JOINED=""
if [[ ${#EXTRA_HEADERS[@]} -gt 0 ]]; then
	# Newline-separated: a header VALUE may contain anything but a newline.
	printf -v EXTRA_HEADERS_JOINED '%s\n' "${EXTRA_HEADERS[@]}"
fi

API_URL="$API_URL" METHOD="$METHOD" ENDPOINT="$ENDPOINT" BODY="$BODY" MODE="$MODE" \
	EXTRA_HEADERS="$EXTRA_HEADERS_JOINED" \
	PYTHONPATH=. uv run --quiet python - <<'PY'
import json
import os
import sys

import httpx

from app.core.config import settings

if not settings.admin_api_key:
    sys.exit("ADMIN_API_KEY is empty in backend/.env — every admin route answers 401.")

method, endpoint, body, mode = (os.environ[k] for k in ("METHOD", "ENDPOINT", "BODY", "MODE"))

if mode == "fingerprint":
    # Never the values — only enough to tell the two 64-hex secrets apart.
    import hashlib

    for label, value in (
        ("ADMIN_API_KEY", settings.admin_api_key),
        ("DEPLOY_API_KEY", settings.deploy_api_key),
    ):
        if not value:
            print(f"{label:15} <unset>")
            continue
        digest = hashlib.sha256(value.encode()).hexdigest()[:12]
        print(f"{label:15} len={len(value):<4} sha256[:12]={digest}")
    print()
    print("The operator console wants ADMIN_API_KEY. Check a paste with:")
    print("    printf %s '<pasted>' | sha256sum | cut -c1-12")
    sys.exit(0)

url = os.environ["API_URL"].rstrip("/") + endpoint

headers = {"X-Admin-Key": settings.admin_api_key}
for line in os.environ.get("EXTRA_HEADERS", "").splitlines():
    if not line.strip():
        continue
    name, separator, value = line.partition(":")
    if not separator:
        sys.exit(f"--header must be K:V, got {line!r}")
    headers[name.strip()] = value.strip()

kwargs = {"headers": headers, "timeout": 60}
if body:
    try:
        kwargs["json"] = json.loads(body)
    except json.JSONDecodeError as error:
        sys.exit(f"body is not valid JSON: {error}")

try:
    response = httpx.request(method.upper(), url, **kwargs)
except httpx.HTTPError as error:
    sys.exit(f"{type(error).__name__}: {error}")

if mode == "whoami":
    # Report only whether the key is accepted. Printing the listing here would
    # dump tenant data (emails, names, Clerk ids) to the terminal as a side
    # effect of a check.
    verdict = {
        200: "key accepted",
        401: "key REJECTED — backend/.env disagrees with the running process, or it restarted",
        403: "key rejected (forbidden)",
    }.get(response.status_code, f"unexpected HTTP {response.status_code}")
    print(f"{verdict}  [{url} -> {response.status_code}]")
    sys.exit(0 if response.status_code == 200 else 1)

if response.status_code >= 400:
    print(f"HTTP {response.status_code}", file=sys.stderr)
    print(response.text[:2000], file=sys.stderr)
    sys.exit(1)

try:
    payload = response.json()
except ValueError:
    print(response.text)
    sys.exit(0)

print(json.dumps(payload, indent=2))
PY
