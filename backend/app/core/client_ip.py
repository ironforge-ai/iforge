"""The one trusted client address the per-IP rate limiters key on."""

from __future__ import annotations

from fastapi import Request

from app.core.config import settings


def trusted_client_ip(request: Request) -> str:
    """Resolve the client address a per-IP rate limit buckets on.

    With ``settings.trust_forwarded_for`` off, only the socket peer
    (``request.client.host``) counts and forwarded headers are ignored, so a
    direct-uvicorn setup cannot be header-spoofed.

    With it on, the order is:

    1. ``CF-Connecting-IP``, which Cloudflare sets from its own view of the
       connection it accepted.
    2. The rightmost ``X-Forwarded-For`` entry: the address the nearest proxy
       appended for its own peer. The leftmost entry is whatever the client
       typed (behind Cloudflare it survives verbatim at the left end of the
       chain), so it is never used; choosing it would let a client pick its
       own rate-limit bucket.
    3. The socket peer, then ``"unknown"`` when there is no client.
    """

    if settings.trust_forwarded_for:
        cf_connecting_ip = (request.headers.get("cf-connecting-ip") or "").strip()
        if cf_connecting_ip:
            return cf_connecting_ip
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            nearest = forwarded.rsplit(",", 1)[-1].strip()
            if nearest:
                return nearest
    return request.client.host if request.client else "unknown"
