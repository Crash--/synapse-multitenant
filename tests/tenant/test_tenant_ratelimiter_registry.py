#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2026 Linagora
#

from unittest.mock import Mock

from twisted.trial import unittest

from synapse.api.ratelimiting import Ratelimiter
from synapse.config.ratelimiting import RatelimitSettings
from synapse.config.tenants import TenantConfig, TenantRatelimitConfig


class TenantRatelimiterRegistryTestCase(unittest.TestCase):
    """TenantRatelimiterRegistry returns per-tenant Ratelimiter instances."""

    def _make_registry(self):
        from synapse.api.tenant_ratelimiting import TenantRatelimiterRegistry

        store = Mock()
        store.get_ratelimit_for_user = Mock(return_value=None)
        clock = Mock()
        clock.time.return_value = 0.0
        clock.looping_call = Mock()

        global_settings = {
            "rc_message": RatelimitSettings(
                key="rc_message", per_second=0.2, burst_count=10
            ),
        }
        return TenantRatelimiterRegistry(store, clock, global_settings)

    def test_no_tenant_returns_global_limiter(self):
        registry = self._make_registry()
        limiter = registry.get("rc_message", tenant=None)
        self.assertIsInstance(limiter, Ratelimiter)
        self.assertAlmostEqual(limiter.rate_hz, 0.2)
        self.assertEqual(limiter.burst_count, 10)

    def test_tenant_without_ratelimit_returns_global(self):
        registry = self._make_registry()
        tenant = TenantConfig(
            server_name="acme.com",
            database_schema="tenant_acme",
            signing_key_path="/tmp/acme.key",
            media_store_path="/tmp/media/acme",
            ratelimit=None,
        )
        limiter = registry.get("rc_message", tenant=tenant)
        self.assertAlmostEqual(limiter.rate_hz, 0.2)

    def test_tenant_with_override_returns_tenant_limiter(self):
        registry = self._make_registry()
        tenant = TenantConfig(
            server_name="corp.com",
            database_schema="tenant_corp",
            signing_key_path="/tmp/corp.key",
            media_store_path="/tmp/media/corp",
            ratelimit=TenantRatelimitConfig(
                rc_message=RatelimitSettings(
                    key="rc_message", per_second=1.0, burst_count=50
                ),
            ),
        )
        limiter = registry.get("rc_message", tenant=tenant)
        self.assertAlmostEqual(limiter.rate_hz, 1.0)
        self.assertEqual(limiter.burst_count, 50)

    def test_tenant_override_one_key_global_for_another(self):
        registry = self._make_registry()
        tenant = TenantConfig(
            server_name="corp.com",
            database_schema="tenant_corp",
            signing_key_path="/tmp/corp.key",
            media_store_path="/tmp/media/corp",
            ratelimit=TenantRatelimitConfig(
                rc_message=RatelimitSettings(
                    key="rc_message", per_second=5.0, burst_count=100
                ),
            ),
        )
        msg_limiter = registry.get("rc_message", tenant=tenant)
        self.assertAlmostEqual(msg_limiter.rate_hz, 5.0)

    def test_same_tenant_same_key_returns_cached_instance(self):
        registry = self._make_registry()
        tenant = TenantConfig(
            server_name="acme.com",
            database_schema="tenant_acme",
            signing_key_path="/tmp/acme.key",
            media_store_path="/tmp/media/acme",
        )
        limiter1 = registry.get("rc_message", tenant=tenant)
        limiter2 = registry.get("rc_message", tenant=tenant)
        self.assertIs(limiter1, limiter2)

    def test_different_tenants_get_different_instances(self):
        registry = self._make_registry()
        tenant_a = TenantConfig(
            server_name="acme.com",
            database_schema="tenant_acme",
            signing_key_path="/tmp/acme.key",
            media_store_path="/tmp/media/acme",
            ratelimit=TenantRatelimitConfig(
                rc_message=RatelimitSettings(
                    key="rc_message", per_second=0.5, burst_count=20
                ),
            ),
        )
        tenant_b = TenantConfig(
            server_name="corp.com",
            database_schema="tenant_corp",
            signing_key_path="/tmp/corp.key",
            media_store_path="/tmp/media/corp",
            ratelimit=TenantRatelimitConfig(
                rc_message=RatelimitSettings(
                    key="rc_message", per_second=2.0, burst_count=80
                ),
            ),
        )
        limiter_a = registry.get("rc_message", tenant=tenant_a)
        limiter_b = registry.get("rc_message", tenant=tenant_b)
        self.assertIsNot(limiter_a, limiter_b)
        self.assertAlmostEqual(limiter_a.rate_hz, 0.5)
        self.assertAlmostEqual(limiter_b.rate_hz, 2.0)
