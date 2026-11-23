# Scenario 5: Managed API Gateway

> HISTORICAL - early 2026-04 managed-gateway concept. Current AI Provider
> behavior does not proxy BYOK model traffic; see
> [`../ai-providers.md`](../ai-providers.md) and
> [`../architecture.md#ai-providers`](../architecture.md#ai-providers). Its
> `iforge run` managed-credential path was conceptual and is not the hosted
> product path; hosted tenant terminals invoke official runtime CLIs directly.

**Date:** 2026-04-15
**Context:** A managed API gateway can provide a zero-config fallback when a
user does not bring provider keys through Vault.

---

## Overview

iforge can support two provider access paths:

1. **Vault / BYOK**: the user stores provider keys in Vault, and `iforge run`
   injects them into the agent process.
2. **Managed gateway**: iforge injects a gateway token and provider-compatible
   base URL when the user has no matching Vault key.

The gateway implementation, routing provider, billing backend, and production
deployment details are outside this open-source scenario.

## User Paths

```
User needs to call LLM / search / transcription APIs
  |
  |-- Vault path
  |   User has provider keys -> Vault injects them -> provider bills user
  |
  `-- Managed path
      User has no provider key -> gateway token is injected -> iforge bills user
```

The paths are not mutually exclusive. `iforge run` can resolve each provider
independently.

## Example: New User

```bash
iforge auth login
iforge setup
iforge run -- claude
```

Expected behavior:

- no matching provider key exists in Vault;
- iforge injects a managed provider token;
- iforge sets the provider-compatible base URL;
- usage is deducted from the user's iforge balance.

## Example: BYOK User

```bash
iforge vault set ANTHROPIC_API_KEY
iforge vault set OPENAI_API_KEY
iforge run -- claude
```

Expected behavior:

- matching keys exist in Vault;
- `iforge run` injects the Vault values;
- traffic goes directly to the configured providers;
- iforge does not charge managed gateway usage for those calls.

## Direct SDK Shape

Custom scripts should use normal provider SDK configuration:

```python
import openai

client = openai.OpenAI(
    base_url="https://gateway.example.test/openai/v1",
    api_key="clawdi_..."
)
```

The concrete production URL is deployment-specific and should not be hardcoded
in open-source development docs.

## Principle

The gateway is a zero-friction fallback. Vault remains the power-user override.
Users should not need to understand internal gateway infrastructure to choose
the right path.
