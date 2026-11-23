# Moving a Live Compose Project and PostgreSQL Identity

Renaming a live PostgreSQL role and database is a data-layer migration, not a
text edit. The rename statements rewrite catalog entries inside a running
cluster, and editing a file back afterwards does not undo them: every
connection string, dump script, backup stanza and piece of automation that
named the old identity stops resolving the moment they succeed. Renaming a
Compose project is the same class of change one layer up — it detaches the
volume set your data lives in.

Nothing in this repository performs either operation, and nothing here will
start doing so. Both live in this document, as prose, for an operator who has
decided to run them — because putting a destructive statement about somebody
else's production data behind a `git pull` is not a migration strategy.

## What the repository change did — and did not do

| Kind of site | What moved | What it does to a running stack |
| --- | --- | --- |
| Parameterised defaults in the self-hosted Compose file | The value after `:-` inside each `${...}` | Nothing. A default applies only where you have not set the variable, and only a *first* start uses it: PostgreSQL reads `POSTGRES_DB` and `POSTGRES_USER` at initialisation and ignores them ever after. |
| Hardcoded values in the preview, test, CI and deployment configuration | The literal itself | Nothing to the data. Those files now *name* the new identity; the clusters they point at still carry whatever names they were created with. |

**No migration has happened.** If your stack is already running, its role and
its database are exactly as they were, and they stay that way until you
perform the procedure below. Pulling this change is not a migration — it
changes what a *new* stack would be created as, and nothing else.

## Which files

You own three things this repository cannot see or edit:

- the environment file your stack reads, which is untracked by design
- the live cluster: its roles, its databases, and their contents
- the Docker volumes your Compose project created, which are named after
  that project

The authority for *which values exist* is the Compose file itself —
[`deploy/self-host/docker-compose.yml`][self-host-compose], its `name:` line
and the `postgres` service's environment block. This runbook deliberately
does not copy those defaults into a table of its own. A copy in a document is
a second place to forget.

## The names

Fill in the three names your stack is running with today. Read them from your
own environment file, from `docker compose ls`, and from `\l` and `\du` in
`psql`:

```bash
OLD_PROJECT=
OLD_DB=
OLD_ROLE=
```

The targets are this repository's current defaults:

```bash
NEW_PROJECT=iforge-self-host
NEW_DB=iforge
NEW_ROLE=iforge
```

Refuse to proceed on an empty name, so a forgotten field fails here rather
than halfway through a rename:

```bash
: "${OLD_PROJECT:?set OLD_PROJECT above}"
: "${OLD_DB:?set OLD_DB above}"
: "${OLD_ROLE:?set OLD_ROLE above}"
PG="docker compose -p $OLD_PROJECT exec -T postgres"
```

**You may not need any of this.** Set `COMPOSE_PROJECT_NAME`, `POSTGRES_DB`
and `POSTGRES_USER` in your own environment file to the names you are already
running. Those variables override the defaults, your data stays where it is,
and the rest of this runbook becomes unnecessary. That is a supported end
state, not a workaround.

## What PostgreSQL will not let you do

Two constraints shape the whole procedure, and neither is negotiable:

- **You cannot rename the database you are connected to.** Connect to a
  different database and rename the target from there. Every cluster has a
  `postgres` database for exactly this kind of errand.
- **You cannot rename the role you are authenticated as.** Connect as a
  different superuser and rename the target from there.

The second one bites harder than it looks in a container image, where the
role named by `POSTGRES_USER` is usually the *only* superuser — so there is no
second account to connect as until you make one. Step 2 makes one.

A third rule is not a refusal but a silent consequence:

- **Renaming a role discards its password if that password is MD5-encrypted.**
  An MD5 password hash is salted with the role name, so the stored hash cannot
  survive a rename and PostgreSQL drops it rather than keeping a hash that can
  never match. Under `scram-sha-256`, the modern default, the stored verifier
  does not depend on the role name and the password is unaffected.

Check which one you have before renaming anything:

```bash
SQL="select left(rolpassword, 4) from pg_authid where rolname = '$OLD_ROLE'"
$PG psql -At -U "$OLD_ROLE" -d postgres -c "$SQL"
```

`md5` means the rename will clear the password and you must set a new one in
the same sitting. `SCRA` is `scram-sha-256` and the password survives. Empty
output means the role has no stored password at all, and nothing is lost.

Read `pg_authid`, not `pg_roles`: the view masks `rolpassword` to a fixed
string of asterisks, which cannot answer this question. `pg_authid` is
superuser-only, which is why the query above connects as the role itself.

## Step 1 — Back up. Before anything else.

Nothing past this section is undone by editing a file. Take a dump first, and
prove to yourself that it exists:

```bash
$PG pg_dumpall -U "$OLD_ROLE" > "pre-rename-$(date +%F).sql"
ls -l pre-rename-*.sql
```

A zero-length dump is a failed dump. Do not continue until the file is there
and is not empty.

Use `pg_dumpall` rather than `pg_dump`: role definitions live at cluster
level, not inside any one database, and a role is one of the two things you
are about to rename.

## Step 2 — Rename the role and the database

Stop everything except `postgres`, so nothing reconnects under the old name
while the catalog is changing:

```bash
docker compose -p "$OLD_PROJECT" stop api web migrate channels-worker
```

Both renames need a session that is neither `$OLD_ROLE` nor connected to
`$OLD_DB`. Create a temporary superuser for the job:

```bash
OLD="$PG psql -v ON_ERROR_STOP=1 -U $OLD_ROLE -d postgres"
$OLD -c "create role rename_admin superuser login"
```

That role can do anything to this cluster. It exists for the next three
commands and is dropped at the end of this step; do not leave it behind.

```bash
TMP="$PG psql -v ON_ERROR_STOP=1 -U rename_admin -d postgres"
$TMP -c "alter database \"$OLD_DB\" rename to \"$NEW_DB\""
$TMP -c "alter role \"$OLD_ROLE\" rename to \"$NEW_ROLE\""
```

If the check above printed `md5`, set the password again now — the renamed
role currently has none, and your application still expects one:

```bash
$TMP -c "alter role \"$NEW_ROLE\" password 'the-password-you-use'"
```

Then remove the temporary superuser, connecting as the renamed role:

```bash
NEW="$PG psql -v ON_ERROR_STOP=1 -U $NEW_ROLE -d $NEW_DB"
$NEW -c "drop role rename_admin"
```

Update the connection details in your own environment file to the new names
before you start the application containers again.

## Step 3 — Rename the Compose project

The project name prefixes every named volume the project creates. Change it
and Compose looks for volumes under the new prefix, finds none, **creates
empty ones**, and PostgreSQL initialises cleanly into an empty data
directory. The stack comes up green with no data in it. That is the worst
outcome available here, because nothing reports an error.

So the volumes are re-attached by hand, before the new project first starts.

Stop the old project. Without `-v` — `-v` deletes the volumes you are about
to copy:

```bash
docker compose -p "$OLD_PROJECT" down
```

List what the old project owns:

```bash
docker volume ls -q --filter "name=^${OLD_PROJECT}_"
```

Copy each one to its name under the new project. Repeat this block for every
suffix that command printed, changing `V=` each time:

```bash
V=pg_data
FROM="${OLD_PROJECT}_${V}"; TO="${NEW_PROJECT}_${V}"
docker volume create "$TO"
docker run --rm -v "$FROM":/from -v "$TO":/to alpine cp -a /from/. /to/
```

Copying rather than moving is deliberate: the old volumes stay intact and
untouched, so this step is the one part of the procedure you can walk back
from. Bring the stack up under the new project:

```bash
docker compose -p "$NEW_PROJECT" up -d
```

`-p` is spelled out for clarity. The Compose file already carries the new
project name, so a plain `docker compose up -d` from that directory does the
same thing.

Compose will warn that the volume "already exists but was not created by
Docker Compose". That warning is the copy above working as intended — it is
telling you it found a volume it did not make. Do not act on it, and in
particular do not reach for `down -v` to make it go away.

## Step 4 — Verify, and only then delete

```bash
NEWPG="docker compose -p $NEW_PROJECT exec -T postgres"
$NEWPG psql -U "$NEW_ROLE" -d "$NEW_DB" -c "\dt"
```

You want your own tables, with your own rows in them. A short list of tables
you do not recognise, or an empty one, means the new project started against
a fresh volume and you should stop and re-check step 3 rather than continue.
Exercise the application too — an empty database also serves pages.

Only once you are satisfied:

```bash
docker volume ls -q --filter "name=^${OLD_PROJECT}_" | xargs docker volume rm
```

Keep the dump from step 1 well past the point where you think you need it.

## What this does not cover

Backup archives are stored under a repository or stanza name of their own,
and renaming a database does not rename the archive it is backed up into.
This repository deliberately leaves those names alone for that reason: an
archive whose name no longer matches the tool looking for it is a restore
that fails on the day you need it. Treat backup identity as a separate
migration, under its own tool's documentation.

The same goes for any operator shortcut you keep outside this repository — a
shell alias, a saved connection, a monitoring check — that spells the old
database or role. They keep pointing at a name that no longer exists.

## Related

- [`docs/runbooks/iforge-env-migration.md`](iforge-env-migration.md) — moving
  your `.env` files to the current variable names. That one renames
  *settings*; this one renames the *data* they point at.

[self-host-compose]: ../../deploy/self-host/docker-compose.yml
