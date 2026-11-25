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
  Any S3-compatible vendor works; the endpoint decides which. Region `auto` is
  what Cloudflare R2 expects. A vendor whose endpoint names a region may need
  that region in the request signature instead; if `check` fails with an
  authorization or region error, set `PGBACKREST_REPO1_S3_REGION` to the region
  the endpoint names.
- **Stanza.** `STANZA` and `PGBACKREST_STANZA` are both `iforge`. Changing the
  name is a backup-continuity event: it needs a new stanza and a fresh full
  backup. See [Renaming the stanza](#renaming-the-stanza).

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

## Renaming the stanza

`archive_command` reads `STANZA` and every other pgBackRest command reads
`PGBACKREST_STANZA`, both from the accessory's env. An accessory reads its env
only when it boots, so a new name in `config/deploy.yml` changes nothing until
`kamal accessory reboot postgres`. From that reboot on, `archive-push` targets
the new stanza and fails until the stanza exists, and WAL accumulates in
`pg_wal` meanwhile. The reboot also stops PostgreSQL, so cloud-api loses its
database until the accessory is back. Run every step in one window, from this
checkout, with no destination (`-d`):

1. Reboot the server so it reads the new name:

   ```bash
   kamal accessory reboot postgres
   ```

2. Create the new stanza right away, then check it:

   ```bash
   kamal accessory exec postgres -i "bash"
   # inside the container:
   gosu postgres pgbackrest --stanza="$PGBACKREST_STANZA" stanza-create
   gosu postgres pgbackrest --stanza="$PGBACKREST_STANZA" check
   exit
   ```

   `check` waits for a WAL segment to reach the repository, and the archiver
   pushes the backlog in order first. If `check` times out while it does,
   rerun it once `pg_stat_archiver.last_archived_time` is current.

3. Reboot the backup scheduler so its schedule uses the new name:

   ```bash
   kamal accessory reboot postgres-backup
   ```

4. Take a full backup now, then read the repository:

   ```bash
   kamal accessory exec postgres -i "bash"
   # inside the container:
   gosu postgres pgbackrest --stanza="$PGBACKREST_STANZA" --type=full backup
   gosu postgres pgbackrest --stanza="$PGBACKREST_STANZA" info
   ```

   Done: `info` shows a full backup in the new stanza.

5. Retire the previous stanza, in the same shell. Its name is the other stanza
   the repository holds. pgBackRest reads `PGBACKREST_STANZA` as its `stanza`
   option, so `info` lists every stanza only with that variable unset:

   ```bash
   OLD_STANZA="$(env -u PGBACKREST_STANZA gosu postgres pgbackrest info | sed -n 's/^stanza: //p' | grep -vx "$PGBACKREST_STANZA")"
   echo "previous stanza: ${OLD_STANZA:-none}"
   # only when it printed a name:
   gosu postgres pgbackrest --stanza="$OLD_STANZA" stop
   gosu postgres pgbackrest --stanza="$OLD_STANZA" --force stanza-delete
   exit
   ```

   `stanza-delete` needs `stop` for that stanza first, and `--force` because
   PostgreSQL is still running at `pg1-path` (pgBackRest command reference).
   It removes the previous stanza's backups and archive, so run it only after
   step 4 shows a full backup in the new one.

**When the previous stanza was never created.** A deployment that booted but
never ran `stanza-create` has failed `archive-push` since its first boot:
`pg_stat_archiver.failed_count` is rising and WAL is already accumulating in
`pg_wal`. Run steps 1 to 4 now. Step 5 prints `previous stanza: none`, and
there is nothing to stop or delete, so skip its last two commands. Done when
`failed_count` stops rising and `last_archived_time` is minutes old.

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
