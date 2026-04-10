#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2026 Linagora
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#
#

"""
AES-256-GCM encryption for tenant signing keys.

Tenant signing keys are stored encrypted in the ``public.tenants`` table.
This module provides encrypt/decrypt using a master key from the
``SYNAPSE_TENANT_KEY_MASTER`` environment variable (base64-encoded 32 bytes).

Wire format of the ``signing_key_encrypted`` BYTEA column::

    IV (12 bytes) || ciphertext || GCM auth tag (16 bytes)

The same scheme is implemented in TypeScript in the control plane service
(``docker-demo/control-plane/src/services/key-manager.ts``).
"""

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def get_master_key_from_env() -> bytes:
    """Read the tenant key master from the environment.

    Returns:
        The 32-byte master key.

    Raises:
        RuntimeError: If the environment variable is not set or invalid.
    """
    raw = os.environ.get("SYNAPSE_TENANT_KEY_MASTER")
    if not raw:
        raise RuntimeError(
            "SYNAPSE_TENANT_KEY_MASTER environment variable is not set"
        )
    try:
        key = base64.b64decode(raw)
    except Exception as e:
        raise RuntimeError(
            f"SYNAPSE_TENANT_KEY_MASTER is not valid base64: {e}"
        ) from e
    if len(key) != 32:
        raise RuntimeError(
            f"SYNAPSE_TENANT_KEY_MASTER must decode to 32 bytes, got {len(key)}"
        )
    return key


def encrypt_signing_key(key_text: str, master_key: bytes) -> bytes:
    """Encrypt a signing key string with AES-256-GCM.

    Args:
        key_text: The signing key in signedjson text format
            (e.g. ``"ed25519 a_acme <base64seed>"``).
        master_key: 32-byte AES key.

    Returns:
        ``IV (12 bytes) || ciphertext || tag (16 bytes)`` as a single
        ``bytes`` object suitable for storing in a BYTEA column.
    """
    iv = os.urandom(12)
    aesgcm = AESGCM(master_key)
    # AESGCM.encrypt appends the 16-byte tag to the ciphertext
    ct_with_tag = aesgcm.encrypt(iv, key_text.encode("utf-8"), None)
    return iv + ct_with_tag


def decrypt_signing_key(encrypted: bytes, master_key: bytes) -> str:
    """Decrypt a signing key previously encrypted with ``encrypt_signing_key``.

    Args:
        encrypted: The raw bytes from the ``signing_key_encrypted`` column
            (``IV || ciphertext || tag``).
        master_key: 32-byte AES key (same as used for encryption).

    Returns:
        The plaintext signing key string.

    Raises:
        cryptography.exceptions.InvalidTag: If the data was tampered with
            or the wrong master key was used.
    """
    iv = encrypted[:12]
    ct_with_tag = encrypted[12:]
    aesgcm = AESGCM(master_key)
    plaintext = aesgcm.decrypt(iv, ct_with_tag, None)
    return plaintext.decode("utf-8")
