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
Tests for the tenant registry.

These tests verify that:
1. Tenant registry correctly loads and manages tenant configurations
2. Tenant lookups work correctly
3. Host header to tenant mapping functions properly
"""

from unittest import TestCase
from unittest.mock import AsyncMock, MagicMock

from twisted.trial.unittest import SynchronousTestCase

from synapse.config.tenants import MultiTenantConfig, TenantConfig
from synapse.tenant_registry import TenantNotFoundError, TenantRegistry


class TestTenantRegistry(TestCase):
    """Tests for the TenantRegistry class."""

    def _create_multi_tenant_config(
        self, tenants: list[TenantConfig]
    ) -> MultiTenantConfig:
        """Create a MultiTenantConfig with the given tenants."""
        return MultiTenantConfig(
            enabled=True,
            default_schema="public",
            tenants={t.server_name: t for t in tenants},
        )

    def _create_test_tenant(
        self, server_name: str, **kwargs
    ) -> TenantConfig:
        """Create a test tenant configuration."""
        defaults = {
            "database_schema": f"tenant_{server_name.replace('.', '_')}",
            "signing_key_path": f"/keys/{server_name}.key",
            "media_store_path": f"/media/{server_name}",
        }
        defaults.update(kwargs)
        return TenantConfig(server_name=server_name, **defaults)

    def test_empty_registry(self):
        """Test registry with no tenants."""
        config = self._create_multi_tenant_config([])
        registry = TenantRegistry(config)

        self.assertEqual(len(registry.get_all_tenants()), 0)
        self.assertFalse(registry.is_local_server_name("any.server"))

    def test_single_tenant(self):
        """Test registry with a single tenant."""
        tenant = self._create_test_tenant("acme.com")
        config = self._create_multi_tenant_config([tenant])
        registry = TenantRegistry(config)

        self.assertEqual(len(registry.get_all_tenants()), 1)
        self.assertTrue(registry.is_local_server_name("acme.com"))
        self.assertFalse(registry.is_local_server_name("other.com"))

    def test_multiple_tenants(self):
        """Test registry with multiple tenants."""
        tenants = [
            self._create_test_tenant("acme.com"),
            self._create_test_tenant("corp.io"),
            self._create_test_tenant("startup.co"),
        ]
        config = self._create_multi_tenant_config(tenants)
        registry = TenantRegistry(config)

        self.assertEqual(len(registry.get_all_tenants()), 3)

        for tenant in tenants:
            self.assertTrue(registry.is_local_server_name(tenant.server_name))

        self.assertFalse(registry.is_local_server_name("unknown.com"))

    def test_get_tenant(self):
        """Test getting a specific tenant."""
        tenants = [
            self._create_test_tenant("acme.com"),
            self._create_test_tenant("corp.io"),
        ]
        config = self._create_multi_tenant_config(tenants)
        registry = TenantRegistry(config)

        tenant = registry.get_tenant("acme.com")
        self.assertIsNotNone(tenant)
        self.assertEqual(tenant.server_name, "acme.com")

        tenant = registry.get_tenant("corp.io")
        self.assertIsNotNone(tenant)
        self.assertEqual(tenant.server_name, "corp.io")

        tenant = registry.get_tenant("unknown.com")
        self.assertIsNone(tenant)

    def test_get_tenant_or_raise(self):
        """Test get_tenant_or_raise raises exception for unknown tenant."""
        tenant = self._create_test_tenant("acme.com")
        config = self._create_multi_tenant_config([tenant])
        registry = TenantRegistry(config)

        # Should succeed
        result = registry.get_tenant_or_raise("acme.com")
        self.assertEqual(result.server_name, "acme.com")

        # Should raise
        with self.assertRaises(TenantNotFoundError) as ctx:
            registry.get_tenant_or_raise("unknown.com")

        self.assertIn("unknown.com", str(ctx.exception))

    def test_get_tenant_by_server_name(self):
        """Test getting tenant by direct server_name lookup."""
        tenants = [
            self._create_test_tenant("matrix.acme.com"),
            self._create_test_tenant("chat.corp.io"),
        ]
        config = self._create_multi_tenant_config(tenants)
        registry = TenantRegistry(config)

        # Direct lookup
        tenant = registry.get_tenant("matrix.acme.com")
        self.assertIsNotNone(tenant)
        self.assertEqual(tenant.server_name, "matrix.acme.com")

        tenant = registry.get_tenant("chat.corp.io")
        self.assertIsNotNone(tenant)
        self.assertEqual(tenant.server_name, "chat.corp.io")

        # Unknown
        tenant = registry.get_tenant("unknown.com")
        self.assertIsNone(tenant)

    def test_get_all_tenants(self):
        """Test getting all tenants."""
        tenants = [
            self._create_test_tenant("a.com"),
            self._create_test_tenant("b.com"),
            self._create_test_tenant("c.com"),
        ]
        config = self._create_multi_tenant_config(tenants)
        registry = TenantRegistry(config)

        all_tenants = registry.get_all_tenants()
        self.assertEqual(len(all_tenants), 3)

        server_names = {t.server_name for t in all_tenants}
        self.assertEqual(server_names, {"a.com", "b.com", "c.com"})

    def test_get_all_server_names(self):
        """Test getting all server names."""
        tenants = [
            self._create_test_tenant("a.com"),
            self._create_test_tenant("b.com"),
        ]
        config = self._create_multi_tenant_config(tenants)
        registry = TenantRegistry(config)

        server_names = registry.get_all_server_names()
        self.assertEqual(set(server_names), {"a.com", "b.com"})


class TestTenantRegistryWithAliases(TestCase):
    """Tests for tenant registry with host aliases."""

    def _create_test_tenant(
        self, server_name: str, **kwargs
    ) -> TenantConfig:
        """Create a test tenant configuration."""
        defaults = {
            "database_schema": f"tenant_{server_name.replace('.', '_')}",
            "signing_key_path": f"/keys/{server_name}.key",
            "media_store_path": f"/media/{server_name}",
        }
        defaults.update(kwargs)
        return TenantConfig(server_name=server_name, **defaults)

    def test_tenant_with_host_aliases(self):
        """Test tenant lookup with host aliases via add_hostname_alias."""
        tenant = self._create_test_tenant("matrix.acme.com")
        config = MultiTenantConfig(
            enabled=True,
            default_schema="public",
            tenants={tenant.server_name: tenant},
        )
        registry = TenantRegistry(config)

        # Register aliases
        registry.add_hostname_alias("acme.com", "matrix.acme.com")
        registry.add_hostname_alias("www.acme.com", "matrix.acme.com")

        # Primary name
        result = registry.get_tenant("matrix.acme.com")
        self.assertIsNotNone(result)
        self.assertEqual(result.server_name, "matrix.acme.com")

        # Aliases
        result = registry.get_tenant("acme.com")
        self.assertIsNotNone(result)
        self.assertEqual(result.server_name, "matrix.acme.com")

        result = registry.get_tenant("www.acme.com")
        self.assertIsNotNone(result)
        self.assertEqual(result.server_name, "matrix.acme.com")


class TestTenantRegistryIsolation(TestCase):
    """Tests for verifying tenant isolation properties."""

    def _create_test_tenant(
        self, server_name: str, **kwargs
    ) -> TenantConfig:
        """Create a test tenant configuration."""
        defaults = {
            "database_schema": f"tenant_{server_name.replace('.', '_')}",
            "signing_key_path": f"/keys/{server_name}.key",
            "media_store_path": f"/media/{server_name}",
        }
        defaults.update(kwargs)
        return TenantConfig(server_name=server_name, **defaults)

    def test_tenants_have_separate_schemas(self):
        """Test that each tenant has a unique database schema."""
        tenants = [
            self._create_test_tenant("a.com", database_schema="schema_a"),
            self._create_test_tenant("b.com", database_schema="schema_b"),
        ]
        config = MultiTenantConfig(
            enabled=True,
            default_schema="public",
            tenants={t.server_name: t for t in tenants},
        )
        registry = TenantRegistry(config)

        tenant_a = registry.get_tenant("a.com")
        tenant_b = registry.get_tenant("b.com")

        self.assertNotEqual(
            tenant_a.database_schema, tenant_b.database_schema
        )

    def test_tenants_have_separate_signing_keys(self):
        """Test that each tenant has a unique signing key path."""
        tenants = [
            self._create_test_tenant("a.com"),
            self._create_test_tenant("b.com"),
        ]
        config = MultiTenantConfig(
            enabled=True,
            default_schema="public",
            tenants={t.server_name: t for t in tenants},
        )
        registry = TenantRegistry(config)

        tenant_a = registry.get_tenant("a.com")
        tenant_b = registry.get_tenant("b.com")

        self.assertNotEqual(
            tenant_a.signing_key_path, tenant_b.signing_key_path
        )

    def test_tenants_have_separate_media_paths(self):
        """Test that each tenant has a unique media storage path."""
        tenants = [
            self._create_test_tenant("a.com"),
            self._create_test_tenant("b.com"),
        ]
        config = MultiTenantConfig(
            enabled=True,
            default_schema="public",
            tenants={t.server_name: t for t in tenants},
        )
        registry = TenantRegistry(config)

        tenant_a = registry.get_tenant("a.com")
        tenant_b = registry.get_tenant("b.com")

        self.assertNotEqual(
            tenant_a.media_store_path, tenant_b.media_store_path
        )


class TestLoadFromDatabase(SynchronousTestCase):
    """Tests TenantRegistry.load_from_database()."""

    def test_load_from_database_populates_registry(self):
        """Registry populates itself via the method, without an external call to reload()."""
        from twisted.internet.defer import ensureDeferred

        # Start with an empty DB-source registry (the post-startup state today)
        config = MultiTenantConfig(
            enabled=True, default_schema="public", source="database", tenants={}
        )
        registry = TenantRegistry(config)
        self.assertEqual(len(registry.get_all_tenants()), 0)

        # Fake db_pool whose runWithConnection calls the callback with a fake conn
        # whose cursor returns two tenant rows.
        fake_row_acme = {
            "server_name": "acme.com",
            "database_schema": "tenant_acme_com",
            "signing_key_data": b"ed25519 0 abc",
            "media_store_path": "/media/acme",
            "status": "active",
            "signing_key_encrypted": None,
        }
        fake_row_corp = {
            "server_name": "corp.io",
            "database_schema": "tenant_corp_io",
            "signing_key_data": b"ed25519 0 def",
            "media_store_path": "/media/corp",
            "status": "active",
            "signing_key_encrypted": None,
        }
        rows = [tuple(fake_row_acme.values()), tuple(fake_row_corp.values())]
        columns = list(fake_row_acme.keys())

        fake_cursor = MagicMock()
        fake_cursor.description = [(c,) for c in columns]
        fake_cursor.fetchall.return_value = rows

        fake_conn = MagicMock()
        fake_conn.conn.cursor.return_value = fake_cursor

        fake_db_pool = MagicMock()
        fake_db_pool.runWithConnection = AsyncMock(
            side_effect=lambda fn: fn(fake_conn)
        )

        # Run the new method through the Twisted reactor
        result = self.successResultOf(
            ensureDeferred(
                registry.load_from_database(
                    fake_db_pool, master_key=None, default_schema="public"
                )
            )
        )

        # Both tenants now present
        self.assertEqual(
            sorted(t.server_name for t in registry.get_all_tenants()),
            ["acme.com", "corp.io"],
        )
        # reload() return value propagates back
        self.assertIn("added", result)
        self.assertEqual(sorted(result["added"]), ["acme.com", "corp.io"])


class TestStartupHydration(SynchronousTestCase):
    """The helper that hydrates the registry at startup."""

    def test_hydration_helper_invokes_load_from_database_when_db_source(self):
        """When source='database', the helper calls registry.load_from_database()."""
        from synapse.tenant_registry import hydrate_registry_at_startup
        from twisted.internet.defer import ensureDeferred

        config = MultiTenantConfig(
            enabled=True, default_schema="public", source="database", tenants={}
        )
        registry = TenantRegistry(config)

        registry.load_from_database = AsyncMock(
            return_value={"added": [], "removed": [], "unchanged": []}
        )
        fake_db_pool = MagicMock()

        self.successResultOf(
            ensureDeferred(
                hydrate_registry_at_startup(
                    registry, fake_db_pool, master_key=None
                )
            )
        )
        registry.load_from_database.assert_awaited_once_with(
            fake_db_pool, None, "public"
        )

    def test_hydration_helper_noop_when_yaml_source(self):
        """When source='yaml', the helper does NOT call load_from_database."""
        from synapse.tenant_registry import hydrate_registry_at_startup
        from twisted.internet.defer import ensureDeferred

        config = MultiTenantConfig(
            enabled=True, default_schema="public", source="yaml", tenants={}
        )
        registry = TenantRegistry(config)
        registry.load_from_database = AsyncMock()
        fake_db_pool = MagicMock()

        self.successResultOf(
            ensureDeferred(
                hydrate_registry_at_startup(
                    registry, fake_db_pool, master_key=None
                )
            )
        )
        registry.load_from_database.assert_not_awaited()

    def test_hydration_helper_noop_when_disabled(self):
        """When multi-tenant disabled, the helper does NOT call load_from_database."""
        from synapse.tenant_registry import hydrate_registry_at_startup
        from twisted.internet.defer import ensureDeferred

        config = MultiTenantConfig(enabled=False)
        registry = TenantRegistry(config)
        registry.load_from_database = AsyncMock()
        fake_db_pool = MagicMock()

        self.successResultOf(
            ensureDeferred(
                hydrate_registry_at_startup(
                    registry, fake_db_pool, master_key=None
                )
            )
        )
        registry.load_from_database.assert_not_awaited()

    def test_hydration_helper_swallows_transient_db_error(self):
        """psycopg2.OperationalError is logged and swallowed; startup continues."""
        import psycopg2
        from synapse.tenant_registry import hydrate_registry_at_startup
        from twisted.internet.defer import ensureDeferred

        config = MultiTenantConfig(
            enabled=True, default_schema="public", source="database", tenants={}
        )
        registry = TenantRegistry(config)
        registry.load_from_database = AsyncMock(
            side_effect=psycopg2.OperationalError("connection refused")
        )
        fake_db_pool = MagicMock()

        with self.assertLogs("synapse.tenant_registry", level="ERROR") as cm:
            self.successResultOf(
                ensureDeferred(
                    hydrate_registry_at_startup(
                        registry, fake_db_pool, master_key=None
                    )
                )
            )
        self.assertTrue(
            any(
                "Startup hydration from database failed" in msg
                for msg in cm.output
            )
        )

    def test_hydration_helper_propagates_non_transient_errors(self):
        """Configuration/schema errors bubble up and crash startup."""
        from synapse.tenant_registry import hydrate_registry_at_startup
        from twisted.internet.defer import ensureDeferred

        config = MultiTenantConfig(
            enabled=True, default_schema="public", source="database", tenants={}
        )
        registry = TenantRegistry(config)
        registry.load_from_database = AsyncMock(
            side_effect=RuntimeError("bad master key")
        )
        fake_db_pool = MagicMock()

        failure = self.failureResultOf(
            ensureDeferred(
                hydrate_registry_at_startup(
                    registry, fake_db_pool, master_key=None
                )
            )
        )
        self.assertIsInstance(failure.value, RuntimeError)
