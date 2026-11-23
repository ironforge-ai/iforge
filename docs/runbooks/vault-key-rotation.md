# Vault Key Rotation and Secret Recovery Runbook

Use this runbook when a vault key is compromised or lost, or when an agent's
manifest fails closed because a stored secret can no longer be read. Every
step goes through the admin API with the `X-Admin-Key` header. No step needs
a database shell, and no response, log line or audit event contains a secret
value.

The examples use `$API` for the backend's public URL, `$ADMIN_API_KEY` for the
admin key and `$AGENT` for the agent id.

## When to rotate

Rotate only as incident response: a vault key is known or suspected to be
compromised, or it is about to be retired. Nothing schedules rotation, and
no policy triggers it. Each rotation is started by an operator, by hand, and
is audited.

Rotation covers the **versioned** secrets, the rows that record which key
encrypted them:

- an agent's hosted runtime secrets
- the credential atoms of the agent's live companion installations

It does not cover the unversioned tables. See [Residual](#residual) before
you treat a compromised key as retired.

## Add a key and make it active

The keyring has one key per version.

- `VAULT_ENCRYPTION_KEY` is always version `vault.v1`.
- `VAULT_ENCRYPTION_KEYS` adds more versions as a JSON object mapping each
  version to 64 hex characters, for example
  `{"vault.v2": "<64 hex characters>"}`.
- `VAULT_ACTIVE_KEY_VERSION` names the version that new versioned writes use.

1. Generate the new key:

   ```bash
   python3 -c "import os; print(os.urandom(32).hex())"
   ```

2. Add it to `VAULT_ENCRYPTION_KEYS` under a new version name, set
   `VAULT_ACTIVE_KEY_VERSION` to that version, and restart the backend. Keep
   every older version in the keyring. A row is always decrypted with the key
   its own version names, so removing a version makes each row still on it
   unreadable.

The backend refuses to start in two cases:

- the active version is not in the keyring
- a `vault.v1` entry in `VAULT_ENCRYPTION_KEYS` disagrees with
  `VAULT_ENCRYPTION_KEY`

The error names the setting and never prints key material.

Done: `curl -sS "$API/health"` answers `{"status":"ok"}`. From here on, every
new hosted runtime secret and companion atom is written under the active
version.

## Rotate an agent

Rotate each agent whose secrets must move to the new key:

```bash
curl -sS -X POST "$API/v1/admin/agents/$AGENT/runtime-secrets/rotate" \
  -H "X-Admin-Key: $ADMIN_API_KEY"
```

The response names secrets by reference and never includes a value:

```json
{
  "agent_id": "…",
  "active_key_version": "vault.v2",
  "reencrypted": ["companion:<installation_id>:db-password", "secret://…"],
  "already_current": [],
  "unreadable": []
}
```

- `reencrypted`: readable rows that were on an older version and are now on
  the active one.
- `already_current`: rows that were already on the active version. A second
  rotation reports every row here. Rotation is safe to repeat, and concurrent
  rotations wait on each other's row locks.
- `unreadable`: rows that cannot be read under their own version. Their key
  was lost, or the version is no longer in the keyring. Rotation leaves them
  untouched. Recover them with the next section.

Hosted runtime secrets are named by their `secret://` reference. Companion
atoms are named `companion:<installation_id>:<secret_name>`. Each call writes
one `hosted_runtime_secrets.rotate` audit event listing the same references.

A `503 vault_keyring_unavailable` means the active version has no key. Fix the
keyring and restart the backend.

To see every secret of an agent with its version and whether it can be read:

```bash
curl -sS "$API/v1/admin/agents/$AGENT/runtime-secrets/health" \
  -H "X-Admin-Key: $ADMIN_API_KEY"
```

## Recover unreadable secrets

A secret is unreadable when its key is gone. While any secret is unreadable,
the agent's manifest fails closed. The health route lists each one with
`"readable": false`.

Recovery acts only on secrets that are currently unreadable. A readable one is
refused with `409 runtime_secret_readable`, so recovery can never be used to
quietly change a working credential. All actions go to one route:

```bash
curl -sS -X POST "$API/v1/admin/agents/$AGENT/runtime-secrets/recover" \
  -H "X-Admin-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '<body>'
```

Each call writes one `hosted_runtime_secrets.recover` audit event of
references. The response lists what changed and what is still unreadable
(`remaining_unreadable`).

### Hosted runtime secrets

Delete a named secret, or replace it with a value the provisioner supplies:

```json
{"delete_refs": ["secret://…"], "replace": {"secret://…": "<new value>"}}
```

The provisioner's next runtime-state upsert also replaces an unreadable secret
with the value it sends.

### Companion credentials the platform generates

`db-password`, `jwt-secret`, `api-key` and `admin-password` are regenerated
with new random values. The old values are never read:

```json
{"companion_regenerate": [{"installation_id": "<id>", "secret_name": "db-password"}]}
```

The installation records `credentials_regenerated_at`. What each one does on
the runtime's next converge:

- A new `db-password` is applied to the running database with `ALTER ROLE`
  before Postiz restarts. The runtime sends the password to psql on standard
  input, never on a command line.
- A new `api-key` is seeded again by the runtime's bootstrap.
- A new `jwt-secret` signs every Postiz session out.
- A new `admin-password` is used only by the bootstrap. The tenant's own
  sign-in is unaffected.

### Companion provider credentials

Re-mint the credential of an install step at its provider:

```json
{"companion_reprovision": [{"installation_id": "<id>", "step": "media"}]}
```

| Step | What happens |
| --- | --- |
| `media` | The installation's bucket token is rolled. The old value stops working, and the new S3 key is stored. The bucket, its objects and its domain are not touched. |
| `temporal` | A new namespace token is minted and stored. The old token cannot be revoked by itself. It stays valid until it expires, unless you rotate the operator's Temporal signing key. |
| `edge` | The tunnel's connector token is read back from Cloudflare and stored again. The token belongs to the tunnel, so this recovers it without moving the hostname. It does not revoke it. |
| `backup-key` | Every backup key under the installation's name is revoked, and a new no-delete key is minted. The repository and its cipher pass are unchanged. |

Reprovision never runs a step's teardown. Teardown empties and deletes the
media bucket and marks the Temporal namespace deleted, which would destroy the
tenant's data just to recover a key.

- A step that never ran is refused with `409 companion_step_not_provisioned`.
- A provider failure answers `502 companion_reprovision_failed`, naming the
  step and a reason code. It is safe to retry.

### Abandon backups (lost backup cipher pass)

A backup repository is encrypted under its cipher pass. If the stored pass
cannot be read, the platform can never restore from that repository again.
The cipher pass cannot be regenerated: that is refused with
`409 companion_backup_cipher_pass_requires_abandon`. The only way forward is
explicit:

```json
{"abandon_backups": ["<installation_id>"]}
```

This does four things:

- generates a new cipher pass
- moves the installation to a new stanza, `<original stanza>-r<n>`, and a
  new repository, `<original path>/r<n>`, a subdirectory of the original
  repository path
- records the old repository in the installation's `backup_state` under
  `abandoned_repositories`, with its restore markers, and marks it
  `restorable_by_platform: false`
- names the old stanza and path in the response

No file is deleted. The old repository stays in the bucket under its normal
retention. The new repository sits inside the installation's key prefix, so
the existing backup key and the rehearsal key keep working and are not
re-minted. A second abandon moves to `/r2`, and so on.

An installation abandoned by an earlier build has a `-r<n>` path next to, and
outside, its key prefix, and its backups fail. The next abandon moves it to
`/r<n+1>`, inside the prefix.

**Consequence:** every backup taken before this call is unrecoverable by the
platform. Point-in-time recovery starts again from the new repository's first
backup, which the runtime takes on its next converge.

## Downtime

Plan for downtime before you start. There is no drain path.

- **Rotation** changes each secret's source identity, so the runtime sees a
  new manifest revision and converges again. The values do not change, so a
  companion usually keeps running. It may still restart, so treat every
  rotation as a planned maintenance event for the agent.
- **Regenerating a companion credential** restarts Postiz. A new
  `db-password` first runs `ALTER ROLE` on the running database, then Postiz
  restarts. A new `jwt-secret` signs everyone out.
- **Reprovisioning `media` or `backup-key`** revokes the old credential
  immediately. Uploads or backups fail until the runtime applies the new one,
  normally within one converge interval. A new backup key also restarts the
  companion's database, because WAL archiving runs inside it with the key it
  started with.
- **Abandoning backups** restarts the companion's database on the next
  converge, so WAL archiving moves to the new stanza and repository. It also
  ends point-in-time recovery for everything before it (see above).

## Residual

Rotation does **not** retire a compromised `vault.v1`. These tables are
encrypted with `VAULT_ENCRYPTION_KEY` and have no column that could name
another key:

- vault items
- channel tokens
- AI provider credentials

They stay on `vault.v1` whatever version is active. A compromised `vault.v1`
therefore still exposes them after every agent has been rotated. Replacing
`vault.v1` completely would mean re-encrypting those tables, which is a
separate decision this runbook does not cover. Until that decision is made,
treat a compromised `vault.v1` as an incident that rotation only contains.

Rotation also covers only live companion installations. A removed
installation's retained atoms, such as the cipher pass of its retained backups,
are not re-encrypted.
