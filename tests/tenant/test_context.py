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
Tests for tenant context management using contextvars.

These tests verify that:
1. Tenant context is properly isolated between concurrent operations
2. Context managers work correctly
3. Reset functions properly clear tenant state
"""

import asyncio
from unittest import TestCase

from synapse.config.tenants import TenantConfig
from synapse.tenant_context import (
    get_current_tenant,
    reset_current_tenant,
    set_current_tenant,
    tenant_context,
)


class TestTenantContext(TestCase):
    """Tests for the tenant context management."""

    def setUp(self):
        """Reset tenant context before each test."""
        reset_current_tenant()

    def tearDown(self):
        """Reset tenant context after each test."""
        reset_current_tenant()

    def _create_test_tenant(self, server_name: str) -> TenantConfig:
        """Create a test tenant configuration."""
        return TenantConfig(
            server_name=server_name,
            database_schema=f"tenant_{server_name.replace('.', '_')}",
            signing_key_path=f"/keys/{server_name}.key",
            media_store_path=f"/media/{server_name}",
        )

    def test_get_current_tenant_returns_none_when_not_set(self):
        """Test that get_current_tenant returns None when no tenant is set."""
        tenant = get_current_tenant()
        self.assertIsNone(tenant)

    def test_set_and_get_current_tenant(self):
        """Test setting and getting the current tenant."""
        tenant = self._create_test_tenant("acme.com")
        set_current_tenant(tenant)

        retrieved = get_current_tenant()
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.server_name, "acme.com")

    def test_reset_current_tenant(self):
        """Test that reset_current_tenant clears the tenant."""
        tenant = self._create_test_tenant("acme.com")
        set_current_tenant(tenant)

        reset_current_tenant()

        retrieved = get_current_tenant()
        self.assertIsNone(retrieved)

    def test_tenant_context_manager(self):
        """Test the tenant_context context manager."""
        tenant = self._create_test_tenant("corp.io")

        # Before context
        self.assertIsNone(get_current_tenant())

        # Inside context
        with tenant_context(tenant):
            retrieved = get_current_tenant()
            self.assertIsNotNone(retrieved)
            self.assertEqual(retrieved.server_name, "corp.io")

        # After context
        self.assertIsNone(get_current_tenant())

    def test_nested_tenant_contexts(self):
        """Test nested tenant contexts."""
        tenant1 = self._create_test_tenant("acme.com")
        tenant2 = self._create_test_tenant("corp.io")

        with tenant_context(tenant1):
            self.assertEqual(get_current_tenant().server_name, "acme.com")

            with tenant_context(tenant2):
                self.assertEqual(get_current_tenant().server_name, "corp.io")

            # After inner context, outer tenant should be restored
            self.assertEqual(get_current_tenant().server_name, "acme.com")

        # After all contexts
        self.assertIsNone(get_current_tenant())

    def test_context_manager_exception_handling(self):
        """Test that context is properly reset even when exception occurs."""
        tenant = self._create_test_tenant("acme.com")

        try:
            with tenant_context(tenant):
                self.assertEqual(get_current_tenant().server_name, "acme.com")
                raise ValueError("Test exception")
        except ValueError:
            pass

        # Tenant should be reset even after exception
        self.assertIsNone(get_current_tenant())


class TestTenantContextAsync(TestCase):
    """Tests for tenant context in async contexts."""

    def setUp(self):
        """Reset tenant context before each test."""
        reset_current_tenant()

    def tearDown(self):
        """Reset tenant context after each test."""
        reset_current_tenant()

    def _create_test_tenant(self, server_name: str) -> TenantConfig:
        """Create a test tenant configuration."""
        return TenantConfig(
            server_name=server_name,
            database_schema=f"tenant_{server_name.replace('.', '_')}",
            signing_key_path=f"/keys/{server_name}.key",
            media_store_path=f"/media/{server_name}",
        )

    def test_async_context_isolation(self):
        """Test that tenant context is isolated in async tasks."""

        async def run_test():
            tenant1 = self._create_test_tenant("tenant1.com")
            tenant2 = self._create_test_tenant("tenant2.com")

            results = []

            async def task_with_tenant(tenant: TenantConfig, delay: float):
                with tenant_context(tenant):
                    # Simulate some async work
                    await asyncio.sleep(delay)
                    # Verify tenant is still correct after await
                    current = get_current_tenant()
                    results.append(current.server_name if current else None)
                    return current.server_name if current else None

            # Run two tasks concurrently with different tenants
            task1 = asyncio.create_task(task_with_tenant(tenant1, 0.01))
            task2 = asyncio.create_task(task_with_tenant(tenant2, 0.005))

            r1, r2 = await asyncio.gather(task1, task2)

            # Each task should have seen its own tenant
            self.assertEqual(r1, "tenant1.com")
            self.assertEqual(r2, "tenant2.com")

        asyncio.run(run_test())

    def test_async_context_manager(self):
        """Test tenant context with async operations."""

        async def run_test():
            tenant = self._create_test_tenant("async.example.com")

            with tenant_context(tenant):
                # Do some async work
                await asyncio.sleep(0.001)

                # Tenant should still be set
                current = get_current_tenant()
                self.assertIsNotNone(current)
                self.assertEqual(current.server_name, "async.example.com")

            # After context
            self.assertIsNone(get_current_tenant())

        asyncio.run(run_test())


class TestTenantConfig(TestCase):
    """Tests for TenantConfig dataclass."""

    def test_tenant_config_creation(self):
        """Test creating a TenantConfig."""
        tenant = TenantConfig(
            server_name="example.com",
            database_schema="tenant_example",
            signing_key_path="/keys/example.key",
            media_store_path="/media/example",
        )

        self.assertEqual(tenant.server_name, "example.com")
        self.assertEqual(tenant.database_schema, "tenant_example")
        self.assertEqual(tenant.signing_key_path, "/keys/example.key")
        self.assertEqual(tenant.media_store_path, "/media/example")

    def test_tenant_config_defaults(self):
        """Test TenantConfig default values."""
        tenant = TenantConfig(
            server_name="example.com",
            database_schema="tenant_example",
            signing_key_path="/keys/example.key",
            media_store_path="/media/example",
        )

        # Check defaults
        self.assertFalse(tenant.registration_enabled)
        self.assertTrue(tenant.federation_enabled)
        self.assertIsNone(tenant.max_users)
        self.assertIsNone(tenant.max_rooms)

    def test_tenant_config_with_custom_values(self):
        """Test TenantConfig with custom values."""
        tenant = TenantConfig(
            server_name="example.com",
            database_schema="tenant_example",
            signing_key_path="/keys/example.key",
            media_store_path="/media/example",
            registration_enabled=True,
            federation_enabled=False,
            max_users=1000,
            max_rooms=500,
        )

        self.assertTrue(tenant.registration_enabled)
        self.assertFalse(tenant.federation_enabled)
        self.assertEqual(tenant.max_users, 1000)
        self.assertEqual(tenant.max_rooms, 500)

    def test_tenant_config_immutable(self):
        """Test that TenantConfig is immutable (frozen)."""
        tenant = TenantConfig(
            server_name="example.com",
            database_schema="tenant_example",
            signing_key_path="/keys/example.key",
            media_store_path="/media/example",
        )

        # Attempting to modify should raise an error
        with self.assertRaises(AttributeError):
            tenant.server_name = "other.com"

    def test_tenant_config_from_dict(self):
        """Test creating TenantConfig from dictionary."""
        data = {
            "server_name": "test.com",
            "database_schema": "tenant_test",
            "signing_key_path": "/keys/test.key",
            "media_store_path": "/media/test",
            "registration_enabled": True,
        }

        tenant = TenantConfig.from_dict(data)

        self.assertEqual(tenant.server_name, "test.com")
        self.assertEqual(tenant.database_schema, "tenant_test")
        self.assertTrue(tenant.registration_enabled)
