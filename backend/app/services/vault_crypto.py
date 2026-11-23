"""AES-256-GCM encryption for vault secrets, and the vault keyring (plan 06-25).

Two paths share one cipher:

* ``encrypt`` / ``decrypt`` are the LEGACY path. They always use ``VAULT_ENCRYPTION_KEY`` --
  keyring version ``vault.v1`` -- because the tables they serve (vault items, channel tokens,
  AI provider credentials) have no column that could name another key.
* The VERSIONED path serves the tables that record a ``key_version`` (hosted runtime secrets and
  companion installation atoms): ``encrypt_with_active_key`` writes under
  ``VAULT_ACTIVE_KEY_VERSION`` and returns the version it used, and ``decrypt_with_key_version``
  reads a row with the key its own version names.

``VAULT_ENCRYPTION_KEYS`` (a JSON object of version to 64-hex key) adds versions beyond
``vault.v1``. Rotation (``vault_key_rotation``) moves an agent's versioned rows to the active
version; nothing here ever logs or returns key material.
"""

import base64
import binascii
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import VAULT_LEGACY_KEY_VERSION, settings

# Prefix used to distinguish encrypted values from legacy plaintext in JSONB columns.
_ENC_PREFIX = "enc:"

LEGACY_KEY_VERSION = VAULT_LEGACY_KEY_VERSION


class VaultKeyVersionUnavailableError(RuntimeError):
    """A row names a key version this process's keyring does not hold.

    A ``RuntimeError`` like every other keyring fault, so a caller that only knows "the vault is
    misconfigured" keeps failing closed; the operator paths that must tell "this row's key is
    gone" apart from "the vault is not configured at all" catch this subclass by name.
    """


def _get_key() -> bytes:
    """Get the encryption key from settings (hex-encoded 32 bytes)."""
    hex_key = settings.vault_encryption_key
    if not hex_key:
        raise RuntimeError("VAULT_ENCRYPTION_KEY not configured")
    key = bytes.fromhex(hex_key)
    if len(key) != 32:
        raise RuntimeError("VAULT_ENCRYPTION_KEY must be 32 bytes (64 hex chars)")
    return key


def encrypt(plaintext: str) -> tuple[bytes, bytes]:
    """Encrypt a string. Returns (ciphertext, nonce)."""
    key = _get_key()
    nonce = os.urandom(12)
    aesgcm = AESGCM(key)
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    return ciphertext, nonce


def decrypt(ciphertext: bytes, nonce: bytes) -> str:
    """Decrypt ciphertext back to string."""
    key = _get_key()
    aesgcm = AESGCM(key)
    plaintext = aesgcm.decrypt(nonce, ciphertext, None)
    return plaintext.decode("utf-8")


# ---------------------------------------------------------------------------
# The keyring.
# ---------------------------------------------------------------------------


def keyring_versions() -> tuple[str, ...]:
    """Every version this process can name, sorted: ``vault.v1`` plus ``VAULT_ENCRYPTION_KEYS``.

    ``vault.v1`` is always a member -- it IS ``VAULT_ENCRYPTION_KEY`` -- even before that key is
    configured; using it then fails loudly with "not configured", exactly as the legacy path does.
    """
    return tuple(sorted({LEGACY_KEY_VERSION, *settings.vault_encryption_keys}))


def is_keyring_member(key_version: str) -> bool:
    return key_version in keyring_versions()


def _versioned_key(key_version: str) -> bytes:
    """The 32-byte key of a NON-legacy version. Never includes key material in an error."""
    hex_key = settings.vault_encryption_keys.get(key_version)
    if hex_key is None:
        raise VaultKeyVersionUnavailableError(f"Vault key version {key_version} is not configured")
    try:
        key = bytes.fromhex(hex_key.strip())
    except ValueError:
        raise RuntimeError(f"Vault key version {key_version} is not valid hex") from None
    if len(key) != 32:
        raise RuntimeError(f"Vault key version {key_version} must be 32 bytes (64 hex chars)")
    return key


def _check_legacy_entry() -> None:
    # Startup refuses this (`Settings._validate_vault_keyring`); re-checked at use so a runtime
    # settings change cannot make the legacy and versioned paths disagree about vault.v1.
    entry = settings.vault_encryption_keys.get(LEGACY_KEY_VERSION)
    if entry is not None and entry.strip().lower() != settings.vault_encryption_key.strip().lower():
        raise RuntimeError(
            f"VAULT_ENCRYPTION_KEYS[{LEGACY_KEY_VERSION!r}] disagrees with VAULT_ENCRYPTION_KEY"
        )


def active_key_version() -> str:
    """The version new versioned writes use. Refuses one the keyring does not hold."""
    version = settings.vault_active_key_version
    if not is_keyring_member(version):
        raise VaultKeyVersionUnavailableError(
            f"VAULT_ACTIVE_KEY_VERSION {version} is not in the keyring"
        )
    return version


def encrypt_with_active_key(plaintext: str) -> tuple[bytes, bytes, str]:
    """Encrypt under the active version. Returns ``(ciphertext, nonce, key_version)``."""
    version = active_key_version()
    if version == LEGACY_KEY_VERSION:
        _check_legacy_entry()
        ciphertext, nonce = encrypt(plaintext)
        return ciphertext, nonce, version
    key = _versioned_key(version)
    nonce = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), None)
    return ciphertext, nonce, version


def decrypt_with_key_version(ciphertext: bytes, nonce: bytes, key_version: str) -> str:
    """Decrypt a versioned row with the key its own ``key_version`` names.

    Raises ``cryptography.exceptions.InvalidTag`` when that key cannot authenticate the row (it
    was lost, replaced, or the row was tampered with), ``VaultKeyVersionUnavailableError`` when
    the keyring does not hold the version, and ``RuntimeError`` when the vault is misconfigured.
    ``vault.v1`` goes through ``decrypt`` itself, so the legacy and versioned reads of a vault.v1
    row are one code path.
    """
    if key_version == LEGACY_KEY_VERSION:
        _check_legacy_entry()
        return decrypt(ciphertext, nonce)
    key = _versioned_key(key_version)
    return AESGCM(key).decrypt(nonce, ciphertext, None).decode("utf-8")


def encrypt_field(plaintext: str) -> str:
    """Encrypt a single string field into a prefixed, base64-encoded token.

    The output format is ``enc:<base64(nonce + ciphertext)>`` so that callers
    can detect whether a DB value is encrypted by checking for the prefix.
    """
    ciphertext, nonce = encrypt(plaintext)
    # Pack nonce (12 bytes) + ciphertext together so we only need one blob.
    blob = base64.b64encode(nonce + ciphertext).decode("ascii")
    return f"{_ENC_PREFIX}{blob}"


def decrypt_field(value: str) -> str:
    """Decrypt a value produced by :func:`encrypt_field`.

    If *value* does not start with the ``enc:`` prefix it is returned as-is
    (legacy plaintext pass-through for transparent migration).

    Raises :class:`ValueError` on malformed input (bad base64, truncated blob,
    or authentication failure) so callers can decide whether to 500, fall
    back, or log — without ever silently returning garbage.
    """
    if not value.startswith(_ENC_PREFIX):
        # Legacy plaintext — return unchanged so old rows keep working.
        return value
    try:
        blob = base64.b64decode(value[len(_ENC_PREFIX) :], validate=True)
    except (binascii.Error, ValueError) as e:
        raise ValueError("malformed encrypted field (invalid base64)") from e
    if len(blob) < 13:
        # 12-byte nonce + at least 1 byte of ciphertext required.
        raise ValueError("malformed encrypted field (truncated blob)")
    nonce = blob[:12]
    ciphertext = blob[12:]
    try:
        return decrypt(ciphertext, nonce)
    except Exception as e:  # AESGCM raises InvalidTag on tamper, ValueError on length
        raise ValueError("malformed encrypted field (authentication failed)") from e


def is_encrypted_field(value: str) -> bool:
    """Return True if *value* was produced by :func:`encrypt_field`."""
    return value.startswith(_ENC_PREFIX)
