/** Canonical agent identities supported by the CLI. */
// Adding an agent: append here AND add the matching entry to `adapterRegistry`
// in `registry.ts`. `ironhermes` is the fork's local runtime (phase 02).
export const AGENT_TYPES = [
	"claude_code",
	"codex",
	"openclaw",
	"hermes",
	"pi",
	"opencode",
	"ironhermes",
] as const;
export type AgentType = (typeof AGENT_TYPES)[number];
