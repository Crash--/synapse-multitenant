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
"""Cross-tenant isolation tests for event-keyed caches (F#1 Group B).

These tests cover the 10 ``@cached`` / ``@cachedList`` methods in the
four storage files listed in ``docs/multi_tenant/cache_audit.md``
(entries #13-#16, #18-#21, #25-#26) whose cache keys were only opaque
event IDs. After the ``@tenant_cached`` / ``@tenant_cached_list`` swap,
the same ``event_id`` under a different tenant context must miss the
cache and re-query the DB.

Each ``@cachedList`` shares the paired single-item cache, so we cover
both entry points with one test per pair. Three of the paired
single-item methods (``_get_user_id_from_membership_event_id``,
``_get_membership_from_event_id``, ``get_event_reference_hash``) have
a ``NotImplementedError`` body and are only populated via their paired
``@cachedList`` — we test those through the batch method, which
exercises the same (now tenant-scoped) ``DeferredCache``.

Stubs replace only the specific ``db_pool`` primitive each method
actually calls so we can record which tenant the DB call fired under
without needing a real database.
"""

from synapse.config.tenants import TenantConfig
from synapse.tenant_context import (
    get_current_tenant,
    reset_current_tenant,
    set_current_tenant,
)

from tests.unittest import HomeserverTestCase


def _tenant(name: str) -> TenantConfig:
    """Build a minimal ``TenantConfig`` for test use."""
    return TenantConfig(
        server_name=name,
        database_schema=f"tenant_{name.replace('.', '_')}",
        signing_key_path=f"/keys/{name}.key",
        media_store_path=f"/media/{name}",
    )


class UserIdFromMembershipEventIdCrossTenantTestCase(HomeserverTestCase):
    """F#1 — ``_get_user_id_from_membership_event_id`` is tenant-scoped.

    The paired single-item cache is populated via
    ``_get_user_ids_from_membership_event_ids`` (the batch method body
    calls ``simple_select_many_batch``). Testing the batch method
    exercises the shared tenant-scoped cache.
    """

    def prepare(self, reactor, clock, hs):
        self.store = hs.get_datastores().main

    def test_cross_tenant_isolation(self):
        invocations: list[str | None] = []

        async def fake_select_many(*args, **kwargs):
            tenant = get_current_tenant()
            invocations.append(tenant.server_name if tenant else None)
            if tenant and tenant.server_name == "a.test":
                return [("$evt1", "@u0:a.test")]
            return []

        self.store.db_pool.simple_select_many_batch = fake_select_many  # type: ignore[method-assign]

        a, b = _tenant("a.test"), _tenant("b.test")

        tok_a = set_current_tenant(a)
        try:
            ra = self.get_success(
                self.store._get_user_ids_from_membership_event_ids(["$evt1"])
            )
        finally:
            reset_current_tenant(tok_a)

        self.assertEqual(ra, {"$evt1": "@u0:a.test"})
        self.assertEqual(invocations, ["a.test"])

        tok_b = set_current_tenant(b)
        try:
            rb = self.get_success(
                self.store._get_user_ids_from_membership_event_ids(["$evt1"])
            )
        finally:
            reset_current_tenant(tok_b)

        # After fix: tenant B MUST re-query (cache miss across tenants).
        self.assertEqual(rb, {"$evt1": None})
        self.assertEqual(invocations, ["a.test", "b.test"])


class MembershipFromEventIdCrossTenantTestCase(HomeserverTestCase):
    """F#1 — ``_get_membership_from_event_id`` is tenant-scoped.

    Paired with the batch ``get_membership_from_event_ids``. The
    single-item method body is ``NotImplementedError`` — the cache is
    populated only via the batch call, so we exercise that.
    """

    def prepare(self, reactor, clock, hs):
        self.store = hs.get_datastores().main

    def test_cross_tenant_isolation(self):
        invocations: list[str | None] = []

        async def fake_select_many(*args, **kwargs):
            tenant = get_current_tenant()
            invocations.append(tenant.server_name if tenant else None)
            if tenant and tenant.server_name == "a.test":
                return [("@u0:a.test", "join", "$evt1")]
            return []

        self.store.db_pool.simple_select_many_batch = fake_select_many  # type: ignore[method-assign]

        a, b = _tenant("a.test"), _tenant("b.test")

        tok_a = set_current_tenant(a)
        try:
            ra = self.get_success(
                self.store.get_membership_from_event_ids(["$evt1"])
            )
        finally:
            reset_current_tenant(tok_a)

        self.assertIn("$evt1", ra)
        self.assertIsNotNone(ra["$evt1"])
        self.assertEqual(ra["$evt1"].user_id, "@u0:a.test")
        self.assertEqual(invocations, ["a.test"])

        tok_b = set_current_tenant(b)
        try:
            rb = self.get_success(
                self.store.get_membership_from_event_ids(["$evt1"])
            )
        finally:
            reset_current_tenant(tok_b)

        # After fix: tenant B MUST re-query (cache miss across tenants).
        self.assertEqual(rb, {"$evt1": None})
        self.assertEqual(invocations, ["a.test", "b.test"])


class EventReferenceHashCrossTenantTestCase(HomeserverTestCase):
    """F#1 — ``get_event_reference_hash`` is tenant-scoped.

    Paired with the batch ``get_event_reference_hashes``. The
    single-item method body is ``NotImplementedError`` — the cache is
    populated only via the batch call, which in turn calls
    ``get_events``. We stub ``get_events`` to observe tenant context.
    """

    def prepare(self, reactor, clock, hs):
        self.store = hs.get_datastores().main

    def test_cross_tenant_isolation(self):
        invocations: list[str | None] = []

        async def fake_get_events(event_ids, **kwargs):
            tenant = get_current_tenant()
            invocations.append(tenant.server_name if tenant else None)
            # Return an empty dict: the batch method will record {} as
            # the reference hash for each requested event id. What
            # matters for the cross-tenant check is that the DB call
            # fires twice (once per tenant), not its value.
            return {}

        self.store.get_events = fake_get_events  # type: ignore[method-assign]

        a, b = _tenant("a.test"), _tenant("b.test")

        tok_a = set_current_tenant(a)
        try:
            self.get_success(self.store.get_event_reference_hashes(["$evt1"]))
        finally:
            reset_current_tenant(tok_a)

        self.assertEqual(invocations, ["a.test"])

        tok_b = set_current_tenant(b)
        try:
            self.get_success(self.store.get_event_reference_hashes(["$evt1"]))
        finally:
            reset_current_tenant(tok_b)

        # After fix: tenant B MUST re-query (cache miss across tenants).
        self.assertEqual(invocations, ["a.test", "b.test"])


class IsPartialStateEventCrossTenantTestCase(HomeserverTestCase):
    """F#1 — ``is_partial_state_event`` is tenant-scoped.

    Unlike the other paired single-item caches, this method has a real
    body calling ``simple_select_one_onecol``. We test the single-item
    method directly; the paired ``get_partial_state_events`` batch
    shares the same tenant-scoped cache.
    """

    def prepare(self, reactor, clock, hs):
        self.store = hs.get_datastores().main

    def test_cross_tenant_isolation(self):
        invocations: list[str | None] = []

        async def fake_select(*args, **kwargs):
            tenant = get_current_tenant()
            invocations.append(tenant.server_name if tenant else None)
            if tenant and tenant.server_name == "a.test":
                return 1
            return None

        self.store.db_pool.simple_select_one_onecol = fake_select  # type: ignore[method-assign]

        a, b = _tenant("a.test"), _tenant("b.test")

        tok_a = set_current_tenant(a)
        try:
            ra = self.get_success(self.store.is_partial_state_event("$evt1"))
        finally:
            reset_current_tenant(tok_a)

        self.assertTrue(ra)
        self.assertEqual(invocations, ["a.test"])

        tok_b = set_current_tenant(b)
        try:
            rb = self.get_success(self.store.is_partial_state_event("$evt1"))
        finally:
            reset_current_tenant(tok_b)

        # After fix: tenant B MUST re-query (cache miss across tenants).
        self.assertFalse(rb)
        self.assertEqual(invocations, ["a.test", "b.test"])


class StateGroupForEventCrossTenantTestCase(HomeserverTestCase):
    """F#1 — ``_get_state_group_for_event`` is tenant-scoped.

    The method body calls ``simple_select_one_onecol`` on
    ``event_to_state_groups``. State groups are per-tenant; the same
    ``event_id`` could refer to different state groups in different
    tenant schemas.
    """

    def prepare(self, reactor, clock, hs):
        self.store = hs.get_datastores().main

    def test_cross_tenant_isolation(self):
        invocations: list[str | None] = []

        async def fake_select(*args, **kwargs):
            tenant = get_current_tenant()
            invocations.append(tenant.server_name if tenant else None)
            if tenant and tenant.server_name == "a.test":
                return 42
            return 99

        self.store.db_pool.simple_select_one_onecol = fake_select  # type: ignore[method-assign]

        a, b = _tenant("a.test"), _tenant("b.test")

        tok_a = set_current_tenant(a)
        try:
            ra = self.get_success(self.store._get_state_group_for_event("$evt1"))
        finally:
            reset_current_tenant(tok_a)

        self.assertEqual(ra, 42)
        self.assertEqual(invocations, ["a.test"])

        tok_b = set_current_tenant(b)
        try:
            rb = self.get_success(self.store._get_state_group_for_event("$evt1"))
        finally:
            reset_current_tenant(tok_b)

        # After fix: tenant B MUST re-query (cache miss across tenants).
        self.assertEqual(rb, 99)
        self.assertEqual(invocations, ["a.test", "b.test"])
