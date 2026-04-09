#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2026 Linagora
#

"""
Tests for TenantRegistry.reload().

Verifies:
1. New tenants are added to the registry after reload
2. Removed tenants become inactive after reload
3. Unchanged tenants are unaffected
4. Re-adding a previously removed tenant reactivates it
5. Malformed config aborts without corrupting state
"""

from unittest import TestCase

from synapse.config.tenants import MultiTenantConfig, TenantConfig
from synapse.tenant_registry import TenantRegistry


def _tenant(name: str) -> TenantConfig:
    """Create a minimal TenantConfig for testing."""
    return TenantConfig(
        server_name=name,
        database_schema=f"tenant_{name.replace('.', '_')}",
        signing_key_path=f"/keys/{name}.key",
        media_store_path=f"/media/{name}",
    )


def _config(tenants: list[TenantConfig]) -> MultiTenantConfig:
    """Create a MultiTenantConfig from a list of TenantConfig."""
    return MultiTenantConfig(
        enabled=True,
        default_schema="public",
        tenants={t.server_name: t for t in tenants},
    )


class TenantRegistryReloadTestCase(TestCase):
    """Tests for TenantRegistry.reload()."""

    def test_reload_adds_new_tenant(self) -> None:
        """A tenant present in the new config but not the old is added."""
        registry = TenantRegistry(_config([_tenant("acme.com")]))
        self.assertIsNotNone(registry.get_tenant("acme.com"))
        self.assertIsNone(registry.get_tenant("newcorp.com"))

        new_config = _config([_tenant("acme.com"), _tenant("newcorp.com")])
        result = registry.reload(new_config)

        self.assertIn("newcorp.com", result["added"])
        self.assertIsNotNone(registry.get_tenant("newcorp.com"))

    def test_reload_removes_tenant(self) -> None:
        """A tenant absent from the new config becomes inactive."""
        registry = TenantRegistry(
            _config([_tenant("acme.com"), _tenant("corp.io")])
        )
        self.assertIsNotNone(registry.get_tenant("corp.io"))

        new_config = _config([_tenant("acme.com")])
        result = registry.reload(new_config)

        self.assertIn("corp.io", result["removed"])
        self.assertIsNone(registry.get_tenant("corp.io"))
        self.assertTrue(registry.is_inactive("corp.io"))

    def test_reload_unchanged_tenants(self) -> None:
        """Tenants present in both old and new configs stay active."""
        registry = TenantRegistry(
            _config([_tenant("acme.com"), _tenant("corp.io")])
        )

        new_config = _config([_tenant("acme.com"), _tenant("corp.io")])
        result = registry.reload(new_config)

        self.assertEqual(len(result["added"]), 0)
        self.assertEqual(len(result["removed"]), 0)
        self.assertIn("acme.com", result["unchanged"])
        self.assertIsNotNone(registry.get_tenant("acme.com"))

    def test_reload_reactivates_tenant(self) -> None:
        """Re-adding a previously removed tenant makes it active again."""
        registry = TenantRegistry(
            _config([_tenant("acme.com"), _tenant("corp.io")])
        )

        # Remove corp.io
        registry.reload(_config([_tenant("acme.com")]))
        self.assertTrue(registry.is_inactive("corp.io"))

        # Re-add corp.io
        result = registry.reload(
            _config([_tenant("acme.com"), _tenant("corp.io")])
        )
        self.assertIn("corp.io", result["added"])
        self.assertFalse(registry.is_inactive("corp.io"))
        self.assertIsNotNone(registry.get_tenant("corp.io"))

    def test_reload_malformed_config_preserves_state(self) -> None:
        """If reload() is given an empty config, all tenants become inactive."""
        acme = _tenant("acme.com")
        registry = TenantRegistry(_config([acme]))
        self.assertIsNotNone(registry.get_tenant("acme.com"))

        result = registry.reload(_config([]))
        self.assertIn("acme.com", result["removed"])
        self.assertTrue(registry.is_inactive("acme.com"))

    def test_reload_inactive_not_in_get_all_tenants(self) -> None:
        """Inactive tenants are excluded from get_all_tenants()."""
        registry = TenantRegistry(
            _config([_tenant("acme.com"), _tenant("corp.io")])
        )
        self.assertEqual(len(registry.get_all_tenants()), 2)

        registry.reload(_config([_tenant("acme.com")]))
        all_tenants = registry.get_all_tenants()
        self.assertEqual(len(all_tenants), 1)
        self.assertEqual(all_tenants[0].server_name, "acme.com")
