# Production Origins: DNS, TLS And Apex Mail

Every origin in [`packages/shared/src/destinations.ts`][destinations] is compiled
into the shipped CLI and the web bundle. The cloud API origin is where a signed-in
binary sends a user's bearer token, and the support address is rendered at the
billing dunning banner and the deactivated-account page.

Nothing in this repository can create a DNS record, start a server or add an MX
record. Those live in a Cloudflare zone and on hosts the operator builds. This
runbook is for the operator who holds them. The host-side procedures (building
the hosts, their tunnels, and deploying the services onto them) are in the hosted
operator runbooks (iforge-hosted), not here.

The gate that measures the current state is
[`scripts/check_production_origins.py`](../../scripts/check_production_origins.py).
It is red today and no commit here can make it green. It is deliberately not part
of `scripts/test.sh` for that reason; its enforcement point is the publish path.

## How production origins are reached

No production origin is reached through a public address record, and no
production host accepts inbound 80/443. Each host makes an outbound Cloudflare
Tunnel connection, and each proxied name in the zone is a `CNAME` to its host's
tunnel (`<tunnel id>.cfargotunnel.com`). Cloudflare's edge is the only public TLS
terminator.

On a Kamal host, kamal-proxy keeps the `proxy.ssl` block in
[`config/deploy.yml`](../../config/deploy.yml) (the zone's Origin CA pair), and
cloudflared reaches it at `https://localhost:443` with the public name as SNI.
Do not remove `ssl:` from the config: it is what kamal-proxy serves on that hop.

Kamal takes kamal-proxy's bind from `proxy.run.bind_ips` whenever
`config/deploy.yml` has a `proxy.run` block, which this config has for its log
hardening, and then ignores `kamal proxy boot_config`. This config pins
`bind_ips` to `127.0.0.1`, so kamal-proxy publishes 80 and 443 on loopback only
and is reached through the host's tunnel. Docker's published ports bypass the
host firewall, so this bind, not the firewall, is what keeps them off the public
address.

A deploy does not rebind a running proxy: `kamal proxy reboot` does. On a host
that serves more than one Kamal service, every service's config must agree on
the bind, because one kamal-proxy serves them all and the last reboot decides
it. A self-hoster who wants kamal-proxy on a public address changes `bind_ips`
in their own copy. This section describes iforge's own production origins only.

Done: `scripts/test-kamal-proxy-bind-render.sh` exits 0 and its last line is
`kamal-proxy bind: --publish 127.0.0.1:80:80 --publish 127.0.0.1:443:443`. It
renders kamal-proxy's run command with Kamal 2.12.0 locally and contacts no
host; run it in Kamal's own image, as its header shows.

## Measured state

Re-measure rather than trusting this table. One command produces it:

```bash
python3 scripts/check_production_origins.py
```

Measured from a developer host on 2026-10-26 at 12:44Z. The rows are transcribed
into a table rather than pasted as a block, because a block of recorded output
reads as something to copy and none of it is a command:

| Row | Host | Verdict | Detail |
| --- | --- | --- | --- |
| origin | `iforge.dev` | `SERVED` | HTTP 200 |
| origin | `www.iforge.dev` | `SERVED` | HTTP 200 after 1 redirect, ending at the apex |
| origin | `cloud.iforge.dev` | `BAD_REDIRECT` | HTTP 301 loop to the URL requested |
| origin | `cloud-api.iforge.dev` | `BAD_REDIRECT` | HTTP 301 loop to the URL requested |
| origin | `api.iforge.dev` | `BAD_REDIRECT` | HTTP 301 loop to the URL requested |
| origin | `assets.iforge.dev` | `BAD_REDIRECT` | HTTP 301 loop to the URL requested |
| mail | `iforge.dev` | `MAIL` | 3 MX record(s) via the system resolver |

Summary line, verbatim: `production origins failed: 6 origins probed, 2 served,
0 no-tls, 0 no-origin, 0 unresolved, 4 bad-redirect; mail MAIL`. Exit status 1.

Every name now resolves through Cloudflare's proxy. That is a change from the
first measurement (2026-09-11), when the two API names answered 525 and the
apex, `www`, `cloud` and `assets` had no address record. **No iforge service is
deployed behind `cloud`, `cloud-api`, `api` or `assets`.** No production host
has been built yet. The records exist, but whatever they reach only redirects.

The zone itself is live and answers authoritatively, so the domain is held:

```bash
dig +short SOA iforge.dev
dig +short NS iforge.dev
```

## Step 1 — What belongs behind each name

| Host | What is behind it | Record |
| --- | --- | --- |
| `iforge.dev` | the marketing homepage | serves today |
| `www.iforge.dev` | the apex alias; also a CORS member | serves today (redirects to the apex) |
| `cloud-api.iforge.dev` | the cloud API (this repository's backend, Kamal service `iforge`) | proxied `CNAME` to its host's tunnel |
| `api.iforge.dev` | the hosted deploy API that publishes `/v2` | proxied `CNAME` to the tunnel of the host it runs on |
| `cloud.iforge.dev` | the signed-in dashboard (`apps/web`, hosted build) | open: where it runs is undecided (hosted gap G-11) |
| `assets.iforge.dev` | static assets and the channel icon base | open: a bucket custom domain is recommended, undecided (G-12) |

`assets` is the one entry that fronts a static bucket rather than an application
origin. `iconBase` in the destinations record is a path under the same host, so
one record covers both entries. A bucket custom domain needs no host and no
tunnel.

The dashboard's hosting is a recorded open decision, not a default. Wherever it
runs, it must be served over https under `iforge.dev`: the runtime UI's edge
session cookie is `SameSite=Strict`, so a dashboard on another domain cannot open
a runtime.

Done: each name has a decided origin, recorded in the hosted operator runbooks.

## Step 2 — Put a service behind each name, then cut the name over

A name serves when something answers behind it. For the two API names that means,
in this order, per the hosted operator runbooks:

1. The host is built, with its tunnel connected and no public origin.
2. The service is deployed and answers `/health` on loopback.
3. The name's record is replaced with a proxied `CNAME` to that host's tunnel
   (`cloudflared tunnel route dns <tunnel> <name>`, or the dashboard).
4. `curl -fsS https://<name>/health` answers through the tunnel, and the
   webhooks that target the name (Stripe on `api`, Clerk on `cloud-api`) show
   2xx deliveries.

Set the zone's SSL/TLS encryption mode to **Full (strict)**. For a tunnel name the
mode does not reach the loopback hop, but strict means no proxied name can ever
fall back to an origin the edge does not verify. A bearer token sent to
`cloud-api` must never cross an unverified hop.

Done: `cloud-api` and `api` answer `/health` with 200 through their tunnel, and
the gate reports both rows `SERVED`.

### `BAD_REDIRECT`: a 301 back to the URL you asked for

Measured 2026-10-26: `cloud`, `cloud-api`, `api` and `assets` each answered
`301` with a `Location` naming the exact `https://` URL requested, so a browser
shows "too many redirects". The gate reports this as `BAD_REDIRECT`:

```bash
curl -sSI https://api.iforge.dev/ | grep -iE '^(HTTP|location)'
# HTTP/2 301
# location: https://api.iforge.dev/      <- the URL you just asked for
```

A loop like this means the edge reached something that only redirects to https.
Nothing iforge runs is deployed behind these names, so this is not an iforge
service misbehaving. Read the record each name carries in the zone dashboard to
see what it reaches.

**Changing the SSL/TLS mode does not fix it.** With nothing deployed, Full
(strict) only trades the loop for an edge error: 521 or 522 when nothing answers,
525 or 526 when the handshake fails. The fix is Step 2: build the host, deploy
the service, and replace the record with the tunnel `CNAME`. Do not turn off
`ssl:` in `config/deploy.yml` to chase a loop either; see "How production origins
are reached".

## Step 3 — Apex mail so the support address can receive

`supportEmail` in the destinations record is rendered at the billing dunning
banner and the deactivated-account page. A user told to write for help whose mail
is silently discarded is worse off than one told there is no support channel, so
mail acceptance is a user-facing obligation rather than a nicety.

**The address stays.** It is the right address once mail routes, and the fix is to
route mail rather than to withdraw the address.

At 00:59Z on 2026-09-11 the apex answered `NOERROR` with zero MX records. By
01:02Z the same query returned three, so this step was performed on the zone
during that window. Confirm it for yourself rather than trusting either number:

```bash
dig MX iforge.dev +noall +answer
dig +short NS iforge.dev
dig +short MX iforge.dev @donald.ns.cloudflare.com
```

Done: the authoritative answer lists at least one MX record.

If it does not, add mail routing for the apex in the Cloudflare dashboard —
Email Routing publishes the MX records for the zone when it is enabled — and
then create the routing rule that delivers the `support` address to a mailbox
somebody reads.

**Expect your own resolver to lie to you for up to half an hour afterwards.**
Adding an MX record to a name that previously had none leaves a cached negative
answer behind, and a recursive resolver keeps serving it until the zone's
negative TTL expires — 1800 seconds here. That is exactly what happened during
this measurement: `dig +short MX` on the developer host returned nothing while
`1.1.1.1` and the zone's own nameservers returned three records, and the local
negative answer carried the *pre-change* SOA serial, which is how you tell a
stale cache from a real absence:

```bash
dig MX iforge.dev +noall +comments +authority | grep -E 'status|SOA'
dig +short SOA iforge.dev
```

If the serial in the negative answer is older than the zone's current serial,
your resolver is out of date and the record does exist. The gate handles this
itself: a negative answer is re-asked of the zone's own nameservers before it
is recorded as `NO_MAIL`, so the verdict does not flap while a cache drains.

**An MX record is not a mailbox.** The gate checks that the domain publishes MX
records, which is the part that is visible from outside; it cannot tell whether a
routing rule exists behind them or whether anyone reads the destination. Close
that half by sending a real message and confirming it arrives:

```bash
TO=support@iforge.dev
printf 'Subject: apex mail check\n\nrouting reachable\n' | sendmail "$TO"
```

Done: the message arrives in the mailbox the routing rule names. Until it does,
the address renders at two account surfaces and the mail goes nowhere, which is
the condition this step exists to end.

## Step 4 — Verify

Run the gate with no arguments:

```bash
python3 scripts/check_production_origins.py
```

Done: exit status 0, every origin row reads `SERVED`, and the mail row reads
`MAIL`. The summary line reads `production origins passed`.

Check the exit status explicitly — the rows are for a human and the status is
what the publish path consults:

```bash
python3 scripts/check_production_origins.py >/dev/null; echo "exit=$?"
```

**Edge error pages fail the gate.** 525 and 526 (the origin's TLS handshake
failed) read `NO_TLS`. 520 to 524 and 530 (nothing behind the name answered: a
stopped service, a closed port, a tunnel with no connector) read `NO_ORIGIN`.
Both fail. A 500, 502 or 503 still reads `SERVED`, because a real origin can
answer with one, so check `/health` on both API names directly as well.

## The publish gate

`packages/cli/scripts/check-native-release.mjs` spawns the same guard before it
parses the release manifest, so a native release cannot be verified while any
baked-in origin is unserved:

```bash
bun run --cwd packages/cli check:native-release
```

`prepublishOnly` in `packages/cli/package.json` runs the origin check too, so the
npm path is gated as well as the native one. `bun run --cwd packages/cli build`
bakes both API origins into the bundle with `--define`, which is why the check
sits in front of it.

There is no way to turn either off. A bypass would be used exactly once, in the
situation the gate exists to prevent.

This is the obligation recorded as **WINDOWS #88**: no native CLI build ships
while a baked-in origin is unserved. The gate makes it enforceable. It does not
discharge it — the entry stays open until every origin serves and steps 1 through 3
are done.

## Decisions held (2026-09-11)

The operator who holds the Cloudflare zone reviewed the measured state above and
ruled on it. Recorded here rather than only in a planning document, because the
next person to read this runbook is the one who needs them.

1. **The verdicts match the zone.** No host believed to be live reads as
   unserved. The table is a fact about the zone, not a defect in the gate.
2. **The steps are performable** with the access the operator holds. The zone is
   theirs.
3. **WINDOWS #88 is confirmed and stays open.** No native CLI build ships while a
   baked-in origin is unserved. This runbook and the gate make that obligation
   enforceable; they do not discharge it. The entry closes when the records
   exist, not when the gate exists.
4. **The support address stays.** It keeps rendering at the billing dunning
   banner and the deactivated-account page. It is the right address once mail
   routes, so the fix is step 3 — route mail — and not withdrawing the address.
   Until a routing rule delivers it to a mailbox somebody reads, mail sent there
   is dropped, and that is a known, accepted interim state rather than an
   oversight.
5. **The npm publish path is gated too.** `prepublishOnly` was found to call only
   `check:publish-manifest` and `build`, never `check:native-release`, so the
   npm route was ungated while `build` baked both API origins into the bundle.
   The origin check was added there by explicit decision.

Status on 2026-10-26: every record now exists, but four names do not serve
(see "Measured state"). Decision 3 is read as "closes when every origin
serves": records alone do not discharge #88.

## Related

- [`release.md`](release.md) — the release procedure this gate blocks
- [`packages/shared/src/destinations.ts`][destinations] — the record every origin
  above is read from

[destinations]: ../../packages/shared/src/destinations.ts
