"""The provisioning-step contract the companion lifecycle worker runs, and what its steps share.

Lives apart from ``companion_lifecycle_worker`` so a step module can build its ``ProvisioningStep``
without importing the worker that runs it (the worker imports the steps to fill its pipelines).
The worker re-exports every name here, so callers keep importing them from the worker.

A step's failure text becomes the job's stored error (redacted, then shown to the tenant), so a
step raises ``CompanionStepError`` with a named code and a value-free detail: never a credential,
never an operator identifier, never a response body.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from uuid import UUID

from pydantic import JsonValue
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.companion_installation import CompanionInstallationSecret
from app.services.vault_crypto import encrypt_with_active_key

JsonObject = dict[str, JsonValue]

# The random part of an installation's hostname label (``generate_hostname_label``): 12 lowercase
# RFC 4648 base32 characters. Every per-installation resource name is derived from it, so a
# retried step names -- and adopts -- exactly what an earlier attempt created.
_LABEL_SUFFIX = re.compile(r"[a-z2-7]{12}")


@dataclass(frozen=True, slots=True)
class LifecycleStepContext:
    """What a pipeline step may know about the installation it provisions.

    Identifiers and addresses only -- never a credential. A step that needs one reads it through
    ``sessionmaker`` itself, so no secret value is ever held by the worker or passed around.
    """

    installation_id: UUID
    user_id: UUID
    environment_id: UUID
    companion_key: str
    operation: str
    hostname: str
    sessionmaker: async_sessionmaker[AsyncSession]


ProvisionAction = Callable[[LifecycleStepContext], Awaitable[JsonObject | None]]
TeardownAction = Callable[[LifecycleStepContext, JsonObject], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class ProvisioningStep:
    """One ordered pipeline step and the teardown that undoes it.

    ``max_attempts`` bounds how often the worker runs a step that raises
    ``CompanionStepRetryableError`` before the operation fails; the default of one means any
    failure fails the operation at once. Between attempts the job keeps its status and is due
    again after an exponential backoff (``next_attempt_at``).
    """

    name: str
    provision: ProvisionAction
    teardown: TeardownAction
    max_attempts: int = 1


class CompanionStepError(RuntimeError):
    """A step's named refusal. Its text is what the failed job stores, so it carries no value."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code


class CompanionStepRetryableError(CompanionStepError):
    """A transient refusal (an outage, a 5xx): worth retrying up to the step's ``max_attempts``."""


def installation_label_suffix(context: LifecycleStepContext) -> str:
    """The 12-character random suffix of the installation's hostname label.

    The hostname is ``<companion>-<suffix>.<zone>`` by construction (``create_installation``).
    """
    label = context.hostname.split(".", 1)[0]
    suffix = label.rpartition("-")[2]
    if _LABEL_SUFFIX.fullmatch(suffix) is None:
        raise CompanionStepError(
            "installation_label_invalid",
            "the installation's hostname label has no 12-character random suffix",
        )
    return suffix


async def store_installation_atoms(
    context: LifecycleStepContext, values: Mapping[str, str]
) -> None:
    """Encrypt and store credential atoms for THIS installation, replacing same-named ones.

    Upserted rather than inserted: a retried step re-derives the same credential names, and the
    newest value is the one its resource now honours.
    """
    async with context.sessionmaker() as db:
        rows = (
            (
                await db.execute(
                    select(CompanionInstallationSecret).where(
                        CompanionInstallationSecret.installation_id == context.installation_id,
                        CompanionInstallationSecret.secret_name.in_(tuple(values)),
                    )
                )
            )
            .scalars()
            .all()
        )
        existing = {row.secret_name: row for row in rows}
        for secret_name, value in values.items():
            # Under the ACTIVE keyring version, which the row records (plan 06-25).
            ciphertext, nonce, key_version = encrypt_with_active_key(value)
            row = existing.get(secret_name)
            if row is None:
                db.add(
                    CompanionInstallationSecret(
                        installation_id=context.installation_id,
                        secret_name=secret_name,
                        encrypted_value=ciphertext,
                        nonce=nonce,
                        key_version=key_version,
                    )
                )
            else:
                row.encrypted_value = ciphertext
                row.nonce = nonce
                row.key_version = key_version
        await db.commit()


async def delete_installation_atoms(
    context: LifecycleStepContext, secret_names: tuple[str, ...]
) -> None:
    """Drop this installation's named atoms: the credential they held was just revoked."""
    async with context.sessionmaker() as db:
        await db.execute(
            delete(CompanionInstallationSecret).where(
                CompanionInstallationSecret.installation_id == context.installation_id,
                CompanionInstallationSecret.secret_name.in_(secret_names),
            )
        )
        await db.commit()


__all__ = [
    "CompanionStepError",
    "CompanionStepRetryableError",
    "JsonObject",
    "LifecycleStepContext",
    "ProvisionAction",
    "ProvisioningStep",
    "TeardownAction",
    "delete_installation_atoms",
    "installation_label_suffix",
    "store_installation_atoms",
]
