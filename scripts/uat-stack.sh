#!/usr/bin/env bash
# Bring up every service the Phase 03 UAT needs, in dependency order.
#
# Starts, waits for, and health-checks:
#   1. Postgres          docker compose, 127.0.0.1:5433
#   2. Alembic migrate   backend/, `alembic upgrade head`
#   3. cloud-api         backend/, uvicorn on :8000
#   4. mock deploy API   backend/, `python -m scripts.mock_deploy_api` on :8001
#   5. worker tier       backend/, `python -m app.workers.channels`, health :8002
#                        (tenant export + purge consumers — without it an export
#                         never leaves `queued`)
#   6. web dashboard     apps/web/, `bun run dev` on :3000 (--web-port to move it)
#
# Usage:
#   bash scripts/uat-stack.sh                 # all services, bound to loopback
#   bash scripts/uat-stack.sh --host 0.0.0.0  # bind wide (browser on another machine)
#   bash scripts/uat-stack.sh --advertise-host myhost.example.ts.net
#                                             # pin the host the BROWSER resolves
#   bash scripts/uat-stack.sh --billing-gate on \
#       --deploy-api-url http://localhost:8001 --deploy-api-key uat-local
#                                             # 06.3 new-user path — a fresh identity
#                                             # lands on the locked dashboard
#                                             # (billing_required) and pays from
#                                             # Settings → Compute.
#                                             # The key must be non-empty (see below);
#                                             # the mock never checks its value.
#   bash scripts/uat-stack.sh --billing-gate off
#                                             # a fresh identity is provisioned
#                                             # straight into the dashboard
#   bash scripts/uat-stack.sh --fresh-db      # wipe the pg volume first (test 1, Cold Start)
#   bash scripts/uat-stack.sh --no-web        # services only, run the web app yourself
#   bash scripts/uat-stack.sh --stop          # stop everything this script started
#
# GATE FLAGS
#   --billing-gate / --deploy-api-url / --deploy-api-key export the matching
#   setting for this run only. Real env outranks backend/.env in pydantic-settings,
#   so the file is never edited and nothing leaks into the next run. This exists
#   because the flags decide WHICH path a walk is actually performing and a wrong
#   setting is silent: with BILLING_GATE_ENABLED=false a fresh identity is
#   provisioned straight into the dashboard, so a walk meant to exercise the
#   locked dashboard and the Choose a plan panel reads as a product failure when
#   the product is correct and the flag is wrong. Walking both paths needs the
#   flag flipped and flipped back, which is what these are for.
#   The "Gate configuration" step below reports the values THIS run will use,
#   read from the app's own settings object rather than from the file.
#
# REMOTE BROWSERS (LAN or Tailscale)
#   Binding wide is only half of it. Three more things have to name a host the
#   browser can actually resolve, and each fails in a way that reads as a product
#   defect rather than a networking one:
#     * VITE_IFORGE_API_URL / VITE_IFORGE_DEPLOY_API_URL are baked into the page.
#       Left at localhost, a remote browser calls ITS OWN machine and every request
#       fails — which the access gate renders as a working dashboard, because the
#       gate fails open on transport error.
#     * CORS_ORIGINS must carry the origin the browser sends. Otherwise the calls
#       leave and the answers get dropped by the browser, with nothing in the
#       backend log to show for it.
#     * Vite's own host check blocks DNS names it does not know (bare IPs are fine),
#       so a MagicDNS name serves "Blocked request" and looks like a dead server.
#   This script sets all three from the advertised host. With --host 0.0.0.0 it
#   advertises the Tailscale IP when tailscaled is up (reachable from the LAN too),
#   otherwise the first LAN address. --advertise-host overrides.
#
# Ctrl-C stops every child it started. Logs land in .uat-logs/.
#
# This script deliberately does NOT create .env files — it reports precisely
# what is missing and exits. A UAT run against a half-configured backend is
# worse than no run at all: the Phase 03 access gate fails open on transport
# error, so a misconfigured backend renders as a fully working dashboard and
# reads as a PASS when nothing was exercised. See 03-UAT.md's DEV_AUTH_BYPASS
# warning.
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$REPO_ROOT/.uat-logs"
PID_FILE="$LOG_DIR/pids"

red()  { printf '\033[31m%s\033[0m\n' "$*"; }
grn()  { printf '\033[32m%s\033[0m\n' "$*"; }
ylw()  { printf '\033[33m%s\033[0m\n' "$*"; }
step() { printf '\n\033[1m▶ %s\033[0m\n' "$*"; }

BIND_HOST="127.0.0.1"
ADVERTISE_HOST=""
WEB_PORT=3000
FRESH_DB=false
START_WEB=true
STOP_ONLY=false
GATE_OVERRIDES=()

# `on`/`off` -> the True/False pydantic-settings parses out of the environment.
# Answers through a global rather than stdout: inside `$(...)` the rejection
# branch's `exit` would only kill the subshell, leaving a typo'd flag to export
# an empty string — which pydantic reads as false. A flag that silently means
# "off" is the exact failure this whole step exists to make impossible.
GATE_BOOL=""
gate_bool() {
	case "${1,,}" in
		on|true|1|yes)  GATE_BOOL=true ;;
		off|false|0|no) GATE_BOOL=false ;;
		*) red "✗ $2 takes on|off, got '$1'"; exit 2 ;;
	esac
}

# How the backend will verify Clerk tokens: pem | jwks | none. Mirrors
# backend/app/core/auth.py _resolve_clerk_signing_key — the PEM wins; otherwise
# the JWKS client built from CLERK_JWT_ISSUER; with neither, the gate 503s.
# $1 is the settings' bool(pem) ("True"/"False"), $2 the issuer string.
clerk_verification_mode() {
	if [[ "$1" == True ]]; then
		echo pem
	elif [[ "$2" == *[![:space:]]* ]]; then
		echo jwks
	else
		echo none
	fi
}

# 06.3 WR-02: the Access-gate probe cannot exercise JWKS. Its `Bearer uat-probe`
# fails to decode before PyJWKClient fetches anything, so the backend answers 401
# for a typo'd issuer, another Clerk instance's issuer, or an unreachable JWKS
# endpoint exactly as it does for a good one. Fetch the document the backend
# will fetch (auth.py: "<issuer>/.well-known/jwks.json") and require a "keys"
# member. The body is captured before matching: under pipefail, `curl | grep -q`
# can fail on curl's SIGPIPE after a match. $1 is the issuer. Answers through
# CLERK_JWKS_OK (true/false) and one printed line; never exits.
CLERK_JWKS_OK=""
clerk_jwks_probe() {
	local url="${1%/}/.well-known/jwks.json"
	local body=""
	body="$(curl -fsS --max-time 5 "$url" 2>/dev/null)" || body=""
	if [[ "$body" == *'"keys"'* ]]; then
		CLERK_JWKS_OK=true
		grn "✓ Clerk JWKS reachable at $url"
	else
		CLERK_JWKS_OK=false
		red "✗ Clerk JWKS not reachable at $url (or it has no \"keys\")."
		red "  Real session tokens will get 503 or 401 and the dashboard gate fails open."
		red "  Check CLERK_JWT_ISSUER in backend/.env. Do NOT record any gate test."
	fi
}

while [[ $# -gt 0 ]]; do
	case "$1" in
		--host) BIND_HOST="${2:?--host needs a value}"; shift 2 ;;
		--advertise-host) ADVERTISE_HOST="${2:?--advertise-host needs a value}"; shift 2 ;;
		--web-port) WEB_PORT="${2:?--web-port needs a value}"; shift 2 ;;
		--billing-gate)
			gate_bool "${2:?--billing-gate needs on|off}" --billing-gate
			export BILLING_GATE_ENABLED="$GATE_BOOL"
			GATE_OVERRIDES+=("BILLING_GATE_ENABLED=$BILLING_GATE_ENABLED"); shift 2 ;;
		--deploy-api-url)
			export DEPLOY_API_URL="${2:?--deploy-api-url needs a value}"
			GATE_OVERRIDES+=("DEPLOY_API_URL=$DEPLOY_API_URL"); shift 2 ;;
		--deploy-api-key)
			export DEPLOY_API_KEY="${2:?--deploy-api-key needs a value}"
			GATE_OVERRIDES+=("DEPLOY_API_KEY=<set>"); shift 2 ;;
		--fresh-db) FRESH_DB=true; shift ;;
		--no-web) START_WEB=false; shift ;;
		--stop) STOP_ONLY=true; shift ;;
		-h|--help) sed -n '2,61p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
		*) echo "unknown argument: $1 (try --help)" >&2; exit 2 ;;
	esac
done

# Loopback is right for a browser on this host; a wide bind needs a routable
# address in the URLs the BROWSER resolves, which is not "0.0.0.0".
#
# Tailscale first, LAN second: a tailnet address is reachable from both the LAN
# and off-site, so preferring it never costs reachability, while the LAN address
# silently dead-ends for a browser that is not in the house. Detection is
# `tailscale ip -4`; `hostname -I` also lists the 100.x address but its position
# in that list is not stable, so it cannot be picked out positionally.
ADVERTISE_SOURCE="explicit --advertise-host"
if [[ -z "$ADVERTISE_HOST" ]]; then
	if [[ "$BIND_HOST" == "0.0.0.0" || "$BIND_HOST" == "::" ]]; then
		ADVERTISE_HOST="$(tailscale ip -4 2>/dev/null | head -1 || true)"
		if [[ -n "$ADVERTISE_HOST" ]]; then
			ADVERTISE_SOURCE="tailscale ip -4"
		else
			ADVERTISE_HOST="$(hostname -I 2>/dev/null | awk '{print $1}')"
			ADVERTISE_HOST="${ADVERTISE_HOST:-$(hostname)}"
			ADVERTISE_SOURCE="first LAN address (no tailscale)"
		fi
	else
		ADVERTISE_HOST="localhost"
		ADVERTISE_SOURCE="loopback bind"
	fi
fi

WEB_ORIGIN_URL="http://${ADVERTISE_HOST}:${WEB_PORT}"
API_URL_ADV="http://${ADVERTISE_HOST}:8000"
DEPLOY_URL_ADV="http://${ADVERTISE_HOST}:8001"

# `uv run` and `bun run` are wrappers: the recorded pid is the launcher, and the
# server that actually holds the port is its child. Killing only the recorded pid
# reaps the wrapper and orphans the listener, so the next run finds the port taken
# and (before --strictPort) silently moved to another one. Signal the process
# group, then verify the ports are really free rather than trusting the kill.
# Pids this launcher started, held in memory. The EXIT trap reaps THESE, never
# the pid file: `--stop` from another shell makes this process's `wait` return
# and fires the trap, and by then a freshly started launcher may already have
# rewritten the shared file — the departing run would kill the arriving one's
# services and leave a stack that looks started but is not.
OWN_PIDS=()
OWN_PORTS=()

SHUTDOWN_DONE=false

# The trap's whole body, banner included. Subshells inherit the EXIT trap, so a
# `$( )` or `( )` ending after the trap is installed runs it again — five
# "Shutting down" banners for one exit, and five attempts to signal process
# groups. Teardown must happen exactly once, from the shell that owns the
# children, so the guard has to sit outside the printing, not inside the kill.
on_exit() {
	[[ "$BASHPID" != "$$" ]] && return 0
	[[ "$SHUTDOWN_DONE" == true ]] && return 0
	SHUTDOWN_DONE=true
	echo
	step "Shutting down"
	shutdown_own
	grn "✓ stopped"
}

shutdown_own() {
	local pid pgid
	for pid in ${OWN_PIDS+"${OWN_PIDS[@]}"}; do
		kill -0 "$pid" 2>/dev/null || continue
		pgid="$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ')"
		if [[ -n "$pgid" ]]; then
			kill -TERM -- "-$pgid" 2>/dev/null || kill "$pid" 2>/dev/null || true
		else
			kill "$pid" 2>/dev/null || true
		fi
	done
	# Only drop the shared file if it is still describing THIS run.
	if [[ -f "$PID_FILE" && "$(head -1 "$PID_FILE" 2>/dev/null | cut -d" " -f1)" == "${OWN_PIDS[0]:-}" ]]; then
		rm -f "$PID_FILE"
	fi
}

stop_stack() {
	local started_ports=()
	if [[ -f "$PID_FILE" ]]; then
		while read -r pid name port; do
			[[ -z "${pid:-}" ]] && continue
			[[ -n "${port:-}" ]] && started_ports+=("$port")
			if kill -0 "$pid" 2>/dev/null; then
				echo "  stopping $name (pid $pid)"
				# Negative pid = the whole group, catching the real server child.
				kill -TERM -- "-$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ')" 2>/dev/null \
					|| kill "$pid" 2>/dev/null || true
			fi
		done < "$PID_FILE"
		rm -f "$PID_FILE"
	fi

	# Give listeners a moment to release, then report anything still holding on.
	# Only ports THIS stack started are checked — they are recorded in the pid
	# file rather than assumed, because --stop may run from a shell that never
	# saw --web-port, and warning about a port we never bound (someone else's
	# service on the default) sends the reader hunting a problem we invented.
	sleep 2
	local stubborn=()
	local port
	for port in "${started_ports[@]}"; do
		(exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null && { exec 3<&- 3>&-; stubborn+=("$port"); }
	done
	if ((${#stubborn[@]})); then
		local joined
		joined="$(IFS='|'; echo "${stubborn[*]}")"
		ylw "⚠ still listening after stop: ${stubborn[*]}"
		ylw "  Check what still owns them before killing anything:"
		ylw "      ss -ltnp | grep -E ':($joined) '"
	fi
}

# Only the foreground run traps; --stop does its work and exits.
if [[ "$STOP_ONLY" == true ]]; then
	step "Stopping UAT stack"
	stop_stack
	echo "  (Postgres left running — 'docker compose down' to stop it too)"
	grn "✓ stopped"
	exit 0
fi

trap on_exit EXIT INT TERM

mkdir -p "$LOG_DIR"
: > "$PID_FILE"

# ---------------------------------------------------------------- preflight

step "Preflight"

missing=()
for tool in docker uv bun; do
	command -v "$tool" >/dev/null 2>&1 || missing+=("$tool")
done
if ((${#missing[@]})); then
	red "✗ missing required tools: ${missing[*]}"
	echo "  Need: docker (+ compose plugin), uv, bun 1.3.14+"
	exit 1
fi
docker compose version >/dev/null 2>&1 || { red "✗ 'docker compose' plugin unavailable"; exit 1; }

# Config files. Absent .env is the single most common cause of a UAT run that
# looks green and tested nothing, so this is fatal rather than a warning.
config_errors=0
if [[ ! -f "$REPO_ROOT/backend/.env" ]]; then
	red "✗ backend/.env missing"
	echo "     cp backend/.env.example backend/.env   # then fill it per 03-UAT.md step 2"
	config_errors=1
fi
if [[ "$START_WEB" == true && ! -f "$REPO_ROOT/apps/web/.env" ]]; then
	red "✗ apps/web/.env missing"
	echo "     see 03-UAT.md step 3 — VITE_IFORGE_API_URL, VITE_IFORGE_DEPLOY_API_URL, VITE_IFORGE_HOSTED"
	config_errors=1
fi
((config_errors)) && exit 1

# Point-at-the-right-port checks. Getting these backwards sends the access-gate
# probe to the mock, which 404s; 03-12 classifies any non-2xx as `refused`, so
# the gate hard-blocks on what is actually a config typo.
if [[ "$START_WEB" == true ]]; then
	web_env="$REPO_ROOT/apps/web/.env"
	api_url="$(grep -E '^VITE_IFORGE_API_URL=' "$web_env" | tail -1 | cut -d= -f2- || true)"
	deploy_url="$(grep -E '^VITE_IFORGE_DEPLOY_API_URL=' "$web_env" | tail -1 | cut -d= -f2- || true)"
	[[ "$api_url" == *:8000* ]]    || ylw "⚠ VITE_IFORGE_API_URL is '${api_url:-unset}' — expected the backend on :8000"
	[[ "$deploy_url" == *:8001* ]] || ylw "⚠ VITE_IFORGE_DEPLOY_API_URL is '${deploy_url:-unset}' — expected the mock on :8001"
	grep -qE '^VITE_IFORGE_HOSTED=true' "$web_env" \
		|| ylw "⚠ VITE_IFORGE_HOSTED is not true — /deploy and the hosted tiles will not mount (UAT tests 4, 5, 54)"
	# G-06.3-3: a hosted walk without the Stripe publishable key cannot load the payment form
	# (top-ups are Elements only). Presence only, never the value. Run the helper standalone when
	# you start the web server without this script.
	bash "$REPO_ROOT/scripts/check-web-stripe-key.sh" "$REPO_ROOT/apps/web" \
		|| { red "✗ hosted web build is missing VITE_STRIPE_PUBLISHABLE_KEY — see the message above"; exit 1; }
fi
grn "✓ tooling and config present"

# Ports must be OURS before anything starts. Vite does not fail on a taken port,
# it silently picks the next one — so an unrelated container on :3000 sends the
# dashboard to :3001 while every :3000 health check below is answered by that
# other service and reports "✓ web dashboard up". The run then looks green while
# the browser is pointed at a different product entirely; and if you find :3001
# by hand, its origin is absent from CORS_ORIGINS, so every API call is dropped
# by the browser — which the access gate's fail-open renders as a fully working
# dashboard. That is a false PASS on tests 3, 4 and 5 at once.
port_in_use() {
	(exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null || return 1
	exec 3<&- 3>&-
	return 0
}

step "Port availability"
wanted_ports=(8000 8001 8002)
[[ "$START_WEB" == true ]] && wanted_ports+=("$WEB_PORT")
taken=()
for port in "${wanted_ports[@]}"; do
	port_in_use "$port" && taken+=("$port")
done
if ((${#taken[@]})); then
	red "✗ already in use: ${taken[*]}"
	echo "  This stack needs 8000 (cloud-api), 8001 (mock deploy), 8002 (workers)$( [[ "$START_WEB" == true ]] && echo ", $WEB_PORT (web)" ) to itself."
	echo "  --web-port N moves the dashboard if the conflict is not yours to stop."
	echo "  Find the owner, then stop it:"
	echo "      ss -ltnp | grep -E ':(${taken[0]}$(printf '|%s' "${taken[@]:1}")) '"
	echo "      docker ps --format '{{.Names}}\\t{{.Ports}}'   # a published container port is a common cause"
	exit 1
fi
grn "✓ ${wanted_ports[*]} free"

# A pid file naming live processes means another launcher owns this stack. Two
# concurrent runs share the pid file, the log files and the ports, and the loser
# is whichever one the other's shutdown reaches first.
if [[ -f "$PID_FILE" ]]; then
	live=()
	while read -r pid name _; do
		[[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null && live+=("$name($pid)")
	done < "$PID_FILE"
	if ((${#live[@]})); then
		red "✗ another uat-stack run is already live: ${live[*]}"
		echo "  Stop it first:  bash scripts/uat-stack.sh --stop"
		exit 1
	fi
fi

# ------------------------------------------------------- remote-browser wiring

# Everything below only matters when the browser is not on this host. Vite copies
# VITE_-prefixed process env into the page and lets it win over apps/web/.env;
# pydantic-settings likewise ranks real env above backend/.env. So the advertised
# host can be applied per-run without editing either file — which also means a
# plain `bun run dev` still behaves exactly as the committed .env says.
if [[ "$ADVERTISE_HOST" != "localhost" && "$ADVERTISE_HOST" != "127.0.0.1" ]]; then
	step "Remote-browser wiring"
	echo "  advertising ${ADVERTISE_HOST}  (${ADVERTISE_SOURCE})"

	# The origin list REPLACES backend/.env's CORS_ORIGINS for this run, so it has
	# to re-state the local origins rather than only add the remote one. Extra
	# entries go in UAT_CORS_EXTRA_ORIGINS (comma-separated).
	cors_hosts=("localhost" "127.0.0.1" "$ADVERTISE_HOST")
	# A MagicDNS name and the tailnet IP are different Origin headers; a browser
	# that follows a ts.net link sends the name, so allow both.
	ts_dns="$(tailscale status --json 2>/dev/null | jq -r '.Self.DNSName // empty' 2>/dev/null | sed 's/\.$//' || true)"
	[[ -n "$ts_dns" ]] && cors_hosts+=("$ts_dns")
	lan_ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
	[[ -n "$lan_ip" ]] && cors_hosts+=("$lan_ip")

	cors_json=""
	for h in "${cors_hosts[@]}"; do
		case ",$cors_json," in *"\"http://$h:${WEB_PORT}\""*) continue ;; esac
		cors_json+="${cors_json:+,}\"http://$h:${WEB_PORT}\""
	done
	IFS=',' read -r -a extra_origins <<< "${UAT_CORS_EXTRA_ORIGINS:-}" || true
	for origin in "${extra_origins[@]}"; do
		origin="${origin// /}"
		[[ -z "$origin" ]] && continue
		cors_json+=",\"$origin\""
	done
	export CORS_ORIGINS="[$cors_json]"
	export WEB_ORIGIN="$WEB_ORIGIN_URL"
	export PUBLIC_API_URL="$API_URL_ADV"
	echo "  CORS_ORIGINS   $CORS_ORIGINS"
	echo "  WEB_ORIGIN     $WEB_ORIGIN"

	if [[ "$START_WEB" == true ]]; then
		export VITE_IFORGE_API_URL="${VITE_IFORGE_API_URL:-$API_URL_ADV}"
		export VITE_IFORGE_DEPLOY_API_URL="${VITE_IFORGE_DEPLOY_API_URL:-$DEPLOY_URL_ADV}"
		# Bare IPs pass Vite's host check unconditionally; DNS names do not, and a
		# blocked name serves a "Blocked request" page that reads as a dead server.
		export UAT_ALLOWED_HOSTS="${UAT_ALLOWED_HOSTS:-${ADVERTISE_HOST}${ts_dns:+,$ts_dns},localhost}"
		echo "  browser → API  $VITE_IFORGE_API_URL"
		echo "  browser → mock $VITE_IFORGE_DEPLOY_API_URL"
		echo "  vite hosts     $UAT_ALLOWED_HOSTS"
	fi

	if [[ "$BIND_HOST" == "127.0.0.1" || "$BIND_HOST" == "localhost" ]]; then
		ylw "⚠ advertising ${ADVERTISE_HOST} but binding to ${BIND_HOST} — nothing off-host"
		ylw "  can connect. Add --host 0.0.0.0."
	fi
fi

# ------------------------------------------------------------- gate config

# The gate flags decide WHICH path you are walking, and both failure modes are
# silent. With BILLING_GATE_ENABLED=false a fresh identity is provisioned
# straight into the dashboard — so a walk meant to land on the locked dashboard
# and pay from Settings → Compute reads as a FAIL ("no Choose a plan panel") when
# the product is fine and the flag is wrong. An empty DEPLOY_API_URL breaks the
# billing gate's only upstream, which the gate turns into a 503
# `billing_unavailable`.
#
# Read through the app's own settings object rather than grepping the file: that
# is the value uvicorn will actually load, so a stale export or a quoted value
# cannot make the report disagree with the process. Secrets are reported as
# set/unset only, never printed.
step "Gate configuration"
gate_report="$(cd "$REPO_ROOT/backend" && PYTHONPATH=. uv run --quiet python -c "
from app.core.config import settings
print(f'billing={settings.billing_gate_enabled}')
print(f'deploy_url={settings.deploy_api_url}')
print(f'issuer={settings.clerk_jwt_issuer}')
print(f'pem={bool(settings.clerk_pem_public_key)}')
print(f'admin={bool(settings.admin_api_key)}')
print(f'deploy_key={bool(settings.deploy_api_key)}')
" 2>/dev/null || true)"

if [[ -z "$gate_report" ]]; then
	ylw "⚠ could not load backend settings — skipping the gate-flag report"
else
	g_billing="$(sed -n 's/^billing=//p' <<< "$gate_report")"
	g_deploy="$(sed -n 's/^deploy_url=//p' <<< "$gate_report")"
	g_issuer="$(sed -n 's/^issuer=//p' <<< "$gate_report")"
	g_pem="$(sed -n 's/^pem=//p' <<< "$gate_report")"
	g_admin="$(sed -n 's/^admin=//p' <<< "$gate_report")"
	g_deploy_key="$(sed -n 's/^deploy_key=//p' <<< "$gate_report")"
	g_clerk_mode="$(clerk_verification_mode "$g_pem" "$g_issuer")"
	case "$g_clerk_mode" in
		pem)  g_clerk_label="PEM (CLERK_PEM_PUBLIC_KEY)" ;;
		jwks) g_clerk_label="JWKS (CLERK_JWT_ISSUER)" ;;
		*)    g_clerk_label="NONE" ;;
	esac

	if ((${#GATE_OVERRIDES[@]})); then
		ylw "  overridden for this run only (backend/.env untouched):"
		for override in "${GATE_OVERRIDES[@]}"; do echo "    $override"; done
	fi
	echo "  BILLING_GATE_ENABLED  $g_billing"
	echo "  DEPLOY_API_URL        ${g_deploy:-<empty>}"
	echo "  CLERK_JWT_ISSUER      ${g_issuer:-<empty>}"
	# Lowercase "unset": an empty PEM is normal when the JWKS verifies tokens.
	echo "  CLERK_PEM_PUBLIC_KEY  $([[ "$g_pem" == True ]] && echo set || echo unset)"
	echo "  Clerk verification    $g_clerk_label"
	echo "  ADMIN_API_KEY         $([[ "$g_admin" == True ]] && echo set || echo unset)"
	echo "  DEPLOY_API_KEY        $([[ "$g_deploy_key" == True ]] && echo set || echo UNSET)"

	if [[ "$g_clerk_mode" == none ]]; then
		red "✗ Clerk token verification is not configured — set CLERK_JWT_ISSUER (verifies"
		red "  through the Clerk JWKS) or CLERK_PEM_PUBLIC_KEY in backend/.env. The gate"
		red "  endpoints answer 503 while the dashboard renders as healthy. Do NOT record"
		red "  any gate test in this state."
	fi
	if [[ "$g_billing" != True ]]; then
		ylw "⚠ BILLING_GATE_ENABLED=false — a fresh identity is provisioned straight"
		ylw "  into the dashboard, NOT the locked 'billing_required' shell. Walking the"
		ylw "  Choose a plan path in this state records a failure against a product"
		ylw "  that is behaving correctly."
	elif [[ -z "$g_deploy" ]]; then
		red "✗ BILLING_GATE_ENABLED=true but DEPLOY_API_URL is empty — the gate has no"
		red "  upstream to ask, so every authenticated call fails closed with 503"
		red "  billing_unavailable. Set DEPLOY_API_URL=http://localhost:8001."
	elif [[ "$g_deploy" != *:8001* ]]; then
		ylw "⚠ DEPLOY_API_URL is '$g_deploy' — the billing walk expects the local mock on :8001."
	fi
	# An empty key is not a soft default: the gate builds "Authorization: Bearer "
	# and httpx rejects that header before opening a connection, so the request
	# never reaches /v2 at all. The gate maps that to 503 billing_unavailable —
	# reported as an upstream outage when it is really a missing setting, with
	# nothing in the mock's log to contradict it. Every authenticated call fails
	# closed, so the billing walk cannot pass. The mock does not check the value.
	if [[ "$g_billing" == True && "$g_deploy_key" != True ]]; then
		red "✗ BILLING_GATE_ENABLED=true but DEPLOY_API_KEY is empty."
		red "  The gate cannot build a legal Authorization header, so every /v2 read"
		red "  fails closed as 503 billing_unavailable and never reaches the mock."
		red "  Pass --deploy-api-key <any-non-empty-value>; the mock ignores it."
		exit 1
	fi
	# uvicorn reads these once, at import. A flag edited after the process
	# started is invisible until it restarts, and the stale process is
	# indistinguishable from a correctly configured one at every later checkpoint.
	ylw "  These are the values THIS run will use. Changing backend/.env now requires"
	ylw "  restarting the stack before the change takes effect."
fi

# ---------------------------------------------------------------- postgres

step "Postgres"
cd "$REPO_ROOT"
if [[ "$FRESH_DB" == true ]]; then
	ylw "  --fresh-db: destroying the existing pg volume"
	docker compose down -v >/dev/null 2>&1 || true
fi
docker compose up -d postgres >/dev/null
printf '  waiting for healthy'
for _ in $(seq 1 60); do
	state="$(docker compose ps --format json postgres 2>/dev/null | grep -o '"Health":"[a-z]*"' | head -1 | cut -d'"' -f4 || true)"
	[[ "$state" == "healthy" ]] && break
	printf '.'; sleep 1
done
echo
if [[ "${state:-}" != "healthy" ]]; then
	red "✗ Postgres did not become healthy — 'docker compose logs postgres'"
	exit 1
fi
grn "✓ Postgres healthy on 127.0.0.1:5433"

# ---------------------------------------------------------------- migrate

step "Migrations"
cd "$REPO_ROOT/backend"
if uv run alembic upgrade head >"$LOG_DIR/alembic.log" 2>&1; then
	grn "✓ alembic at head"
else
	red "✗ alembic upgrade failed — $LOG_DIR/alembic.log"
	tail -20 "$LOG_DIR/alembic.log"
	exit 1
fi

# ---------------------------------------------------------------- services

# Waits for an HTTP endpoint to answer with ANY status. A 401/404 still proves
# the listener is up, which is all this needs to decide "started".
wait_for_http() {
	local url="$1" name="$2" tries="${3:-60}"
	printf '  waiting for %s' "$name"
	for _ in $(seq 1 "$tries"); do
		if curl -sS -o /dev/null --max-time 2 "$url" 2>/dev/null; then echo; return 0; fi
		printf '.'; sleep 1
	done
	echo
	return 1
}

start_service() {
	local name="$1" logfile="$2" port="$3"; shift 3
	"$@" >"$logfile" 2>&1 &
	local pid=$!
	# The port is recorded so --stop, possibly from another shell with no flags,
	# can verify exactly the listeners this stack owns.
	echo "$pid $name $port" >> "$PID_FILE"
	OWN_PIDS+=("$pid")
	OWN_PORTS+=("$port")
	printf '  %s started (pid %s, port %s, log %s)\n' "$name" "$pid" "$port" "${logfile#"$REPO_ROOT"/}"
}

step "mock deploy API (:8001)"
cd "$REPO_ROOT/backend"
# `-m scripts.mock_deploy_api`, NOT `python scripts/mock_deploy_api.py` — running
# it by path puts scripts/ on sys.path instead of backend/, so `import app` fails.
# The stripe checkout branch answers a `checkout_url` built from
# MOCK_WEB_BASE_URL, which defaults to localhost:3001 — a port nothing in this
# stack listens on, so a card checkout dead-ends. Point it at the web app the
# browser actually resolves. (Wallet funding never uses it; UAT tests 4/5 take
# the wallet path. See 03-UAT.md setup step 9.)
export MOCK_WEB_BASE_URL="${MOCK_WEB_BASE_URL:-${WEB_ORIGIN_URL}}"
start_service mock-deploy "$LOG_DIR/mock-deploy.log" 8001 \
	uv run python -m scripts.mock_deploy_api --host "$BIND_HOST" --port 8001
if ! wait_for_http "http://127.0.0.1:8001/health" "mock-deploy"; then
	red "✗ mock deploy API never answered — $LOG_DIR/mock-deploy.log"
	tail -25 "$LOG_DIR/mock-deploy.log"
	exit 1
fi
grn "✓ mock deploy API up"

step "cloud-api (:8000)"
cd "$REPO_ROOT/backend"
# Started AFTER the mock on purpose. The billing gate reads /v2 on the first
# authenticated request, and a refused connection is cached as
# `billing_unavailable` for 60s (T-03-15's anti-hammer cache). Starting the
# consumer before its upstream leaves a window where one poll — a browser tab
# left open from the previous run is enough — poisons that cache, and the stack
# then reports billing_unavailable for a minute while being configured
# perfectly. That is indistinguishable from the empty-DEPLOY_API_KEY failure.
start_service cloud-api "$LOG_DIR/backend.log" 8000 \
	uv run uvicorn app.main:app --host "$BIND_HOST" --port 8000
if ! wait_for_http "http://127.0.0.1:8000/openapi.json" "cloud-api"; then
	red "✗ cloud-api never answered — $LOG_DIR/backend.log"
	tail -25 "$LOG_DIR/backend.log"
	exit 1
fi
grn "✓ cloud-api up"

step "worker tier (:8002 health)"
cd "$REPO_ROOT/backend"
# `python -m app.workers.channels` is what deploy/self-host runs. Without it the
# TenantExportWorker and TenantPurgeWorker never run, so an export sits at
# `queued` forever and a scheduled purge never advances — UAT test 6 reads that
# as a product failure when nothing is consuming the queue at all.
# Its health server hardcodes 8000 in the compose topology, which is cloud-api's
# port here; CHANNEL_WORKER_HEALTH_PORT moves it off the collision.
export CHANNEL_WORKER_HEALTH_PORT=8002
start_service workers "$LOG_DIR/workers.log" 8002 \
	uv run python -m app.workers.channels
if ! wait_for_http "http://127.0.0.1:8002/" "workers" 60; then
	ylw "⚠ worker tier never answered on :8002 — $LOG_DIR/workers.log"
	ylw "  Tenant exports stay 'queued' and scheduled purges never advance (UAT test 6)."
else
	grn "✓ worker tier up (tenant export + purge consumers running)"
fi


if [[ "$START_WEB" == true ]]; then
	step "web dashboard (:${WEB_PORT})"
	cd "$REPO_ROOT/apps/web"
	# UAT_JWT makes the BROWSER authenticate as a locally minted test identity:
	# the web app sends VITE_DEV_AUTH_TOKEN verbatim as the bearer on both the
	# client and the gate's server fn, and the backend falls through dev-bypass
	# to Clerk-JWT verification for anything that is not the literal dev token.
	# Without it the browser is `dev_browser` while your curl probes are someone
	# else — two principals, one conclusion. See 03-UAT.md setup step 8.
	if [[ -n "${UAT_JWT:-}" ]]; then
		export VITE_DEV_AUTH_TOKEN="$UAT_JWT"
		echo "  browser identity: VITE_DEV_AUTH_TOKEN <- UAT_JWT (sub from your minted token)"
		# An expired token 401s, and the dashboard renders that as "Your session
		# has expired. Please sign in again." — a product-shaped message for a
		# fixture that timed out. There is no sign-in to return to here, so the
		# walk simply stops. uat-auth.sh mints 24h by default and UAT sessions
		# outlive that, so report the remaining life before the browser depends
		# on it rather than after.
		jwt_report="$(SUB_TOKEN="$UAT_JWT" python3 -c "
import base64, json, os, time
part = os.environ['SUB_TOKEN'].split('.')[1]
part += '=' * (-len(part) % 4)
claims = json.loads(base64.urlsafe_b64decode(part))
print(claims.get('sub', '?'), int(claims.get('exp', 0)) - int(time.time()))
" 2>/dev/null || true)"
		jwt_sub="${jwt_report%% *}"
		jwt_left="${jwt_report##* }"
		if [[ -z "$jwt_report" ]]; then
			ylw "⚠ could not read UAT_JWT's expiry — is it a well-formed JWT?"
		elif ((jwt_left <= 0)); then
			red "✗ UAT_JWT for '$jwt_sub' EXPIRED $(( -jwt_left / 3600 ))h ago."
			red "  Every browser call 401s and the dashboard says the session expired,"
			red "  which reads as a product behaviour. Re-mint before walking anything:"
			red "      JWT=\$(bash scripts/uat-auth.sh --sub $jwt_sub --ttl 604800 --quiet)"
			exit 1
		elif ((jwt_left < 7200)); then
			ylw "⚠ UAT_JWT for '$jwt_sub' expires in $((jwt_left / 60)) min — it will die mid-walk."
			ylw "  Re-mint with a longer --ttl before starting a long test."
		else
			echo "  token valid for $((jwt_left / 3600))h (sub=$jwt_sub)"
		fi
	else
		ylw "⚠ UAT_JWT unset — the browser will authenticate as DEV_AUTH_CLERK_ID"
		echo "     (dev_browser), NOT as the identity your curl probes use. Tests 4, 5,"
		echo "     3d and 52 need them to be the same principal. See 03-UAT.md step 8."
	fi
	# `--host` here is Vite's bind address; the browser-facing URLs came from
	# ADVERTISE_HOST above and are already in this process's environment.
	# --strictPort is not optional: without it Vite answers a taken port by
	# quietly moving to the next one, and everything downstream — the health
	# check, the CORS origin list, the URL printed at the end — still describes
	# the port it did not get.
	start_service web "$LOG_DIR/web.log" "$WEB_PORT" \
		bun run dev --host "$BIND_HOST" --port "$WEB_PORT" --strictPort
	if ! wait_for_http "http://127.0.0.1:${WEB_PORT}/" "web" 90; then
		red "✗ web dev server never answered — $LOG_DIR/web.log"
		tail -25 "$LOG_DIR/web.log"
		exit 1
	fi
	# "Something answered" is not "our app answered". /@vite/client exists only
	# on a Vite dev server, so it separates this dashboard from any unrelated
	# service that happens to hold the port.
	if ! curl -sS -o /dev/null --max-time 5 "http://127.0.0.1:${WEB_PORT}/@vite/client" 2>/dev/null; then
		red "✗ :${WEB_PORT} answered, but it is not the Vite dev server."
		red "  Something else owns this port. Do NOT test against it — check"
		red "  $LOG_DIR/web.log for the port Vite actually took."
		exit 1
	fi
	grn "✓ web dashboard up (Vite dev server confirmed on :${WEB_PORT})"
fi

# ---------------------------------------------------------------- gate check

step "Access-gate reachability"

# THE check that decides whether a UAT result means anything. The gate endpoints
# run on require_clerk_claims, which has NO dev-bypass branch: when neither
# CLERK_JWT_ISSUER nor CLERK_PEM_PUBLIC_KEY is set they answer 503 regardless of
# DEV_AUTH_BYPASS. Because
# the route fails open on transport error, that 503 renders as a fully working
# dashboard — a false PASS on UAT test 3. Surface it loudly here.
# `-w %{http_code}` already prints 000 for a connection that never lands, and
# curl still exits non-zero — `|| echo 000` on top of it yields "000000", which
# misses the 000 case below and reports it as an unexpected status.
gate_code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 \
	http://127.0.0.1:8000/v1/access/status -H 'Authorization: Bearer uat-probe' 2>/dev/null || true)"
gate_code="${gate_code:-000}"

case "$gate_code" in
	503)
		red "✗ GET /v1/access/status → 503 — Clerk is NOT configured."
		echo "  DEV_AUTH_BYPASS does NOT cover this endpoint. The gate fails open on"
		echo "  transport error, so the dashboard will look completely healthy while"
		echo "  testing nothing. Do NOT record any gate test in this state."
		echo "  Fix: set CLERK_JWT_ISSUER (Clerk JWKS) or CLERK_PEM_PUBLIC_KEY in backend/.env, then restart."
		;;
	401|403)
		# 06.3 WR-02: the probe token never reaches signature verification, so
		# this proves the route is mounted and Clerk is configured — not that a
		# real session token verifies.
		grn "✓ gate route is mounted (probe token rejected with $gate_code, as expected)"
		echo "  This does NOT prove real tokens verify — only a real session token does."
		echo "  Mint a real token to walk the gate tests — 03-UAT.md step 2."
		;;
	200)
		grn "✓ gate is live and answered 200"
		;;
	000)
		red "✗ no answer from /v1/access/status — backend may still be booting"
		;;
	*)
		ylw "⚠ GET /v1/access/status → $gate_code (unexpected; check $LOG_DIR/backend.log)"
		;;
esac

# 06.3 WR-02: in JWKS mode the probe above cannot tell a good issuer from a bad
# one, so fetch the issuer's JWKS document directly. A PEM needs no network.
if [[ "${g_clerk_mode:-}" == jwks ]]; then
	clerk_jwks_probe "${g_issuer:-}"
fi

# ------------------------------------------------------- advertised reachability

# The services answered on 127.0.0.1 above. That says nothing about whether they
# answer on the address the BROWSER will use — a bind that quietly stayed on
# loopback, or a firewall on the tailscale interface, both pass every check so
# far and then fail only in the browser, where the gate's fail-open turns the
# failure into a dashboard that looks fine.
if [[ "$ADVERTISE_HOST" != "localhost" && "$ADVERTISE_HOST" != "127.0.0.1" ]]; then
	step "Advertised address"
	adv_ok=true
	for probe in "8000:$API_URL_ADV/openapi.json" "8001:$DEPLOY_URL_ADV/health"; do
		port="${probe%%:*}"; url="${probe#*:}"
		if curl -sS -o /dev/null --max-time 5 "$url" 2>/dev/null; then
			grn "✓ :${port} answers on ${ADVERTISE_HOST}"
		else
			red "✗ :${port} does NOT answer on ${ADVERTISE_HOST} (${url})"
			adv_ok=false
		fi
	done
	if [[ "$START_WEB" == true ]]; then
		if curl -sS -o /dev/null --max-time 10 "$WEB_ORIGIN_URL/" 2>/dev/null; then
			grn "✓ :${WEB_PORT} answers on ${ADVERTISE_HOST}"
		else
			red "✗ :${WEB_PORT} does NOT answer on ${ADVERTISE_HOST}"
			adv_ok=false
		fi
	fi
	[[ "$adv_ok" == true ]] || ylw "  A remote browser will not reach this stack. Check the bind host and any firewall."
fi

# ---------------------------------------------------------------- summary

step "Stack up"
cat <<EOF
  cloud-api      http://${ADVERTISE_HOST}:8000
  mock deploy    http://${ADVERTISE_HOST}:8001
  workers        health on 127.0.0.1:8002 (tenant export + purge)
$( [[ "$START_WEB" == true ]] && echo "  web dashboard  ${WEB_ORIGIN_URL}    <- open THIS in the browser" )
  postgres       127.0.0.1:5433
  logs           ${LOG_DIR#"$REPO_ROOT"/}/

  Mint a test identity (a sub you have not used = a fresh, unprovisioned user):
    JWT=\$(bash scripts/uat-auth.sh --sub uat_tester_1 --check)

  Then restart this script with that identity in the browser too:
    UAT_JWT="\$JWT" bash scripts/uat-stack.sh --host ${BIND_HOST}

  First run on a host (generates the keypair and writes backend/.env):
    bash scripts/uat-auth.sh --write-env      # then restart uvicorn

  Ctrl-C stops everything. 'bash scripts/uat-stack.sh --stop' from another shell.
EOF

if [[ "$ADVERTISE_HOST" != "localhost" && "$ADVERTISE_HOST" != "127.0.0.1" ]]; then
cat <<EOF

  Remote browser: every URL above is already what the page will call. Do NOT
  substitute localhost in the address bar — the page would then be same-origin
  with the wrong machine and the API calls would leave this stack entirely.
    open ${WEB_ORIGIN_URL}/
EOF
fi

# Idle in the foreground so the EXIT trap owns the children's lifetime.
wait
