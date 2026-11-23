# Sign-up Access (Clerk)

Use this runbook when an operator needs to decide who may create an iforge
account. Clerk decides this on its own, through the instance's access mode and
its allowlist and blocklist. iforge has no sign-up policy of its own. It
provisions the user and their Personal project the first time it sees a
verified Clerk session.

## Choose an access mode

Exactly one access mode is active on a Clerk instance. The Dashboard label and
the `sign_up_mode` API value name the same setting:

| Dashboard label | `sign_up_mode` value | Use it when |
|-----------------|----------------------|-------------|
| Open | `public` | Anyone may sign up (public launch). Clerk's default. |
| Invite-only | `restricted` | Only people you invite from Clerk may sign up (closed beta). |
| Waitlist | `waitlist` | People ask to join and you approve them in Clerk. |

Waitlist approval emails are sent only if the Email strategy is enabled on the
instance.

## Set it in the Clerk Dashboard

1. Open the Clerk Dashboard for the instance the backend verifies sessions
   against.
2. Go to **User & authentication** → **Access mode**.
3. Select Open, Invite-only or Waitlist and save.
4. For Invite-only, send invitations from the **Users** page in Clerk.

Done: the Access mode page shows the mode you selected.

## Set it from the command line

1. Read the current schema so you patch real keys:

   ```bash
   npx clerk@latest config schema
   ```

2. Patch the instance. Pass the payload inline with `--json` or from a file
   with `--file`:

   ```bash
   npx clerk@latest config patch --json '{"sign_up_mode": "restricted"}'
   ```

The keys that control sign-up are `sign_up_mode` (`public`, `restricted` or
`waitlist`) and `auth_access_control` (allowlist and blocklist). Clerk also
offers `block_email_subaddresses` and `block_disposable_email_domains`.

Done: `npx clerk@latest config schema` (or the Dashboard Access mode page)
reports the `sign_up_mode` you patched.

## Allowlists and blocklists

Configure allowlists and blocklists in Clerk, on the Dashboard Restrictions
settings or through `auth_access_control`. The blocklist applies independently
of the access mode: a blocked address or domain cannot sign up even when the
mode is Open. iforge adds no allowlist or blocklist of its own.

## What iforge does after sign-up

- The first verified Clerk session creates the user and their Personal
  project. There is no invite step and no code to redeem.
- With the billing gate enabled, an account without a paid plan lands on
  **Choose a plan** in the dashboard and stays locked until a plan is paid
  from **Settings** → **Compute**. The hosted service refuses compute with
  `402 payment_required` until then.
- iforge behaves the same whichever access mode is chosen, including Clerk's
  default Open. The mode only decides who reaches iforge; it never changes
  what iforge does with them.
- Partner-tenant accounts are billing-exempt. A Clerk account is exempt only
  when an operator exempts it explicitly (below), or when its row was
  grandfathered: every user row that existed when the access columns were added
  had `users.invite_exempted_at` and `users.billing_unlocked_at` stamped, and
  keeps both.

### Exempting a Clerk account from billing

Creating an account never exempts it, whichever call creates it: the user's
own first sign-in, or an `X-Admin-Key` call such as hosted deploy
orchestration (`POST /v1/admin/auth/keys`, environments, agents, channels,
AI providers). Exemption is a separate operator action:

```bash
curl -X PUT "$API/v1/admin/auth/billing-exemptions" \
  -H "X-Admin-Key: $ADMIN_API_KEY" -H 'Content-Type: application/json' \
  -d '{"target_clerk_id": "user_...", "exempt": true, "reason": "design partner"}'
```

- `exempt: true` stamps `users.invite_exempted_at`. The gate then lets the
  account through without reading `/v2`, even if it never subscribes.
- `exempt: false` clears it, and the account is gated on its `/v2`
  subscription again.
- A revoke that changes the exemption also clears the unlock
  (`users.billing_unlocked_at`, stamped when the account first had an active or
  trialing subscription). An account that once paid and has since lapsed is
  locked again, and a later re-grant restores only the exemption, not the
  unlock. The `billing.exempt_revoked` audit event records
  `billing_unlock_cleared` and the cleared value in
  `previous_billing_unlocked_at`. After a mistaken revoke, copy that value back
  into `users.billing_unlocked_at` by hand.
- The call is idempotent (`changed` reports whether anything changed). It
  creates the user row if the account has never signed in.
- Each call writes a `billing.exempt_granted` or `billing.exempt_revoked`
  audit event with the reason.

This route is the only way to exempt a Clerk account today. Two other exempt
populations exist:

- Partner-tenant accounts are exempt by design when they are created, because
  they cannot pay.
- Grandfathered rows keep the exemption and the unlock that the
  access-columns migration stamped on every user row that existed then. The
  gate lets them through without reading `/v2`, the same as an operator grant.
  They change only if an operator revokes the exemption with the route above,
  which also clears their unlock.

Tests in `backend/tests/test_user_provisioning_first_touch.py` enforce this:

- `test_first_touch_admin_route_never_exempts` calls every admin route that can
  create a Clerk account and checks that the new account is still gated.
- `test_billing_exemption_writer_inventory` lists every code path that writes
  the exemption, and how many writes each one makes.
- `test_grandfathered_row_is_unaffected` checks that a grandfathered row reaches
  gated routes with the gate on and no `/v2` call.

## Lapsed subscriptions

An unlocked, non-exempt account is not unlocked for ever. The gate re-reads its
`/v2` subscription at most once an hour (`BILLING_RECHECK_INTERVAL_SECONDS`,
default 3600):

- A read that finds the subscription neither active nor trialing (none,
  canceled, past due) stamps `users.billing_lapsed_at` and writes a
  `billing.lapse_observed` audit event. The account keeps access.
- Once the lapse has lasted one day (`BILLING_LAPSE_GRACE_SECONDS`, default
  86400), the next read clears `users.billing_unlocked_at` and writes
  `billing.relocked` with `previous_billing_unlocked_at`. The account is then
  locked like any unpaid account and lands on the plan picker.
- A paid read during the grace clears the lapse (`billing.lapse_cleared`);
  paying after a relock unlocks the account again as usual.
- If `/v2` cannot be reached during a re-read, the account keeps access and the
  read is retried after a minute.

The grace starts when the gate first sees the lapse, so an account that is
active every day is locked about a day after its subscription ends. Exempt
accounts are never re-read.

## Retired settings

The old invite gate setting no longer exists. Remove `INVITE_GATE_ENABLED`
from any deployed environment file. The backend ignores it if it is left in
place, but it no longer controls anything.
