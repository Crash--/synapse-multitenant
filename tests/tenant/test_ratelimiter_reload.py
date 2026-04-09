#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2026 Linagora
#

"""
Tests for TenantRatelimiterRegistry.reload().

Verifies:
1. Cached limiters for removed tenants are cleared after reload
2. Existing tenant limiters survive reload
"""

from unittest import TestCase
from unittest.mock import MagicMock

from synapse.api.tenant_ratelimiting import TenantRatelimiterRegistry
from synapse.config.ratelimiting import RatelimitSettings
from synapse.config.tenants import (
    MultiTenantConfig,
    TenantConfig,
    TenantRatelimitConfig,
)
from synapse.tenant_registry import TenantRegistry


def _tenant(
    name: str, ratelimit: TenantRatelimitConfig | None = None
) -> TenantConfig:
    return TenantConfig(
        server_name=name,
        database_schema=f"tenant_{name.replace('.', '_')}",
        signing_key_path=f"/keys/{name}.key",
        media_store_path=f"/media/{name}",
        ratelimit=ratelimit,
    )


def _config(tenants: list[TenantConfig]) -> MultiTenantConfig:
    return MultiTenantConfig(
        enabled=True,
        default_schema="public",
        tenants={t.server_name: t for t in tenants},
    )


class TenantRatelimiterRegistryReloadTestCase(TestCase):

    def _make_registry(self) -> TenantRatelimiterRegistry:
        store = MagicMock()
        clock = MagicMock()
        global_settings = {
            "rc_message": RatelimitSettings(
                key="rc_message", per_second=0.2, burst_count=10
            ),
        }
        return TenantRatelimiterRegistry(
            store=store, clock=clock, global_settings=global_settings
        )

    def test_reload_clears_removed_tenant_limiters(self) -> None:
        """Cached limiters for removed tenants are evicted."""
        rl = TenantRatelimitConfig(
            rc_message=RatelimitSettings(
                key="rc_message", per_second=1.0, burst_count=5
            )
        )
        acme = _tenant("acme.com", ratelimit=rl)
        corp = _tenant("corp.io", ratelimit=rl)
        registry = TenantRegistry(_config([acme, corp]))
        rl_registry = self._make_registry()

        # Populate cache
        rl_registry.get("rc_message", tenant=acme)
        rl_registry.get("rc_message", tenant=corp)
        self.assertIn(("acme.com", "rc_message"), rl_registry._cache)
        self.assertIn(("corp.io", "rc_message"), rl_registry._cache)

        # Remove corp.io
        registry.reload(_config([acme]))
        rl_registry.reload(registry)

        self.assertNotIn(("corp.io", "rc_message"), rl_registry._cache)
        self.assertIn(("acme.com", "rc_message"), rl_registry._cache)

    def test_reload_preserves_active_tenant_limiters(self) -> None:
        """Limiters for active tenants survive reload."""
        rl = TenantRatelimitConfig(
            rc_message=RatelimitSettings(
                key="rc_message", per_second=1.0, burst_count=5
            )
        )
        acme = _tenant("acme.com", ratelimit=rl)
        registry = TenantRegistry(_config([acme]))
        rl_registry = self._make_registry()

        limiter_before = rl_registry.get("rc_message", tenant=acme)

        registry.reload(_config([acme]))
        rl_registry.reload(registry)

        limiter_after = rl_registry.get("rc_message", tenant=acme)
        self.assertIs(limiter_before, limiter_after)
