# Using iforge with Claude Code

End-to-end guide for connecting Claude Code to iforge Cloud and using it day-to-day. Assumes you've already set up the project (backend + web) per the root `README.md`, and that Claude Code is installed on your machine.

---

## One-time setup

### 1. Log in

```bash
$ iforge auth login
To get an API key:
  1. Go to the iforge Cloud dashboard
  2. Open user menu → API Keys
  3. Create a new key and copy it

Paste your API key: clawdi_xxxxxxxxxxxxxxxxxxxxxxxx

✓ Logged in as you@example.com
  Credentials saved to ~/.iforge/auth.json
```

Verify:

```bash
$ iforge status
iforge Cloud Status

  Auth:    ✓ logged in
  User:    you@example.com
  API:     http://localhost:8000

  Sync:    no sync history
```

### 2. Register Claude Code as an agent

```bash
$ iforge setup --agent claude_code
✓ Claude Code registered
✓ MCP server registered in Claude Code
✓ iforge skill installed in Claude Code
```

What this does:
- Creates a stable Agent identity row in the backend `AgentEnvironment` table for Claude Code
- Runs `claude mcp add-json iforge '...' --scope user` so Claude Code spawns the iforge MCP server on every launch
- Copies the bundled `iforge` skill to `~/.claude/skills/iforge/SKILL.md` so Claude Code can surface memory-retrieval guidance

Re-running `iforge setup --agent claude_code` later is safe — the skill is overwritten with the latest bundled version; the MCP registration is idempotent.

### 3. (Re)start Claude Code

Quit and reopen Claude Code so it picks up the new MCP server and skill. Inside Claude Code:

```
/mcp
```

You should see `iforge · ✔ connected` in the list. Drilling into it shows the tools it exposes: `memory_search`, `memory_create`, plus any connector tools (Gmail / GitHub / etc.) you've authorized in the dashboard.

---

## Daily workflow

### A. Memory — save things, recall them later

In a Claude Code session, ask Claude to remember something:

```
You: remember that we use pnpm, not npm, in this monorepo
```

Claude will invoke the `memory_create` tool (you'll see the call in the transcript) and store the memory in your iforge Cloud account. It's now readable from any other agent you connect.

Open a **new** Claude Code session a week later — no shared conversation state — and ask:

```
You: which package manager should I use here?
```

Claude should auto-invoke `memory_search`, pull back your stored preference, and answer with it. This works because the `iforge` skill installed during setup tells Claude to search memory at the start of any question about you, your project, or your preferences.

Inspect what's stored from the CLI anytime:

```bash
$ iforge memory list
$ iforge mem list                            # shorter alias

$ iforge memory search "package manager"     # semantic search
$ iforge mem search "my preferences"         # shorter alias

$ iforge memory add "I use pnpm instead of npm" # add from CLI directly
$ iforge memory rm <memory-id>                # delete
```

**Categories**: when Claude saves a memory it picks one of `fact` / `preference` / `pattern` / `decision` / `context`. You can filter from the CLI or the dashboard.

#### Batch extraction at end of a conversation

After a long working session it's often clearer what's worth saving than in the moment. Say "extract memories" and Claude will batch-propose up to 5 durable memories from the current conversation — **without saving any of them yet**. You confirm, then they're saved:

```
You: extract memories from this conversation

Claude: → memory_extract()
        → memory_search("package manager preference")
          [empty]
        → memory_search("auth provider decision")
          [empty]
        → memory_search("English comments convention")
          [empty]

        Found 3 candidate memories:
        1. [preference] The user prefers pnpm over npm in the iforge monorepo.
        2. [decision]   iforge chose Clerk for auth because the team already had a Clerk account.
        3. [pattern]    All code comments in iforge must be in English (per CLAUDE.md).

        Save all? Or pick (e.g. "save 1 and 3", "edit 2 to say ...", "cancel").

You: save 1 and 3

Claude: → memory_create({ content: "The user prefers pnpm over npm in the iforge monorepo.", category: "preference" })
          ✓ abc12345
        → memory_create({ content: "All code comments in iforge must be in English (per CLAUDE.md).", category: "pattern" })
          ✓ def67890

        Saved:
        - [preference] abc12345 — The user prefers pnpm over npm...
        - [pattern]    def67890 — All code comments must be in English.
```

Behavior:

- **Hard cap of 5** candidates per invocation. Even in a very long conversation Claude picks the highest-signal.
- **Dedup built in** — Claude runs `memory_search` per candidate first; anything already stored is dropped silently, so repeated calls don't grow duplicates.
- **Nothing is saved before you approve.** You can say `save all`, pick indices, rephrase (`edit 2 to say ...`), or `cancel`.
- **IDs are printed** — to undo a specific save: `iforge memory rm <id>`.

Works the same under Codex / Hermes / OpenClaw when their MCP connection to iforge is active.

### B. Vault — inject secrets at runtime without putting them on disk

Store a secret once:

```bash
$ iforge vault set OPENAI_API_KEY --prompt
Value for OPENAI_API_KEY: ••••••••••
✓ Stored OPENAI_API_KEY

$ printf '%s\n' "$STRIPE_SECRET_KEY" | iforge vault set payments/env/STRIPE_SECRET_KEY --stdin
✓ Stored payments/env/STRIPE_SECRET_KEY

$ iforge vault import --vault payments --section stripe --project personal --yes .env.stripe
✓ Imported 3 keys to vault "payments" section "stripe"
```

List what's stored (values never leave the CLI unmasked):

```bash
$ iforge vault list
  default
    OPENAI_API_KEY
    GITHUB_TOKEN
    ANTHROPIC_API_KEY
```

Run any command with the vault injected into its environment:

```bash
$ iforge run -- python app.py
✓ Injected 3 vault secrets
# ... your app runs with $OPENAI_API_KEY etc. already set
```

You can ask Claude to run the command for you. Claude uses the `Bash` tool; just tell it to prefix with `iforge run --`:

```
You: run the ingestion script with my vault secrets injected
Claude: → Bash(`iforge run -- python scripts/ingest.py`)
```

Secrets live encrypted in the backend; the web dashboard can list keys but cannot read values. Only the CLI (authenticated with an API key) can resolve them.

### C. Skills — portable instructions that all your agents share

```bash
# List skills in your iforge account
$ iforge skill list

# Commit a local directory into Claude Code's guarded Skills root, then project it
$ iforge skill add ./skills/my-review-flow --agent claude_code

# Install a public skill from GitHub (owner/repo or owner/repo/path)
$ iforge skill install anthropics/skills --agent claude_code
$ iforge skill install anthropics/skills/artifacts-builder --agent claude_code

# Remove from the authoritative local root, then remove its projection
$ iforge skill rm artifacts-builder --agent claude_code
```

For an Agent target, `add` and `install` commit validated bytes under
`~/.claude/skills/<skill-key>/` before uploading the read-only Cloud
projection. A failed local activation creates no projection. Cloud events never
write or delete these files.

Inside Claude Code, skills become available via `/skill` or when Claude decides the skill's `description` matches the task.

### D. Sessions — sync your conversations to the cloud for review

After a day of work in Claude Code:

```bash
$ iforge push
Agent:   Claude Code
Modules: sessions, skills

→ Scanning local data...

Summary
  Sessions: 12 to upload
  Skills:   4 to upload

Proceed with upload? [Y/n] y
→ Uploading 12 sessions...
  ✓ Synced 12 sessions
→ Uploading session content...
  ✓ Uploaded 10 session contents
→ Uploading 4 skills...
  ✓ Synced 4 skills

✓ Sync complete
```

Only sessions from the current project are pushed by default (uses `cwd` at invocation time). To sync everything — every module, every registered agent, every project — pass `--all`:

```bash
$ iforge push --all
```

Each axis can still be narrowed back down with the corresponding flag:

```bash
$ iforge push --modules sessions      # only sessions, skip skills
$ iforge push --agent claude_code     # only this agent (multi-agent machines)
$ iforge push --all --project ~/foo   # every module / every agent, but only this project
$ iforge push --dry-run               # preview, no uploads
```

To intentionally import a Cloud-owned workspace/personal Project Skill into an
Agent, name that source Project explicitly:

```bash
$ iforge pull --modules skills --project <project> --agent claude_code
```

After sync, open <http://localhost:3000/sessions> in the web dashboard to browse the conversations, see tokens consumed per session, re-read Claude's responses rendered as markdown, etc.

---

## Example flow from zero to useful memory

```bash
# Day 1 — bootstrap
$ iforge auth login
$ iforge setup --agent claude_code
# (restart Claude Code)

# Day 1 inside Claude Code, while coding:
You: remember I prefer rg over grep and fd over find for searching files
Claude: → memory_create({ content: "The user prefers rg over grep and fd over find...", category: "preference" })
        ✓ Memory stored (abc12345)

You: also remember we chose Clerk for auth because the team already had an account
Claude: → memory_create({ content: "We chose Clerk for auth because the team already had an account", category: "decision" })
        ✓ Memory stored (def67890)

# ... lots of work, quit Claude Code ...

# Day 7 — new session, no context carried over:
You: what's my preferred tool for searching code?
Claude: → memory_search({ query: "preferred tool for searching code" })
        [preference] The user prefers rg over grep and fd over find for searching files.
Claude: You prefer rg over grep for code search. Want me to use rg in the next command?

# Day 7, same session, a harder question:
You: why did we pick our auth provider?
Claude: → memory_search({ query: "auth provider decision" })
        [decision] We chose Clerk for auth because the team already had an account
Claude: You chose Clerk because the team already had a Clerk account.
```

All four pieces work together: `setup` wires up MCP + skill, which makes Claude's judgment biased toward calling the tools; when Claude calls them, they talk to the backend; the backend uses either FTS + vector search to retrieve; results come back as context Claude uses to answer.

---

## Running multiple agents from the same machine

If you also have Codex / Hermes / OpenClaw installed on this machine:

```bash
$ iforge setup                   # auto-detects and asks to register each one
```

Any memory you add from Claude Code is visible from every other agent you've registered, and vice-versa. `iforge push` on a multi-agent machine targets every registered agent by default and prints a one-line notice so you know:

```
Targets: claude_code, codex, hermes (use --agent to narrow)
```

Narrow to one agent with `--agent`:

```bash
$ iforge push --agent codex
```

---

## Command cheat sheet

| Command | Purpose |
|---|---|
| `iforge auth login` / `logout` / `status` | Auth and state |
| `iforge config set apiUrl <url>` | Point CLI at a non-default backend (env: `IFORGE_API_URL`) |
| `iforge setup --agent claude_code` | One-time: register Claude Code + MCP + skill |
| `iforge push` | Push sessions + skills to the cloud |
| `iforge pull` | Mirror sessions, or explicitly import Skills from a workspace/personal Project |
| `iforge memory list / search / add / rm` | Inspect or edit cross-agent memory (alias: `mem`) |
| `iforge vault set / list / import / attach / detach / rm` | Manage runtime secrets and Project access |
| `iforge run -- <cmd>` | Run a command with vault secrets injected |
| `iforge skill list / add / install / rm` | Manage skills |
| `iforge mcp` | Start MCP stdio server (invoked by Claude Code automatically) |

All subcommands support `--help`.

---

## Troubleshooting

**Claude Code doesn't call `memory_search` on an obvious question**
Quit Claude Code fully (`/exit` or Cmd+Q) and reopen. It caches MCP tool descriptions at session start; if you upgraded the CLI recently, an old cache may still be in place.

**`iforge memory list` works but Claude says "no memory found"**
Verify Claude actually called the tool — look in your Claude Code transcript for a tool-use entry. If it didn't call: try a more specific phrasing ("what's my X?", "remember Y"). If it did call and still got nothing: the stored phrasing and query may be too far apart in semantic space; check with `iforge memory search "<same query>"` directly.

**`iforge push` says "No environment registered"**
Run `iforge setup --agent claude_code` first. It creates the stable Agent identity row sync needs.

**`/mcp` in Claude Code doesn't list iforge**
Check `claude mcp list` at the shell. If `iforge` is missing, `iforge setup --agent claude_code` re-registers it.

**Memory feature works locally but dashboard shows nothing**
You're searching the right user's account? `iforge status` shows the email the CLI is logged in as; compare to the email you logged into the web dashboard with.

**`iforge setup` says "Could not auto-register MCP server"**
The `claude` binary isn't on PATH when you ran setup. Install Claude Code CLI first (ensure `claude --version` works), then re-run setup.
