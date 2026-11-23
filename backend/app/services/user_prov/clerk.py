"""Clerk implementation of the user-provisioning provider seam (D-02).

Wraps the shared `app.services.clerk_backend` request contract
(`clerk_user_url` URL-quotes the subject; `clerk_backend_headers`
carries the secret key and the pinned Clerk-API-Version).

RESEARCH A4: delete semantics under API version 2026-05-12 are
docs-cited; the first live call is the real proof.
"""

from __future__ import annotations

import httpx

from app.services.clerk_backend import clerk_backend_headers, clerk_user_url


class ClerkUserProvisioningProvider:
    """Outbound Clerk Backend API operations for identity lifecycle.

    `transport` is an injection point for tests (httpx.MockTransport);
    production callers use the default network transport. Only invoked
    from the lifecycle path (03-08), never from request-controlled
    input (T-03-11).
    """

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    async def delete_identity(self, subject: str) -> None:
        """DELETE the Clerk user for `subject`; 404 is already-gone success.

        Raises httpx.HTTPStatusError on any other non-2xx — 03-08's
        caller relies on that failure mode to surface partial deletions.
        """
        async with httpx.AsyncClient(transport=self._transport) as client:
            response = await client.delete(
                clerk_user_url(subject),
                headers=clerk_backend_headers(),
            )
        if response.status_code == httpx.codes.NOT_FOUND:
            return
        response.raise_for_status()
