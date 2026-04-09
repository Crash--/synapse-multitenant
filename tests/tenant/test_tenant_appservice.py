#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2026 Linagora
#

from unittest.mock import patch

from twisted.trial import unittest

from synapse.appservice import ApplicationService
from synapse.config.tenants import TenantConfig
from synapse.types import UserID


class TenantAppServiceRegistryTestCase(unittest.TestCase):
    """Per-tenant app service isolation."""

    def _make_registry(self):
        from synapse.appservice.tenant_registry import TenantAppServiceRegistry

        tenant_a = TenantConfig(
            server_name="acme.com",
            database_schema="tenant_acme",
            signing_key_path="/tmp/acme.key",
            media_store_path="/tmp/media/acme",
            app_service_config_files=["/etc/as/acme-bridge.yaml"],
        )
        tenant_b = TenantConfig(
            server_name="corp.com",
            database_schema="tenant_corp",
            signing_key_path="/tmp/corp.key",
            media_store_path="/tmp/media/corp",
            app_service_config_files=None,
        )

        as_acme = ApplicationService(
            token="acme-token",
            id="acme-bridge",
            sender=UserID.from_string("@acme-bridge:acme.com"),
            namespaces={
                "users": [{"regex": "@irc_.*:acme.com", "exclusive": True}],
                "aliases": [],
                "rooms": [],
            },
        )

        with patch(
            "synapse.appservice.tenant_registry.load_appservices"
        ) as mock_load:
            mock_load.side_effect = lambda hostname, files: (
                [as_acme] if hostname == "acme.com" else []
            )
            registry = TenantAppServiceRegistry([tenant_a, tenant_b])

        return registry, tenant_a, tenant_b, as_acme

    def test_tenant_with_as_returns_services(self):
        registry, tenant_a, _, as_acme = self._make_registry()
        services = registry.get_app_services(tenant_a)
        self.assertEqual(len(services), 1)
        self.assertEqual(services[0].id, "acme-bridge")

    def test_tenant_without_as_returns_empty(self):
        registry, _, tenant_b, _ = self._make_registry()
        services = registry.get_app_services(tenant_b)
        self.assertEqual(services, [])

    def test_no_tenant_returns_empty(self):
        registry, _, _, _ = self._make_registry()
        services = registry.get_app_services(None)
        self.assertEqual(services, [])

    def test_get_by_user_id_correct_tenant(self):
        registry, tenant_a, _, _ = self._make_registry()
        result = registry.get_app_service_by_user_id(
            "@acme-bridge:acme.com", tenant_a
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.id, "acme-bridge")

    def test_get_by_user_id_wrong_tenant(self):
        registry, _, tenant_b, _ = self._make_registry()
        result = registry.get_app_service_by_user_id(
            "@acme-bridge:acme.com", tenant_b
        )
        self.assertIsNone(result)

    def test_get_by_token_correct_tenant(self):
        registry, tenant_a, _, _ = self._make_registry()
        result = registry.get_app_service_by_token("acme-token", tenant_a)
        self.assertIsNotNone(result)

    def test_get_by_token_wrong_tenant(self):
        registry, _, tenant_b, _ = self._make_registry()
        result = registry.get_app_service_by_token("acme-token", tenant_b)
        self.assertIsNone(result)

    def test_get_all_services_returns_union(self):
        registry, _, _, _ = self._make_registry()
        all_services = registry.get_all_app_services()
        self.assertEqual(len(all_services), 1)

    def test_interested_in_user_correct_tenant(self):
        registry, tenant_a, _, _ = self._make_registry()
        self.assertTrue(
            registry.get_if_app_services_interested_in_user(
                "@irc_nick:acme.com", tenant_a
            )
        )

    def test_interested_in_user_wrong_tenant(self):
        registry, _, tenant_b, _ = self._make_registry()
        self.assertFalse(
            registry.get_if_app_services_interested_in_user(
                "@irc_nick:acme.com", tenant_b
            )
        )
