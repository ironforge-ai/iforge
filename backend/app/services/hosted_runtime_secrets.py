from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Iterable
from uuid import UUID

from cryptography.exceptions import InvalidTag
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.hosted_runtime import HostedRuntimeSecret
from app.schemas.runtime import HostedRuntimeSecretValues
from app.services.vault_crypto import (
    LEGACY_KEY_VERSION,
    decrypt_with_key_version,
    encrypt_with_active_key,
    is_keyring_member,
)

# The version every row was written under before the keyring (plan 06-25): the legacy
# `VAULT_ENCRYPTION_KEY`. New writes use the ACTIVE keyring version, not this constant.
HOSTED_RUNTIME_SECRET_KEY_VERSION = LEGACY_KEY_VERSION
_IDEMPOTENCY_KEY_DERIVATION_DOMAIN = b"clawdi.hosted-runtime-secret-values.idempotency-key.v1"

# The stored value of a row whose ciphertext the configured vault key cannot authenticate
# (`InvalidTag`: the key was lost or rotated away, or the row was tampered with). It is a bare
# `object()` so it can never compare equal to any desired plaintext string: an unreadable row is
# always "changed", which is what lets the upsert that supplies a new value REPLACE it instead of
# failing on the decrypt it used to need (D-10, WINDOWS #200).
_UNREADABLE = object()


def runtime_secret_values_idempotency_identity(values: HostedRuntimeSecretValues) -> str:
    """Return a server-keyed identity safe to persist with idempotency metadata."""

    key_hex = settings.vault_encryption_key
    if not key_hex:
        raise RuntimeError("VAULT_ENCRYPTION_KEY not configured")
    key = bytes.fromhex(key_hex)
    if len(key) != 32:
        raise RuntimeError("VAULT_ENCRYPTION_KEY must be 32 bytes (64 hex chars)")
    idempotency_key = hmac.new(
        key,
        _IDEMPOTENCY_KEY_DERIVATION_DOMAIN,
        hashlib.sha256,
    ).digest()
    material = json.dumps(
        _plaintext_secret_values(values),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return hmac.new(idempotency_key, material, hashlib.sha256).hexdigest()


async def load_hosted_runtime_secrets_for_update(
    db: AsyncSession,
    *,
    environment_id: UUID,
) -> list[HostedRuntimeSecret]:
    return list(
        (
            await db.execute(
                select(HostedRuntimeSecret)
                .where(HostedRuntimeSecret.environment_id == environment_id)
                .order_by(HostedRuntimeSecret.secret_ref)
                .with_for_update()
                # A row lock is only meaningful against the version it locked: refresh any
                # instance this session already holds, so a caller that read the rows before
                # waiting on the lock (the admin recover route) sees what a concurrent writer
                # committed rather than its own stale copy.
                .execution_options(populate_existing=True)
            )
        ).scalars()
    )


def hosted_runtime_secret_values_changed(
    rows: Iterable[HostedRuntimeSecret],
    desired: HostedRuntimeSecretValues,
) -> bool:
    return _decrypted_secret_values(rows) != _plaintext_secret_values(desired)


def validate_hosted_runtime_secret_key_version(key_version: str) -> None:
    """A stored row's version must be a keyring member (plan 06-25): ``vault.v1`` or a
    ``VAULT_ENCRYPTION_KEYS`` entry. Shared by hosted runtime secrets and companion atoms."""
    if not is_keyring_member(key_version):
        raise RuntimeError(f"Unsupported hosted runtime secret key version: {key_version}")


async def sync_hosted_runtime_secret_values(
    db: AsyncSession,
    *,
    environment_id: UUID,
    rows: Iterable[HostedRuntimeSecret],
    desired: HostedRuntimeSecretValues,
) -> None:
    desired_plaintext = _plaintext_secret_values(desired)
    existing = {row.secret_ref: row for row in rows}
    for secret_ref, plaintext in sorted(desired_plaintext.items()):
        row = existing.pop(secret_ref, None)
        if row is not None and _stored_value_or_unreadable(row) == plaintext:
            continue
        ciphertext, nonce, key_version = encrypt_with_active_key(plaintext)
        if row is None:
            db.add(
                HostedRuntimeSecret(
                    environment_id=environment_id,
                    secret_ref=secret_ref,
                    encrypted_value=ciphertext,
                    nonce=nonce,
                    key_version=key_version,
                )
            )
            continue
        row.encrypted_value = ciphertext
        row.nonce = nonce
        row.key_version = key_version
    for row in existing.values():
        await db.delete(row)


def _plaintext_secret_values(values: HostedRuntimeSecretValues) -> dict[str, str]:
    return {secret_ref: value.get_secret_value() for secret_ref, value in values.items()}


def unreadable_hosted_runtime_secret_refs(rows: Iterable[HostedRuntimeSecret]) -> list[str]:
    """Sorted refs whose stored ciphertext the configured vault key cannot authenticate.

    Returns refs only -- never a value -- so callers can report or act on the broken rows
    without handling any secret material.
    """
    return sorted(row.secret_ref for row in rows if _stored_value_or_unreadable(row) is _UNREADABLE)


def _stored_value_or_unreadable(row: HostedRuntimeSecret) -> str | object:
    """The row's plaintext, or ``_UNREADABLE`` when its ciphertext fails authentication.

    Only ``InvalidTag`` is treated as unreadable. A missing or malformed vault key, or an
    unsupported ``key_version``, raises ``RuntimeError``: that is a configuration fault of this
    process, not a property of the row, and swallowing it would let a misconfigured control plane
    overwrite every secret it cannot read.
    """
    validate_hosted_runtime_secret_key_version(row.key_version)
    try:
        return decrypt_with_key_version(row.encrypted_value, row.nonce, row.key_version)
    except InvalidTag:
        return _UNREADABLE


def _decrypted_secret_values(rows: Iterable[HostedRuntimeSecret]) -> dict[str, str | object]:
    return {row.secret_ref: _stored_value_or_unreadable(row) for row in rows}
