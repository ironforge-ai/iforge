"""Canonical API permission bundles for Clawdi runtimes."""

# `companions:read` / `companions:invoke` mirror the `connectors:*` pair beside them (D-09): read
# gates the companion tool list, invoke gates every companion tool call. Platform-minted runtime
# keys and runtime deployment keys both inherit them, which is what lets an install register its
# companion's tools with the Agent in the same action (WINDOWS #199). Inserted, never reordered:
# stored keys and the platform scope normalizer follow this tuple's order.
RUNTIME_MCP_SCOPES = (
    "companions:read",
    "companions:invoke",
    "connectors:read",
    "connectors:invoke",
    "memories:read",
    "memories:write",
    "projects:read",
    "sessions:read",
    "sessions:write",
    "skills:read",
    "skills:write",
    "vault:read",
    "vault:write",
)

RUNTIME_DEPLOYMENT_KEY_SCOPES = (
    *RUNTIME_MCP_SCOPES,
    "runtime-observations:write",
)
