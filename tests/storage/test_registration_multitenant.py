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
"""Cross-tenant isolation tests for cached methods in ``registration.py``.

These tests prove that after the ``@cached`` → ``@tenant_cached`` swap,
lookups on the auth-critical methods are isolated per tenant. The cache
key now includes the current tenant's ``server_name``, so the same
argument value under a different tenant context re-queries the DB
instead of returning the cached value from tenant A.

The tests stub the underlying ``db_pool`` call (``runInteraction`` for
``get_user_by_access_token``, ``simple_select_one_onecol`` for
``get_user_by_external_id``, ``simple_update_one`` for
``mark_access_token_as_used``) so they can observe which tenant the DB
call executed under, without needing a real database.
"""

from synapse.config.tenants import TenantConfig
from synapse.storage.databases.main.registration import TokenLookupResult
from synapse.tenant_context import (
    get_current_tenant,
    reset_current_tenant,
    set_current_tenant,
)

from tests.unittest import HomeserverTestCase


def _tenant(name: str) -> TenantConfig:
    """Build a minimal TenantConfig for test use."""
    return TenantConfig(
        server_name=name,
        database_schema=f"tenant_{name.replace('.', '_')}",
        signing_key_path=f"/keys/{name}.key",
        media_store_path=f"/media/{name}",
    )


class TokenCacheCrossTenantIsolationTestCase(HomeserverTestCase):
    """F#1 canonical — ``get_user_by_access_token`` is tenant-scoped.

    Before the fix: the same access token looked up under tenant B would
    return the ``TokenLookupResult`` cached under tenant A (cross-tenant
    token leak). After the fix: tenant B's lookup misses the cache and
    re-queries the DB.
    """

    def prepare(self, reactor, clock, hs):
        self.store = hs.get_datastores().main

    def test_same_token_different_tenants_returns_different_results(self):
        invocations: list[str | None] = []

        async def fake_run_interaction(desc, fn, *args, **kwargs):
            tenant = get_current_tenant()
            invocations.append(tenant.server_name if tenant else None)
            if tenant and tenant.server_name == "a.test":
                return TokenLookupResult(
                    user_id="@u0:a.test",
                    token_id=1,
                    token_owner="@u0:a.test",
                    device_id="D",
                    valid_until_ms=None,
                    token_used=False,
                )
            return None

        self.store.db_pool.runInteraction = fake_run_interaction  # type: ignore[method-assign]

        a = _tenant("a.test")
        b = _tenant("b.test")

        tok_a = set_current_tenant(a)
        try:
            ra = self.get_success(self.store.get_user_by_access_token("TOK"))
        finally:
            reset_current_tenant(tok_a)

        self.assertIsNotNone(ra)
        self.assertEqual(ra.user_id, "@u0:a.test")
        self.assertEqual(invocations, ["a.test"])

        tok_b = set_current_tenant(b)
        try:
            rb = self.get_success(self.store.get_user_by_access_token("TOK"))
        finally:
            reset_current_tenant(tok_b)

        # After fix: lookup under tenant B MUST re-query (cache miss across tenants).
        self.assertIsNone(rb)
        self.assertEqual(invocations, ["a.test", "b.test"])

    def test_same_token_same_tenant_hits_cache(self):
        """Sanity check: within a tenant, the cache still works."""
        invocations: list[str | None] = []

        async def fake_run_interaction(desc, fn, *args, **kwargs):
            tenant = get_current_tenant()
            invocations.append(tenant.server_name if tenant else None)
            return TokenLookupResult(
                user_id="@u0:a.test",
                token_id=1,
                token_owner="@u0:a.test",
            )

        self.store.db_pool.runInteraction = fake_run_interaction  # type: ignore[method-assign]

        a = _tenant("a.test")
        tok = set_current_tenant(a)
        try:
            r1 = self.get_success(self.store.get_user_by_access_token("TOK"))
            r2 = self.get_success(self.store.get_user_by_access_token("TOK"))
        finally:
            reset_current_tenant(tok)

        self.assertEqual(r1, r2)
        # Only one DB call — the second lookup hit the tenant-scoped cache.
        self.assertEqual(invocations, ["a.test"])


class ExternalIdCacheCrossTenantIsolationTestCase(HomeserverTestCase):
    """``get_user_by_external_id`` must not leak MXIDs across tenants.

    Same ``(auth_provider, external_id)`` tuple can legitimately map to
    different MXIDs on different tenants. Before fix: tenant B's lookup
    returns tenant A's cached MXID.
    """

    def prepare(self, reactor, clock, hs):
        self.store = hs.get_datastores().main

    def test_same_external_id_different_tenants_returns_different_results(self):
        invocations: list[str | None] = []

        async def fake_select(*args, **kwargs):
            tenant = get_current_tenant()
            invocations.append(tenant.server_name if tenant else None)
            if tenant and tenant.server_name == "a.test":
                return "@alice:a.test"
            return None

        self.store.db_pool.simple_select_one_onecol = fake_select  # type: ignore[method-assign]

        a = _tenant("a.test")
        b = _tenant("b.test")

        tok_a = set_current_tenant(a)
        try:
            ra = self.get_success(
                self.store.get_user_by_external_id("oidc-main", "ext-42")
            )
        finally:
            reset_current_tenant(tok_a)

        self.assertEqual(ra, "@alice:a.test")
        self.assertEqual(invocations, ["a.test"])

        tok_b = set_current_tenant(b)
        try:
            rb = self.get_success(
                self.store.get_user_by_external_id("oidc-main", "ext-42")
            )
        finally:
            reset_current_tenant(tok_b)

        # After fix: lookup under tenant B MUST re-query (cache miss across tenants).
        self.assertIsNone(rb)
        self.assertEqual(invocations, ["a.test", "b.test"])


class MarkAccessTokenAsUsedCrossTenantIsolationTestCase(HomeserverTestCase):
    """``mark_access_token_as_used`` is per-tenant.

    Per-tenant ``access_tokens.id`` sequence means the same integer can
    refer to different tokens in different tenants. The cached "yes, I
    marked this" memoization must not short-circuit the DB write for
    tenant B when tenant A already marked the same token_id.
    """

    def prepare(self, reactor, clock, hs):
        self.store = hs.get_datastores().main

    def test_same_token_id_different_tenants_fires_db_write_twice(self):
        invocations: list[str | None] = []

        async def fake_update(*args, **kwargs):
            tenant = get_current_tenant()
            invocations.append(tenant.server_name if tenant else None)
            return None

        self.store.db_pool.simple_update_one = fake_update  # type: ignore[method-assign]

        a = _tenant("a.test")
        b = _tenant("b.test")

        tok_a = set_current_tenant(a)
        try:
            self.get_success(self.store.mark_access_token_as_used(42))
        finally:
            reset_current_tenant(tok_a)

        self.assertEqual(invocations, ["a.test"])

        tok_b = set_current_tenant(b)
        try:
            self.get_success(self.store.mark_access_token_as_used(42))
        finally:
            reset_current_tenant(tok_b)

        # After fix: cache did NOT suppress the second call — it fired
        # under tenant B even though tenant A already marked token_id=42.
        self.assertEqual(invocations, ["a.test", "b.test"])
