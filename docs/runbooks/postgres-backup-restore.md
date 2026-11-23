# PostgreSQL Backup And Restore (Kamal Accessory)

This covers the `postgres` and `postgres-backup` accessories in
[`config/deploy.yml`](../../config/deploy.yml): PostgreSQL 18 with pgvector and
pgBackRest, archiving WAL to an S3-compatible repository. It applies to any
deployment of this config, self-hosted or not. Hosted-operator specifics (which
vendor holds the repository, which other databases share the cluster) are in the
hosted operator runbooks (iforge-hosted), not here.

## What the config does

- **Image.** One image for both accessories, pinned by digest. The authority is
  [`config/postgres-image.txt`](../../config/postgres-image.txt); `deploy.yml`
  carries a literal copy kept aligned by a parity test.
- **WAL archiving.** The server runs with `archive_mode=on`,
  `archive_command=pgbackrest --stanza=$STANZA archive-push %p` and
  `archive_timeout=60`, so a WAL segment reaches the repository at least every
  minute when anything is written.
- **Scheduled backups.** `postgres-backup` runs `supercronic` as the `postgres`
  user against `/etc/pgbackrest/backup-cron` inside the image. The config comment
  records the schedule as a full backup on Sunday and a differential daily. That
  file lives in the image, not in this repository, so confirm it before relying
  on the schedule:

  ```bash
  kamal accessory exec postgres-backup "cat /etc/pgbackrest/backup-cron"
  ```

- **Repository.** `PGBACKREST_REPO1_TYPE: s3`, path-style URIs, region `auto`,
  two full backups retained (`PGBACKREST_REPO1_RETENTION_FULL: "2"`),
  `aes-256-cbc` encryption and `zst` compression. Bucket, endpoint, path, keys
  and the cipher pass are secrets (`PGBACKREST_REPO1_*` in `.kamal/secrets`).
  Any S3-compatible vendor works; the endpoint decides which.
- **Stanza.** `STANZA` and `PGBACKREST_STANZA` are both `clawdi`. That name is a
  carry-over. Renaming it is a backup-continuity event: it needs a new stanza
  and a fresh full backup, so it is not changed in passing.

**The cipher pass cannot be recovered.** Losing `PGBACKREST_REPO1_CIPHER_PASS`
abandons every backup in the repository. Keep it in the operator's secret store,
apart from the database host. See
[`vault-key-rotation.md`](vault-key-rotation.md) for what an abandoned
repository means.

## First setup

The accessories boot with the first `kamal setup`. Create the stanza once, then
confirm the repository answers. Run pgBackRest as the `postgres` user, the same
user the backup scheduler uses:

```bash
kamal accessory exec postgres -i "bash"
# inside the container:
gosu postgres pgbackrest --stanza="$PGBACKREST_STANZA" stanza-create
gosu postgres pgbackrest --stanza="$PGBACKREST_STANZA" check
gosu postgres pgbackrest --stanza="$PGBACKREST_STANZA" info
```

Done: `check` exits 0 and `info` shows the stanza. Take the first full backup
now rather than waiting for Sunday:

```bash
gosu postgres pgbackrest --stanza="$PGBACKREST_STANZA" \
  --type=full backup
```

## Health checks

```bash
kamal accessory exec postgres -i "bash"
# inside the container:
gosu postgres pgbackrest --stanza="$PGBACKREST_STANZA" info
exit
kamal accessory logs postgres-backup
```

Done when:

- a full backup is less than 7 days old and a differential less than 1 day;
- `pg_stat_archiver.failed_count` is not rising and `last_archived_time` is
  minutes old;
- the data volume has room: a failing `archive-push` keeps WAL in `pg_wal`
  until it succeeds.

## Restore

**Backups are cluster-wide.** Every database in the cluster is in the same
backup. A point-in-time restore of the live cluster rolls all of them back
together, so it is never the way to recover one database.

### Recover one database (scratch restore, then dump)

1. Restore into a **scratch** data directory on a host that is not the live
   one, with archiving off so the scratch server cannot write to the
   repository. Use an env file holding the same `PGBACKREST_*` settings as the
   accessory, `PGBACKREST_PG1_PATH` and the secrets included (mode 0600). This
   is the pattern `backend/scripts/companion_pitr_rehearsal.py` runs:

   ```bash
   IMAGE="$(cat config/postgres-image.txt)"
   docker volume create pg-scratch
   # PGDATA must exist, owned by postgres, before the restore
   docker run --rm --user postgres \
     -v pg-scratch:/var/lib/postgresql --entrypoint mkdir \
     "$IMAGE" -p -m 0700 /var/lib/postgresql/18/docker
   docker run --rm --user postgres \
     --env-file /path/to/pgbackrest.env \
     -v pg-scratch:/var/lib/postgresql \
     --entrypoint pgbackrest "$IMAGE" \
     --stanza=<stanza> --delta --type=time \
     --target='<YYYY-MM-DD HH:MM:SS+00:00>' \
     --target-action=promote --archive-mode=off restore
   ```

   `<stanza>` is the accessory's `PGBACKREST_STANZA`. Drop `--type=time`,
   `--target` and `--target-action` to restore the latest backup.

2. Start PostgreSQL from the same image on that volume, wait for it to
   promote, then dump only the database you need:

   ```bash
   pg_dump --format=custom --no-owner \
     --dbname "<scratch DSN>" --file <db>.dump
   ```

3. Stop the writers: `kamal app stop`.
4. Restore into the live database as its owner role:

   ```bash
   pg_restore --clean --if-exists --no-owner --role <role> \
     --dbname "<live DSN>" <db>.dump
   ```

5. `kamal app boot`, then run the smoke checks in [`release.md`](release.md).
6. Remove the scratch container and `docker volume rm pg-scratch`.

### Lose the whole host

1. Build a new host and boot the `postgres` accessory's data directory empty.
2. Before the first boot of PostgreSQL, restore the latest backup into it with
   the same `pgbackrest ... restore` command, without `--archive-mode=off`, so
   archiving continues on the new primary.
3. Boot the accessory, then the app.
4. Run `pgbackrest check`, then take a fresh full backup.

## Restore drill

A backup that has never been restored is not proven. Before production traffic,
and then quarterly, run "Recover one database" end to end against a scratch
host for each database in the cluster, and record:

- the backup label restored and the target time;
- `select count(*)` from two or three tables you know, compared with the live
  database at that time;
- how long the restore took.

Never point a drill at the live data directory, and never run it with
archiving on.

## Related

- [`release.md`](release.md): deploy, rollback and production checks
- [`vault-key-rotation.md`](vault-key-rotation.md): the cipher pass and
  abandoning backups
- [`companion-restore-rehearsal.md`](companion-restore-rehearsal.md): the same
  pgBackRest restore pattern, for companion databases
