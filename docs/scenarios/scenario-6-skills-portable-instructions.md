# Scenario 6: Skills — Portable Agent Instructions

> HISTORICAL - early 2026-04 skills registry scenario. Current commands use
> `iforge skill ...`, `iforge push`, and `iforge pull`; see
> [`../using-iforge-with-claude-code.md#c-skills-portable-instructions-that-all-your-agents-share`](../using-iforge-with-claude-code.md#c-skills-portable-instructions-that-all-your-agents-share)
> and [`../cli-development.md`](../cli-development.md).

**Date:** 2026-04-15
**Context:** Skills are static config (markdown files), not MCP tools. Loaded at session start, synced via CLI.

---

## Overview

Skills are reusable, versioned instruction packages that teach agents how to do things. Think of them as portable CLAUDE.md fragments — standardized, shareable, and automatically distributed across agents via `iforge skills sync`.

---

## What Is a Skill?

A markdown file with frontmatter metadata:

```markdown
# ~/.iforge/skills/github-workflow.md
---
name: github-workflow
version: 1.2.0
description: Git branching, commit message, and PR conventions
requires:
  tools: [Bash]
  env: []
---

## Git Workflow

- Always create feature branches from main: `feat/xxx`, `fix/xxx`
- Commit messages follow Conventional Commits
- Never force push to main
- PR title under 70 chars, body includes ## Summary and ## Test Plan
...
```

It tells the agent "how to do X" — loaded into the system prompt before the session starts, not fetched at runtime.

---

## Skills vs CLAUDE.md

| | CLAUDE.md | iforge Skills |
|---|---|---|
| Scope | Per-project | Cross-project, cross-agent |
| Maintained by | You | You / community / official |
| Updates | Edit file manually | `iforge skills sync` pulls latest version |
| Sharing | Copy-paste | Subscribe + auto-distribute |
| Cross-agent | Claude Code only | Claude Code / Cursor / Codex / custom |

Skills complement CLAUDE.md — project-specific instructions stay in CLAUDE.md, reusable patterns go in Skills.

---

## Skill Sources

```bash
# 1. Official (maintained by iforge)
iforge skills search
  github-workflow     Official   v1.2.0   Git branching, commits, PR conventions
  web-search          Official   v2.0.1   Effective web search patterns
  composio            Official   v1.0.0   How to use Composio connectors
  code-review         Official   v1.1.0   Code review checklist and patterns

# 2. Community
  deploy-aws          Community  v0.9.0   AWS deployment procedures
  django-patterns     Community  v1.0.0   Django best practices
  rust-safety         Community  v0.8.0   Rust memory safety guidelines

# 3. Private (your own)
iforge skills publish ./my-skill.md --private
# → only your account can subscribe
```

---

## CLI Workflow

### Subscribe and Sync

```bash
# Subscribe to skills
iforge skills add github-workflow
iforge skills add web-search
iforge skills add composio

# Sync to local (pulls latest versions)
iforge skills sync
# → ~/.iforge/skills/github-workflow.md  (v1.2.0)
# → ~/.iforge/skills/web-search.md       (v2.0.1)
# → ~/.iforge/skills/composio.md         (v1.0.0)

# Update skills (author published new version)
iforge skills sync
# → github-workflow updated: v1.2.0 → v1.3.0

# List installed
iforge skills list
  github-workflow   v1.3.0   ✓ synced
  web-search        v2.0.1   ✓ synced
  composio          v1.0.0   ✓ synced

# Uninstall
iforge skills rm web-search
```

### Apply to Agents

Different agents load skills differently. `iforge skills apply` adapts to each:

```bash
# Claude Code — reference in CLAUDE.md
iforge skills apply --agent claude
# → appends @~/.iforge/skills/ to ~/.claude/CLAUDE.md

# Cursor — write to .cursorrules
iforge skills apply --agent cursor
# → merges skill content into .cursorrules

# Codex — write to codex instruction file
iforge skills apply --agent codex
# → writes to AGENTS.md or codex config format

# Custom agent — read files directly
# At startup: cat ~/.iforge/skills/*.md and prepend to system prompt
```

---

## Per-Project Skill Configuration

```json
// .clawdi/skills.json in project root
{
  "skills": ["github-workflow", "composio"],
  "exclude": ["web-search"]
}
```

`iforge skills sync` in this project directory only syncs the specified skills. Different projects can use different skill combinations.

---

## Scenarios

### A: Team Shared Standards

```bash
# Team lead publishes a private skill
iforge skills publish ./our-team-conventions.md --org my-team

# Team members subscribe
iforge skills add our-team-conventions
iforge skills sync

# Everyone's Claude Code / Cursor follows the same conventions
# Lead updates skill → members sync → auto-applied
```

### B: Skills + Connectors Integration

The `composio` skill teaches agents how to use Connectors:

```markdown
# composio.md skill content
## Using Connectors

When the user asks to interact with GitHub, Notion, Linear, etc.:
1. Call connector_list() to see available services
2. Call connector_call(service, action, params) to execute
3. Available services depend on user's connected accounts

Example:
  connector_call("github", "list_pull_requests", {repo: "owner/repo", state: "open"})
```

Skills teach agents the patterns. Connectors provide the actual capabilities.

### C: Auto-Sync Daemon

```bash
# Keep skills up-to-date automatically
iforge skills sync --watch
# → watches for subscription changes and version updates
# → updates local files when new versions are published

# Or as a cron job
# 0 9 * * * iforge skills sync
```

---

## Architecture

```
iforge Skills Registry (cloud)
  │
  │ iforge skills sync (pull latest versions)
  │
  ▼
~/.iforge/skills/
  ├── github-workflow.md    (v1.3.0)
  ├── web-search.md         (v2.0.1)
  └── composio.md           (v1.0.0)
  │
  │ loaded at session start (not runtime)
  │
  ├──→ Claude Code:  @~/.iforge/skills/ in CLAUDE.md
  ├──→ Cursor:       merged into .cursorrules
  ├──→ Codex:        merged into AGENTS.md
  └──→ Custom:       cat ~/.iforge/skills/*.md → system prompt
```

---

## Why Not MCP for Skills

Skills are **static instructions**, not runtime tools:
- They belong in the system prompt before the conversation starts
- Fetching a skill mid-conversation via MCP wastes context tokens
- The agent "learns" the skill at startup, not on-demand
- File-based distribution is simpler and works offline

---

## Key Principle

**Skills are portable knowledge. Write once, apply to every agent.** `iforge skills sync` is the distribution mechanism — pull from registry, write to disk, agents load at startup. No runtime dependency, no MCP overhead.
