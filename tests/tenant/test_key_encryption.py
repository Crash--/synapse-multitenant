#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2026 Linagora
#

"""Tests for AES-256-GCM tenant signing key encryption."""

import base64
import os

from twisted.trial import unittest

from synapse.crypto.tenant_key_encryption import (
    decrypt_signing_key,
    encrypt_signing_key,
    get_master_key_from_env,
)


class EncryptDecryptRoundTripTestCase(unittest.TestCase):
    """Verify encrypt → decrypt produces the original key text."""

    def setUp(self):
        self.master_key = os.urandom(32)
        self.key_text = "ed25519 a_acme dGVzdGtleWRhdGFoZXJl"

    def test_round_trip(self):
        encrypted = encrypt_signing_key(self.key_text, self.master_key)
        decrypted = decrypt_signing_key(encrypted, self.master_key)
        self.assertEqual(decrypted, self.key_text)

    def test_different_ivs(self):
        """Two encryptions of the same plaintext should produce different ciphertext."""
        enc1 = encrypt_signing_key(self.key_text, self.master_key)
        enc2 = encrypt_signing_key(self.key_text, self.master_key)
        self.assertNotEqual(enc1, enc2)

    def test_wrong_key_fails(self):
        encrypted = encrypt_signing_key(self.key_text, self.master_key)
        wrong_key = os.urandom(32)
        from cryptography.exceptions import InvalidTag
        self.assertRaises(InvalidTag, decrypt_signing_key, encrypted, wrong_key)

    def test_tampered_data_fails(self):
        encrypted = encrypt_signing_key(self.key_text, self.master_key)
        # Flip a byte in the ciphertext
        tampered = bytearray(encrypted)
        tampered[15] ^= 0xFF
        tampered = bytes(tampered)
        from cryptography.exceptions import InvalidTag
        self.assertRaises(InvalidTag, decrypt_signing_key, tampered, self.master_key)

    def test_wire_format_iv_12_bytes(self):
        encrypted = encrypt_signing_key(self.key_text, self.master_key)
        # First 12 bytes are IV, rest is ciphertext + 16-byte tag
        self.assertGreater(len(encrypted), 12 + 16)

    def test_unicode_key_text(self):
        """Key text with non-ASCII should round-trip correctly."""
        key_text = "ed25519 a_test ABCDëfgh"
        encrypted = encrypt_signing_key(key_text, self.master_key)
        decrypted = decrypt_signing_key(encrypted, self.master_key)
        self.assertEqual(decrypted, key_text)


class GetMasterKeyFromEnvTestCase(unittest.TestCase):
    """Test master key loading from environment."""

    def test_valid_key(self):
        key = os.urandom(32)
        encoded = base64.b64encode(key).decode()
        os.environ["SYNAPSE_TENANT_KEY_MASTER"] = encoded
        try:
            result = get_master_key_from_env()
            self.assertEqual(result, key)
        finally:
            del os.environ["SYNAPSE_TENANT_KEY_MASTER"]

    def test_missing_env_raises(self):
        os.environ.pop("SYNAPSE_TENANT_KEY_MASTER", None)
        self.assertRaises(RuntimeError, get_master_key_from_env)

    def test_wrong_length_raises(self):
        os.environ["SYNAPSE_TENANT_KEY_MASTER"] = base64.b64encode(b"short").decode()
        try:
            self.assertRaises(RuntimeError, get_master_key_from_env)
        finally:
            del os.environ["SYNAPSE_TENANT_KEY_MASTER"]

    def test_invalid_base64_raises(self):
        os.environ["SYNAPSE_TENANT_KEY_MASTER"] = "not-valid-base64!!!"
        try:
            self.assertRaises(RuntimeError, get_master_key_from_env)
        finally:
            del os.environ["SYNAPSE_TENANT_KEY_MASTER"]
