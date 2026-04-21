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
"""Unit tests for ``@tenant_cached`` / ``@tenant_cached_list``.

These tests exercise the tenant-aware cache decorators without spinning up a
full homeserver. We construct minimal store-like objects that carry the
`server_name` and `clock` attributes the underlying descriptor expects,
then call the decorated methods with different tenant contexts.
"""

from twisted.internet.defer import ensureDeferred
from twisted.trial.unittest import SynchronousTestCase

from synapse.config.tenants import TenantConfig
from synapse.tenant_context import (
    get_current_tenant,
    reset_current_tenant,
    set_current_tenant,
)
from synapse.util.caches.descriptors import tenant_cached, tenant_cached_list
from synapse.util.clock import Clock

from tests.server import get_clock


class _Store:
    """Minimal object with a tenant_cached method for decorator testing."""

    def __init__(self) -> None:
        self.call_count = 0
        self.server_name = "test_server"
        _, self.clock = get_clock()

    @tenant_cached()
    async def lookup(self, token: str) -> str:
        self.call_count += 1
        tenant = get_current_tenant()
        suffix = tenant.server_name if tenant else "default"
        return f"{token}@{suffix}"


class _StoreTwoArgs:
    """Store with a two-arg tenant_cached method that uses num_args=1.

    Verifies that specifying num_args=1 on a 2-arg method keys the cache
    on (tenant, first_arg) and treats the second arg as uncached data.
    """

    def __init__(self) -> None:
        self.call_count = 0
        self.server_name = "test_server"
        _, self.clock = get_clock()

    @tenant_cached(num_args=1)
    async def lookup(self, key: str, extra: str) -> str:
        self.call_count += 1
        tenant = get_current_tenant()
        suffix = tenant.server_name if tenant else "default"
        return f"{key}:{extra}@{suffix}"


class _StoreWithList:
    """Store with a tenant_cached + tenant_cached_list pair.

    Uses the same layout as Synapse storage code: a single-item
    ``_get_one`` decorated with ``@tenant_cached`` and a batch
    ``get_many`` decorated with ``@tenant_cached_list`` that points back
    at ``_get_one``.
    """

    def __init__(self) -> None:
        self.single_calls = 0
        self.batch_calls = 0
        self.last_batch_input: set[str] | None = None
        self.server_name = "test_server"
        _, self.clock = get_clock()

    @tenant_cached()
    async def _get_one(self, event_id: str) -> str:
        self.single_calls += 1
        tenant = get_current_tenant()
        suffix = tenant.server_name if tenant else "default"
        return f"{event_id}@{suffix}"

    @tenant_cached_list(cached_method_name="_get_one", list_name="event_ids")
    async def get_many(self, event_ids: list) -> dict:
        self.batch_calls += 1
        self.last_batch_input = set(event_ids)
        tenant = get_current_tenant()
        suffix = tenant.server_name if tenant else "default"
        return {eid: f"{eid}@{suffix}" for eid in event_ids}


class TenantCachedTestCase(SynchronousTestCase):
    def _tenant(self, name: str) -> TenantConfig:
        return TenantConfig(
            server_name=name,
            database_schema=f"tenant_{name.replace('.', '_')}",
            signing_key_path=f"/keys/{name}.key",
            media_store_path=f"/media/{name}",
        )

    def test_same_token_different_tenants_is_not_a_cache_hit(self) -> None:
        store = _Store()
        a = self._tenant("a.test")
        b = self._tenant("b.test")

        # Lookup under tenant A
        tok_a = set_current_tenant(a)
        try:
            ra = self.successResultOf(ensureDeferred(store.lookup("TOKEN")))
        finally:
            reset_current_tenant(tok_a)

        # Lookup under tenant B with the same token string
        tok_b = set_current_tenant(b)
        try:
            rb = self.successResultOf(ensureDeferred(store.lookup("TOKEN")))
        finally:
            reset_current_tenant(tok_b)

        # Both calls executed (cache did NOT cross tenants)
        self.assertEqual(store.call_count, 2)
        self.assertEqual(ra, "TOKEN@a.test")
        self.assertEqual(rb, "TOKEN@b.test")

    def test_repeat_within_same_tenant_hits_cache(self) -> None:
        store = _Store()
        a = self._tenant("a.test")

        tok = set_current_tenant(a)
        try:
            r1 = self.successResultOf(ensureDeferred(store.lookup("TOKEN")))
            r2 = self.successResultOf(ensureDeferred(store.lookup("TOKEN")))
        finally:
            reset_current_tenant(tok)

        self.assertEqual(store.call_count, 1)  # second call was a cache hit
        self.assertEqual(r1, r2)

    def test_no_tenant_context_uses_none_key(self) -> None:
        store = _Store()
        r1 = self.successResultOf(ensureDeferred(store.lookup("TOKEN")))
        r2 = self.successResultOf(ensureDeferred(store.lookup("TOKEN")))
        self.assertEqual(store.call_count, 1)
        self.assertEqual(r1, "TOKEN@default")
        self.assertEqual(r1, r2)

    def test_num_args_bump_accounts_for_tenant_key(self) -> None:
        """With num_args=1 on a 2-arg method, cache is keyed by
        (tenant, first_arg); varying the second arg must still hit the cache.
        """
        store = _StoreTwoArgs()
        a = self._tenant("a.test")

        tok = set_current_tenant(a)
        try:
            r1 = self.successResultOf(ensureDeferred(store.lookup("K", "X")))
            # Same first arg, different second arg: should be a cache HIT
            # because num_args=1 means only the first arg (+ tenant) is keyed.
            r2 = self.successResultOf(ensureDeferred(store.lookup("K", "Y")))
        finally:
            reset_current_tenant(tok)

        self.assertEqual(store.call_count, 1)
        # Both calls got the same cached value (the first call's result,
        # which was computed with extra="X").
        self.assertEqual(r1, "K:X@a.test")
        self.assertEqual(r2, "K:X@a.test")

    def test_num_args_bump_isolates_across_tenants(self) -> None:
        """Same test as above, but ensure the tenant key still isolates."""
        store = _StoreTwoArgs()
        a = self._tenant("a.test")
        b = self._tenant("b.test")

        tok_a = set_current_tenant(a)
        try:
            ra = self.successResultOf(ensureDeferred(store.lookup("K", "X")))
        finally:
            reset_current_tenant(tok_a)

        tok_b = set_current_tenant(b)
        try:
            rb = self.successResultOf(ensureDeferred(store.lookup("K", "X")))
        finally:
            reset_current_tenant(tok_b)

        self.assertEqual(store.call_count, 2)
        self.assertEqual(ra, "K:X@a.test")
        self.assertEqual(rb, "K:X@b.test")


class TenantCachedListTestCase(SynchronousTestCase):
    def _tenant(self, name: str) -> TenantConfig:
        return TenantConfig(
            server_name=name,
            database_schema=f"tenant_{name.replace('.', '_')}",
            signing_key_path=f"/keys/{name}.key",
            media_store_path=f"/media/{name}",
        )

    def test_batch_lookup_isolates_across_tenants(self) -> None:
        """Batch lookup under two tenants should not share entries."""
        store = _StoreWithList()
        a = self._tenant("a.test")
        b = self._tenant("b.test")

        tok_a = set_current_tenant(a)
        try:
            ra = self.successResultOf(
                ensureDeferred(store.get_many(["e1", "e2"]))
            )
        finally:
            reset_current_tenant(tok_a)

        tok_b = set_current_tenant(b)
        try:
            rb = self.successResultOf(
                ensureDeferred(store.get_many(["e1", "e2"]))
            )
        finally:
            reset_current_tenant(tok_b)

        self.assertEqual(ra, {"e1": "e1@a.test", "e2": "e2@a.test"})
        self.assertEqual(rb, {"e1": "e1@b.test", "e2": "e2@b.test"})
        # Both tenants triggered a batch fetch because cache was empty for
        # each tenant's key space.
        self.assertEqual(store.batch_calls, 2)

    def test_batch_lookup_hits_single_cache_within_tenant(self) -> None:
        """After a single-item lookup, a batch lookup reuses the cached entry."""
        store = _StoreWithList()
        a = self._tenant("a.test")

        tok = set_current_tenant(a)
        try:
            # Warm the cache via the single-item method
            r_single = self.successResultOf(
                ensureDeferred(store._get_one("e1"))
            )
            self.assertEqual(r_single, "e1@a.test")
            self.assertEqual(store.single_calls, 1)

            # Batch lookup for [e1, e2]: e1 is cached, e2 must be fetched.
            r_batch = self.successResultOf(
                ensureDeferred(store.get_many(["e1", "e2"]))
            )
        finally:
            reset_current_tenant(tok)

        self.assertEqual(r_batch, {"e1": "e1@a.test", "e2": "e2@a.test"})
        # Exactly one batch call, and it only had to fetch {e2}.
        self.assertEqual(store.batch_calls, 1)
        self.assertEqual(store.last_batch_input, {"e2"})
