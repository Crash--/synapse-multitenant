#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2026 Linagora
#

"""
Tests for TenantAppServiceRegistry.reload().

Verifies:
1. New tenants get their app services loaded
2. Removed tenants have their app services cleaned up
"""

from unittest import TestCase

from synapse.appservice.tenant_registry import TenantAppServiceRegistry
from synapse.config.tenants import MultiTenantConfig, TenantConfig
from synapse.tenant_registry import TenantRegistry


def _tenant(name: str, as_files: list[str] | None = None) -> TenantConfig:
    return TenantConfig(
        server_name=name,
        database_schema=f"tenant_{name.replace('.', '_')}",
        signing_key_path=f"/keys/{name}.key",
        media_store_path=f"/media/{name}",
        app_service_config_files=as_files,
    )


def _config(tenants: list[TenantConfig]) -> MultiTenantConfig:
    return MultiTenantConfig(
        enabled=True,
        default_schema="public",
        tenants={t.server_name: t for t in tenants},
    )


class TenantAppServiceRegistryReloadTestCase(TestCase):
    """Tests for TenantAppServiceRegistry.reload()."""

    def test_reload_adds_new_tenant(self) -> None:
        """New tenant appears in the registry after reload."""
        acme = _tenant("acme.com")
        registry = TenantRegistry(_config([acme]))
        as_registry = TenantAppServiceRegistry(registry.get_all_tenants())

        self.assertIn("acme.com", as_registry._tenant_services)

        newcorp = _tenant("newcorp.com")
        new_config = _config([acme, newcorp])
        registry.reload(new_config)
        as_registry.reload(registry)

        self.assertIn("newcorp.com", as_registry._tenant_services)

    def test_reload_removes_inactive_tenant(self) -> None:
        """Inactive tenant is removed from the registry after reload."""
        acme = _tenant("acme.com")
        corp = _tenant("corp.io")
        registry = TenantRegistry(_config([acme, corp]))
        as_registry = TenantAppServiceRegistry(registry.get_all_tenants())

        self.assertIn("corp.io", as_registry._tenant_services)

        registry.reload(_config([acme]))
        as_registry.reload(registry)

        self.assertNotIn("corp.io", as_registry._tenant_services)
        self.assertIn("acme.com", as_registry._tenant_services)
