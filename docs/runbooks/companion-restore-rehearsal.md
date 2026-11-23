# Companion restore rehearsal runbook

Use this runbook to schedule and read the point-in-time restore rehearsal for a tenant-installed
companion (Postiz), to run retention expiry, and to drill that the runtime's backup key cannot
destroy backups.

The rehearsal is how restorability is proven rather than asserted. It restores ONE installation's
database to a chosen instant, in a scratch container, and passes only when the database rows say
so.

## What the rehearsal proves

Each scheduled backup of an installation first writes a marker row, a random token, into
`iforge_rehearsal.marker` in the installation's database. The control plane keeps each marker and
the time its backup completed.

A rehearsal:

1. picks the newest pair of markers `(k, k+1)` where backup `k` completed;
2. picks the instant halfway between that completion and marker `k+1`'s write;
3. restores the installation's database to that instant in a scratch container;
4. passes only if marker `k` is in the restored database and marker `k+1` is not.

A pass proves that this installation's database can be restored to a chosen instant from its own
repository, with a base backup plus continuous WAL, and that the restore lands where it was told
to land.

It does NOT prove:

- **Application reachability.** A blank Postiz answers HTTP 200, and `/` answers 307 with its
  backend dead, so the runner never probes the application. Rows are the only evidence.
- **Media.** Uploaded media lives in object storage, not in the database; the tenant export
  covers it.
- **Other installations.** One rehearsal covers one installation. Schedule one per installation.

The first rehearsal of each installation also checks research assumption A4: that Postiz's
`prisma db push`, which runs on every Postiz start, leaves the `iforge_rehearsal` schema alone. The
bundle names the OLDEST marker the control plane knows. If the restore still holds that row, the
schema survived every Postiz start since it was written. An installation's rehearsal cannot pass
until one rehearsal has reported the schema as surviving.

## Credentials

The rehearsal uses a key of its own:

- It is minted for one rehearsal, with `listFiles` and `readFiles` only, restricted to the
  installation's prefix, and valid for at most one hour.
- It is neither the runtime's backup key (list, read and write, never delete) nor the operator's
  delete-capable key.
- The control plane revokes it when the runner posts the result. A runner that dies without
  posting leaves it to expire within the hour.

The restore also runs with `--archive-mode=off`, so the scratch database can never push WAL into
the tenant's repository.

The bundle carries the read key and the repository's cipher pass. The runner writes both only to an
env file, mode `0600`, in a private temporary directory, and removes it when the run ends. Neither
ever appears on a command line or in a log line, and the control plane never logs or audits them.

## Run a rehearsal

Run from an operator host that has:

- Docker (or podman);
- the control plane's admin key;
- outbound reach to the control plane and to the backup provider's S3 endpoint;
- this repository checked out, so the runner can read the pinned database image from
  `config/postgres-image.txt`.

Run:

```bash
export IFORGE_ADMIN_API_KEY="<admin key>"   # never pass the key on the command line
python3 backend/scripts/companion_pitr_rehearsal.py \
  --api-url "https://<control plane>" \
  --installation-id "<installation id>"
```

Add `--runtime podman` on a podman host. The runner needs Python 3 and `httpx`; `uv run` from
`backend/` provides both.

The runner:

1. asks the control plane for a bundle
   (`POST /v1/admin/companions/installations/<id>/rehearsals`);
2. creates a fresh volume and restores the stanza into it with
   `pgbackrest --delta --type=time --target=<instant> --target-action=promote --archive-mode=off restore`;
3. starts PostgreSQL from the same image and waits for it to promote at the target;
4. reads `iforge_rehearsal.marker`;
5. posts the result (`POST /v1/admin/companions/rehearsals/<rehearsal id>/result`);
6. removes the container, the volume and the env file, whatever happened.

The bundle's target is UTC with microsecond precision and an explicit `+00:00`. pgBackRest takes it
as `YYYY-MM-DD HH:MM:SS.ffffff+00:00`: the same instant, with a space where the bundle has `T`.

### Exit codes

| Code | Meaning | What to do |
|------|---------|------------|
| `0` | The rehearsal passed: the earlier marker present, the later one absent, the schema survived. | Nothing. |
| `1` | The rehearsal ran and the rows failed it. | Read the recorded reason below. This is a restorability finding; treat it as an incident. |
| `2` | Operational error: no bundle, a failed restore, a server that never promoted. | Read the runner's stderr. The runner still posts a failed result, so the key is revoked. |

### Refusals when asking for a bundle

| HTTP | Code | Meaning |
|------|------|---------|
| 409 | `companion_rehearsal_needs_two_markers` | Fewer than two markers, the earlier with a completed backup. A new installation qualifies after its second scheduled backup. |
| 409 | `companion_rehearsal_no_backup_repository` | The installation's backup step never recorded a repository, or its cipher pass is gone. |
| 502 | `companion_rehearsal_backup_provider_failed` | The backup provider refused the read key, or answered a key wider or longer-lived than requested (it is revoked). |
| 404 | `companion_installation_not_found` | Unknown or removed installation. |

## Schedule it

Run the rehearsal **weekly for each installation**, from the operator host, in the operator's own
scheduler. Stagger installations so restores do not run at the same moment. A cron entry:

```cron
# m h dom mon dow  command
17 4 * * 1  /path/to/run-rehearsal.sh <installation id>
```

`run-rehearsal.sh` is the operator's own wrapper. It should:

- read the admin key from a root-only file into `IFORGE_ADMIN_API_KEY`;
- run the command above;
- page on exit code `1` or `2`.

Keep the scheduler's own log of each run's exit code; the control plane keeps the outcome.

## Read the results

Each posted result is ONE audited control-plane event:

- action `companion.pitr_rehearsal`;
- resource `companion_installation` and the installation id;
- details: `rehearsal_id`, `installation_id`, `target_time`, `outcome` (`passed` or `failed`),
  `reason`, `present_found`, `absent_found`, `schema_survived`, `restored_to`, `key_revoked`.

It never carries the cipher pass, a key or a marker token.

The installation's `backup_state` also keeps:

- the newest ten rehearsals under `rehearsals`, each with the target, the tokens' SHA-256 hashes,
  the key id and, once recorded, the outcome and a redacted runner detail;
- the latest under `last_rehearsal`;
- the first time the schema was seen surviving under `rehearsal_schema_survived_at`.

A second result for the same rehearsal is refused with 409 `companion_rehearsal_already_recorded`.
A result for a rehearsal that was never planned is refused with 404.

| Reason | Meaning |
|--------|---------|
| `present_marker_missing` | The marker written before the target is not in the restore. The restore landed too early, or the backup or WAL is missing. |
| `absent_marker_found` | The marker written after the target is in the restore. The restore overshot the target. |
| `assumption_a4_schema_lost` | The `iforge_rehearsal` schema, or its oldest marker, is gone. Postiz's `prisma db push` dropped it: research assumption A4 does not hold, and markers must move elsewhere. |
| `assumption_a4_unchecked` | The installation's first rehearsal did not report on the schema. Rerun it. |
| `runner_reported_failure` | The runner failed the run itself, usually an operational error. Its detail says why. |

`key_revoked: false` means the backup provider could not be told. The key still expires within
the hour. Check that it is gone before the next run.

## Retention expiry

The runtime runs pgBackRest with `expire-auto` off, and its key cannot delete. Retention expiry is
therefore only ever the operator's act, with the operator's own delete-capable key, from an
operator host:

```bash
# An env file holding the installation's PGBACKREST_REPO1_* settings, its cipher pass
# and the OPERATOR's delete-capable key, mode 0600; never the runtime's key.
docker run --rm --env-file /path/to/operator-expire.env \
  --entrypoint pgbackrest "$(cat config/postgres-image.txt)" \
  --stanza=<stanza> --repo1-retention-full=2 expire
```

Object Lock keeps every version until its lock expires. Expiry removes backups from pgBackRest's
view, and the bucket's lifecycle rule removes their versions once their locks allow. Run expiry
after a rehearsal has passed, never instead of one.

## Revocation drill

Run this drill after the backup repository is first provisioned, and then quarterly. It proves
that a compromised runtime cannot destroy an installation's backups.

Run it on the installation's tenant host, as root, with ONLY the runtime's backup key: the
installation's backup env credential, whose key id is the `key_id` recorded by the installation's
`backup` step. Use any S3 client pointed at the backup provider's S3 endpoint. With the AWS CLI:

1. **List a version.** Pick one object under the installation's prefix and note its version id:

   ```bash
   aws s3api list-object-versions --endpoint-url "https://<s3 endpoint>" \
     --bucket "<bucket>" --prefix "<tenant id>/<installation id>/backup/" --max-items 1
   ```

2. **Try to delete that version.** The request must be refused (access denied):

   ```bash
   aws s3api delete-object --endpoint-url "https://<s3 endpoint>" \
     --bucket "<bucket>" --key "<key>" --version-id "<version id>"
   ```

3. **Hide the file.** A delete without a version id succeeds for this key: it only hides the file.

   ```bash
   aws s3api delete-object --endpoint-url "https://<s3 endpoint>" \
     --bucket "<bucket>" --key "<key>"
   ```

4. **Retrieve the hidden version.** With the operator's key, the version is still there under
   Object Lock:

   ```bash
   aws s3api get-object --endpoint-url "https://<s3 endpoint>" \
     --bucket "<bucket>" --key "<key>" --version-id "<version id>" /tmp/drill.out
   ```

5. **Restore the view.** Remove the hide marker with the operator's key, so pgBackRest sees the
   file again. Then run a rehearsal for the installation and confirm it passes.

Also confirm, after any rehearsal, that its read key is gone from the provider's key list, and
that a rehearsal key cannot write: a `put-object` with it is refused.

Record every command and response. Never record a key value. If the version delete in step 2
succeeds, the runtime key is wider than designed: revoke it at once and treat it as an incident.
