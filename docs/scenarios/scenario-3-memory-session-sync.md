# Scenario 3: Memory — Claude Code Session Sync

> HISTORICAL - early 2026-04 memory extraction scenario. Current memory behavior
> is explicit user/agent writes through MCP or CLI; see
> [`../architecture.md#memory`](../architecture.md#memory) and
> [`../using-iforge-with-claude-code.md`](../using-iforge-with-claude-code.md).

**Date:** 2026-04-15
**Context:** Memory is Layer 1 (LLM-facing). Cross-agent recall via `memory_search` MCP tool.

---

## Overview

iforge Memory extracts structured knowledge from your Claude Code sessions and makes it searchable from any agent. Raw transcripts are never uploaded — only LLM-extracted facts, decisions, and debugging insights.

---

## The Problem

- You fixed a Postgres connection pool bug last week in Claude Code. Today you hit a similar issue in Cursor — but Cursor has no idea what happened in Claude Code.
- You made an architecture decision with Claude Code 3 days ago. Now you can't remember the reasoning.
- You switch machines. Your local Claude Code memory (`~/.claude/.../memory/`) doesn't follow you.

---

## Data Source: Claude Code Sessions

Claude Code stores sessions as JSONL files:

```
~/.claude/projects/{project-path}/
  ├── 06b03a78-...jsonl       # one file per session
  ├── 99b6e29d-...jsonl       # contains full conversation: user/assistant/system/tool_use
  └── ...
```

Each JSONL record contains:
- `type`: user / assistant / system / progress / file-history-snapshot
- `message.content`: conversation text or tool_use
- `timestamp`, `sessionId`, `cwd`, `gitBranch`

Raw sessions are large and noisy (largest observed: 16MB, 907 messages). They cannot be stored directly — valuable memories must be extracted first.

---

## Extraction Flow

```
iforge memory sync --source claude-code
  │
  ├── 1. Scan ~/.claude/projects/
  │     Find un-synced session files (tracked by sync watermark)
  │
  ├── 2. Filter: keep only user + assistant text messages
  │     Skip: tool_use details, file-history-snapshot, progress
  │     Skip: sessions shorter than 3 rounds
  │
  ├── 3. Send to iforge API for LLM extraction
  │     POST /v1/memory/extract
  │     {
  │       "source": "claude_code",
  │       "session_id": "06b03a78-...",
  │       "project": "iforge",
  │       "messages": [...condensed...],
  │       "git_branch": "main"
  │     }
  │
  │     Server-side extraction (Haiku or similar small model):
  │     - What decisions were made, and why
  │     - What bugs were fixed, root cause
  │     - Technical details learned
  │     - User preferences and patterns
  │
  ├── 4. Extracted memories → embed with text-embedding-3-small → store in pgvector
  │
  └── 5. Record sync watermark to ~/.iforge/sync_state.json
        Prevents re-processing
```

---

## CLI Commands

### Syncing Sessions

```bash
# Sync all un-synced Claude Code sessions
iforge memory sync --source claude-code

# Only sync current project
iforge memory sync --source claude-code --project .

# Only sync recent sessions
iforge memory sync --source claude-code --since 7d

# Preview what would be extracted (no upload)
iforge memory sync --source claude-code --dry-run

# Check sync status
iforge memory status
  claude-code:
    projects: 12
    sessions synced: 87 / 156
    last sync: 2h ago
    memories extracted: 243
```

### Managing Memories

```bash
# List all memories
iforge memory list

# Semantic search
iforge memory search "postgres connection pool"

# Delete single entry
iforge memory rm <id>

# Delete all memories from a source
iforge memory rm --source claude-code

# Delete everything
iforge memory rm --all

# Export (data portability)
iforge memory export > memories.json

# Manual add
iforge memory add "Deploy requires --force flag on staging because of X"
```

---

## Cross-Agent Recall Example

```
Day 1: Claude Code session (auto-synced)

  User: Fix the connection pool exhaustion error
  Claude Code: [investigates, fixes, explains]

  iforge memory sync extracts:
  ┌──────────────────────────────────────────────────────┐
  │ "Fixed Postgres connection pool exhaustion by setting │
  │  max_connections=20 in asyncpg pool and adding        │
  │  connection timeout of 30s. Root cause: missing       │
  │  pool.release() in error paths."                      │
  │  tags: [postgres, asyncpg, connection-pool, bugfix]   │
  │  source: claude_code / session 06b03a78               │
  │  project: iforge                                      │
  └──────────────────────────────────────────────────────┘

Day 3: Cursor session (different agent, same iforge account)

  User: I'm seeing database connection errors again

  Cursor → iforge mcp stdio → memory_search("database connection error")
  → Returns the memory from Day 1
  → Cursor knows the prior fix without user re-explaining
```

---

## Privacy and Security

**Default behavior:**
- `iforge memory sync` is **user-initiated**, not a background daemon
- Raw conversations are **never uploaded** — only extracted structured memories
- All sources are OFF by default, must be explicitly specified with `--source`
- Extraction happens server-side; condensed messages are sent over HTTPS, processed, then discarded

**What is NOT stored:**
- Raw transcripts (too noisy, too expensive)
- Code diffs (belongs in git)
- Tool use details (file reads, bash commands)
- Secrets or env vars (belongs in vault)
- CLAUDE.md contents (already loaded at session start)

**User control:**
- Browse, search, delete individual entries in CLI or Dashboard
- Per-source opt-in toggles in Dashboard
- `iforge memory export` for data portability
- `iforge memory rm --all` for right-to-be-forgotten

---

## iforge Memory vs Claude Code Built-in Memory

| | Claude Code Memory (`~/.claude/.../memory/`) | iforge Memory |
|---|---|---|
| Storage | Local markdown files | iforge cloud (pgvector) |
| Cross-device | No | Yes, via iforge account |
| Cross-agent | Claude Code only | Any agent with iforge MCP |
| Search | File name matching | Semantic vector search |
| Sources | Current agent only | Multiple agents aggregated |
| Trigger | Auto (Claude Code decides what to save) | User-initiated sync + manual add |

---

## Key Principle

**Memory sync is explicit and opt-in.** The user controls what gets extracted, from which sources, and can inspect/delete everything. No background surveillance of agent sessions.
