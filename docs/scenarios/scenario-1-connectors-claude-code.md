# Scenario 1: iforge + Claude Code (CLI-First)

> HISTORICAL - early 2026-04 CLI-first scenario. Current Claude Code setup is in
> [`../using-iforge-with-claude-code.md`](../using-iforge-with-claude-code.md);
> current API/MCP compatibility is in
> [`../api-compatibility.md`](../api-compatibility.md).

**Date:** 2026-04-15
**Context:** Based on product-design.md, adopting CLI-first approach over remote MCP endpoint.

---

## Overview

Two-phase setup: `iforge setup` registers a local MCP server into Claude Code's config (one-time), then `iforge run -- claude` handles vault injection and launches the agent. No remote MCP endpoint, no per-agent token configuration.

---

## Step 0: Install and Login

```bash
# Install
brew install iforge

# Login (OAuth Device Auth, RFC 8628)
$ iforge auth login
> Visit: https://clawdi.ai/activate
> Enter code: WDJB-MJHT
> Waiting...
✓ Authorized as paco@example.com
✓ Token saved to ~/.iforge/config
```

Login happens once. Token persists in `~/.iforge/config`.

### Register MCP Server (one-time)

```bash
$ iforge setup
✓ Registered clawdi MCP server in Claude Code
  → wrote to ~/.claude.json: {"type":"stdio","command":"iforge","args":["mcp","stdio"]}
✓ Registered clawdi MCP server in Cursor
  → wrote to ~/.cursor/mcp.json
```

Under the hood, `iforge setup` calls `claude mcp add-json` to register a stdio MCP server:

```bash
# What iforge setup does internally for Claude Code:
claude mcp add-json clawdi '{"type":"stdio","command":"iforge","args":["mcp","stdio"]}' --scope user
```

This tells Claude Code: "whenever you start, spawn `iforge mcp stdio` as a local MCP server." The registration persists in `~/.claude.json` — no need to repeat.

---

## Step 1: Configure Resources in Dashboard

One-time setup at `clawdi.ai`:

```
Vault:
  ├── OPENAI_API_KEY=sk-xxx
  ├── ANTHROPIC_API_KEY=sk-ant-xxx
  └── STRIPE_SECRET_KEY=stripe-secret-placeholder

Connectors:
  ├── GitHub ✓ (OAuth authorized)
  ├── Notion ✓
  └── Linear ✓

Memory:
  └── (accumulated from previous agent sessions)

Skills:
  ├── web-search.md ✓
  └── github-workflow.md ✓
```

---

## Step 2: Sync Static Resources to Local

```bash
# Pull skills to local files
iforge skills sync
# → ~/.iforge/skills/web-search.md
# → ~/.iforge/skills/github-workflow.md

# Reference in Claude Code's CLAUDE.md
# @~/.iforge/skills/
```

Skills are static config — loaded before the session starts, not fetched at runtime.

---

## Step 3: Launch Claude Code

```bash
iforge run -- claude
```

`iforge run` only handles vault injection + exec. MCP is already configured from `iforge setup`.

```
iforge run -- claude
  │
  ├── 1. Vault Injection (Layer 2 — never enters LLM context)
  │     Fetch secrets from iforge API
  │     Set as child process env vars:
  │       OPENAI_API_KEY=sk-xxx
  │       ANTHROPIC_API_KEY=sk-ant-xxx
  │     SDK reads env vars directly. LLM context never sees raw keys.
  │
  └── 2. exec(claude)
        Launch Claude Code.
        Claude Code reads ~/.claude.json, finds registered clawdi MCP server,
        spawns "iforge mcp stdio" as a child process.
        The MCP server reads ~/.iforge/config for auth token,
        connects to iforge API, exposes tools:
          connector_list()
          connector_call(service, action, params)
          memory_search(query)
          memory_create(content)
```

### What each component is responsible for

| Component | Responsibility |
|-----------|---------------|
| `iforge setup` (one-time) | Register MCP server config in `~/.claude.json` |
| `iforge run` (each launch) | Vault env injection + exec agent |
| `iforge mcp stdio` (spawned by Claude Code) | Local MCP server, proxies connector/memory calls to iforge API |
| Claude Code | Reads MCP config, spawns MCP server, calls tools during conversation |

---

## Usage Examples

### Querying a connected service

```
User: Check open bugs in the INGEST project on Linear

Claude Code:
  → calls connector_call("linear", "list_issues",
      {project: "INGEST", status: "open", label: "bug"})
  → path: Claude Code → iforge mcp stdio → iforge API → Composio → Linear API
  → issue list returned to LLM context
  → Claude Code presents results
```

### Cross-agent memory

```
User: Save the fix for this connection pool bug

Claude Code:
  → calls memory_create("INGEST-142: connection pool leak, fixed by...")
  → stored in iforge Memory (pgvector)

--- later, in Cursor ---

$ iforge run -- cursor

User: How did we fix that connection pool issue?

Cursor:
  → calls memory_search("connection pool")
  → retrieves the memory saved from Claude Code session
  → no re-explanation needed
```

---

## Data Flow

```
┌──────────────────────────────────────────────────────────────┐
│  iforge run -- claude                                        │
│  (vault injection + exec)                                    │
│                                                              │
│  ┌─────────────────────┐    ┌─────────────────────────────┐  │
│  │ Claude Code process │    │ iforge mcp stdio            │  │
│  │                     │    │ (spawned by Claude Code     │  │
│  │ env:               │    │  from ~/.claude.json config) │  │
│  │  OPENAI_API_KEY ────┼──→ │ SDK uses directly,          │  │
│  │  ANTHROPIC_API_KEY  │    │ LLM unaware                 │  │
│  │                     │    │                             │  │
│  │ LLM tool calls: ───┼──→ │  connector_call() ──────────┼──┼──→ iforge API
│  │                     │    │  memory_search()  ──────────┼──┼──→ iforge API
│  │                     │    │  memory_create()  ──────────┼──┼──→ iforge API
│  │                     │    │                             │  │
│  │ system prompt:      │    └─────────────────────────────┘  │
│  │  @~/.iforge/skills/ │    (skills are local files,         │
│  │  (loaded at start)  │     no MCP needed)                  │
│  └─────────────────────┘                                     │
└──────────────────────────────────────────────────────────────┘
         │                              │
         │ model calls                  │ tool calls
         ▼                              ▼
   api.openai.com                 api.example.test/v1/
   (uses env key,                  connectors → Composio → GitHub/Notion/Linear
    or iforge Gateway)             memory → pgvector
```

**Note:** `iforge run` and `iforge mcp stdio` are separate processes with different responsibilities:
- `iforge run`: injects vault env vars, then exec's claude (process replaces itself)
- `iforge mcp stdio`: spawned later by Claude Code as a child process, reads auth from `~/.iforge/config`

---

## CLI-First vs Remote MCP (Design Decision)

| Aspect | Remote MCP (original design) | CLI-First (this approach) |
|--------|------------------------------|---------------------------|
| Connectors/Memory | Agent connects to `mcp.clawdi.ai` directly | Local MCP server, proxies to API |
| Auth | Each agent needs its own token | `iforge auth login` once, CLI manages tokens |
| Network dependency | Agent must reach mcp.clawdi.ai | Local IPC, only API calls go over network |
| Agent config | Fill URL+token in each agent's MCP config | `iforge setup` once, then `iforge run -- <agent>` |
| Offline | Not possible | Memory can have local cache, partial offline support |

---

## Key Principle

**First-time: `iforge auth login && iforge setup`. Daily use: `iforge run -- claude`.**

Setup registers the MCP server once. After that, `iforge run` only handles vault injection — Claude Code spawns the MCP server automatically on every launch.
