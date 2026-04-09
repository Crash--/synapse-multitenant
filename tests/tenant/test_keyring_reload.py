#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2026 Linagora
#

"""
Tests for MultiTenantKeyring.reload().

Verifies:
1. Keys for new tenants are loaded after reload
2. Keys for inactive tenants are removed after reload
"""

import os
import tempfile
from unittest import TestCase
from unittest.mock import MagicMock

from signedjson.key import generate_signing_key, write_signing_keys

from synapse.config.tenants import MultiTenantConfig, TenantConfig
from synapse.crypto.multitenant_keyring import MultiTenantKeyring
from synapse.tenant_registry import TenantRegistry


def _tenant_with_key(name: str, key_dir: str) -> TenantConfig:
    """Create a TenantConfig with a real signing key on disk."""
    key_path = os.path.join(key_dir, f"{name}.key")
    key = generate_signing_key("a")
    with open(key_path, "w") as f:
        write_signing_keys(f, [key])
    return TenantConfig(
        server_name=name,
        database_schema=f"tenant_{name.replace('.', '_')}",
        signing_key_path=key_path,
        media_store_path=f"/media/{name}",
    )


def _config(tenants: list[TenantConfig]) -> MultiTenantConfig:
    return MultiTenantConfig(
        enabled=True,
        default_schema="public",
        tenants={t.server_name: t for t in tenants},
    )


class MultiTenantKeyringReloadTestCase(TestCase):
    """Tests for MultiTenantKeyring.reload()."""

    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()
        self._hs = MagicMock()

    def test_reload_loads_new_tenant_keys(self) -> None:
        """After reload, keys for newly added tenants are loaded."""
        acme = _tenant_with_key("acme.com", self._tmpdir)
        registry = TenantRegistry(_config([acme]))
        keyring = MultiTenantKeyring(self._hs, registry)

        self.assertIn("acme.com", keyring._signing_keys)

        newcorp = _tenant_with_key("newcorp.com", self._tmpdir)
        new_config = _config([acme, newcorp])
        registry.reload(new_config)
        keyring.reload(registry)

        self.assertIn("newcorp.com", keyring._signing_keys)

    def test_reload_removes_inactive_tenant_keys(self) -> None:
        """After reload, keys for inactive tenants are removed."""
        acme = _tenant_with_key("acme.com", self._tmpdir)
        corp = _tenant_with_key("corp.io", self._tmpdir)
        registry = TenantRegistry(_config([acme, corp]))
        keyring = MultiTenantKeyring(self._hs, registry)

        self.assertIn("corp.io", keyring._signing_keys)

        registry.reload(_config([acme]))
        keyring.reload(registry)

        self.assertNotIn("corp.io", keyring._signing_keys)
        self.assertIn("acme.com", keyring._signing_keys)
