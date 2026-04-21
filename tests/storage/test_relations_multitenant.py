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
"""Cross-tenant isolation tests for relations/thread caches (F#1 Group C).

These tests cover the 8 ``@cached`` / ``@cachedList`` methods in
``synapse/storage/databases/main/relations.py`` whose cache keys were
only opaque event IDs (``docs/multi_tenant/cache_audit.md`` #27-#34).
After the ``@tenant_cached`` / ``@tenant_cached_list`` swap, the same
``event_id`` under a different tenant context must miss the cache and
re-query the DB.

Three of the five unique single-item methods are paired with a
``@cachedList`` that shares the same tenant-scoped cache:

  * ``get_references_for_event``        paired with ``get_references_for_events``
  * ``get_applicable_edit``             paired with ``get_applicable_edits``
  * ``get_thread_summary``              paired with ``get_thread_summaries``

The other two are standalone:

  * ``get_thread_id``
  * ``get_thread_id_for_receipts``

All three paired single-item methods have ``NotImplementedError`` bodies
— their caches are only populated via the paired batch method. The two
standalone methods have real bodies calling ``db_pool.runInteraction``.

Stubs replace only the specific primitive each method actually calls
(``db_pool.runInteraction`` or ``db_pool.simple_select_one_onecol``) so
we can record which tenant the DB call fired under without needing a
real database.
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


class ReferencesForEventCrossTenantTestCase(HomeserverTestCase):
    """F#1 — ``get_references_for_event`` is tenant-scoped.

    The single-item method body is ``NotImplementedError``; the cache
    is populated only via the paired batch method
    ``get_references_for_events``, which calls ``db_pool.runInteraction``.
    We exercise the batch entry point to cover the shared
    tenant-scoped cache.
    """

    def prepare(self, reactor, clock, hs):
        self.store = hs.get_datastores().main

    def test_cross_tenant_isolation(self):
        invocations: list[str | None] = []

        async def fake_run_interaction(desc, func, *args, **kwargs):
            tenant = get_current_tenant()
            invocations.append(tenant.server_name if tenant else None)
            # Return an empty mapping: the batch method will map
            # every requested event id to ``None``. What matters for
            # the cross-tenant check is that the DB call fires twice
            # (once per tenant), not its value.
            return {}

        self.store.db_pool.runInteraction = fake_run_interaction  # type: ignore[method-assign]

        a, b = _tenant("a.test"), _tenant("b.test")

        tok_a = set_current_tenant(a)
        try:
            self.get_success(self.store.get_references_for_events(["$evt1"]))
        finally:
            reset_current_tenant(tok_a)

        self.assertEqual(invocations, ["a.test"])

        tok_b = set_current_tenant(b)
        try:
            self.get_success(self.store.get_references_for_events(["$evt1"]))
        finally:
            reset_current_tenant(tok_b)

        # After fix: tenant B MUST re-query (cache miss across tenants).
        self.assertEqual(invocations, ["a.test", "b.test"])


class ApplicableEditCrossTenantTestCase(HomeserverTestCase):
    """F#1 — ``get_applicable_edit`` is tenant-scoped.

    The single-item method body is ``NotImplementedError``; the cache
    is populated only via the paired batch method
    ``get_applicable_edits``, which calls ``db_pool.runInteraction``
    (and then ``self.get_events``). We stub ``runInteraction`` to
    return an empty map — the batch method's follow-up ``get_events``
    call then receives an empty iterable and is never invoked with
    real event ids, so we only need to observe the outer tenant
    context once per call.
    """

    def prepare(self, reactor, clock, hs):
        self.store = hs.get_datastores().main

    def test_cross_tenant_isolation(self):
        invocations: list[str | None] = []

        async def fake_run_interaction(desc, func, *args, **kwargs):
            tenant = get_current_tenant()
            invocations.append(tenant.server_name if tenant else None)
            return {}

        async def fake_get_events(event_ids, **kwargs):
            return {}

        self.store.db_pool.runInteraction = fake_run_interaction  # type: ignore[method-assign]
        self.store.get_events = fake_get_events  # type: ignore[method-assign]

        a, b = _tenant("a.test"), _tenant("b.test")

        tok_a = set_current_tenant(a)
        try:
            self.get_success(self.store.get_applicable_edits(["$evt1"]))
        finally:
            reset_current_tenant(tok_a)

        self.assertEqual(invocations, ["a.test"])

        tok_b = set_current_tenant(b)
        try:
            self.get_success(self.store.get_applicable_edits(["$evt1"]))
        finally:
            reset_current_tenant(tok_b)

        # After fix: tenant B MUST re-query (cache miss across tenants).
        self.assertEqual(invocations, ["a.test", "b.test"])


class ThreadSummaryCrossTenantTestCase(HomeserverTestCase):
    """F#1 — ``get_thread_summary`` is tenant-scoped.

    The single-item method body is ``NotImplementedError``; the cache
    is populated only via the paired batch method
    ``get_thread_summaries``, which calls ``db_pool.runInteraction``.
    """

    def prepare(self, reactor, clock, hs):
        self.store = hs.get_datastores().main

    def test_cross_tenant_isolation(self):
        invocations: list[str | None] = []

        async def fake_run_interaction(desc, func, *args, **kwargs):
            tenant = get_current_tenant()
            invocations.append(tenant.server_name if tenant else None)
            # Return the sentinel the batch method uses when no
            # thread replies are found: an empty mapping of latest
            # event ids, and an empty mapping of counts.
            return ({}, {})

        async def fake_get_events(event_ids, **kwargs):
            return {}

        self.store.db_pool.runInteraction = fake_run_interaction  # type: ignore[method-assign]
        self.store.get_events = fake_get_events  # type: ignore[method-assign]

        a, b = _tenant("a.test"), _tenant("b.test")

        tok_a = set_current_tenant(a)
        try:
            self.get_success(self.store.get_thread_summaries(["$evt1"]))
        finally:
            reset_current_tenant(tok_a)

        self.assertEqual(invocations, ["a.test"])

        tok_b = set_current_tenant(b)
        try:
            self.get_success(self.store.get_thread_summaries(["$evt1"]))
        finally:
            reset_current_tenant(tok_b)

        # After fix: tenant B MUST re-query (cache miss across tenants).
        self.assertEqual(invocations, ["a.test", "b.test"])


class ThreadIdCrossTenantTestCase(HomeserverTestCase):
    """F#1 — ``get_thread_id`` is tenant-scoped.

    The same ``event_id`` could refer to different thread roots (or
    ``main``) in different tenant schemas. The method body calls
    ``db_pool.runInteraction("get_thread_id", ...)`` and there is no
    paired ``@cachedList``.
    """

    def prepare(self, reactor, clock, hs):
        self.store = hs.get_datastores().main

    def test_cross_tenant_isolation(self):
        invocations: list[str | None] = []

        async def fake_run_interaction(desc, func, *args, **kwargs):
            tenant = get_current_tenant()
            invocations.append(tenant.server_name if tenant else None)
            if tenant and tenant.server_name == "a.test":
                return "$thread-a"
            return "$thread-b"

        self.store.db_pool.runInteraction = fake_run_interaction  # type: ignore[method-assign]

        a, b = _tenant("a.test"), _tenant("b.test")

        tok_a = set_current_tenant(a)
        try:
            ra = self.get_success(self.store.get_thread_id("$evt1"))
        finally:
            reset_current_tenant(tok_a)

        tok_b = set_current_tenant(b)
        try:
            rb = self.get_success(self.store.get_thread_id("$evt1"))
        finally:
            reset_current_tenant(tok_b)

        self.assertEqual(ra, "$thread-a")
        self.assertEqual(rb, "$thread-b")
        self.assertEqual(invocations, ["a.test", "b.test"])


class ThreadIdForReceiptsCrossTenantTestCase(HomeserverTestCase):
    """F#1 — ``get_thread_id_for_receipts`` is tenant-scoped.

    Same shape as ``get_thread_id``: a standalone ``@cached`` with no
    paired ``@cachedList``, backed by ``db_pool.runInteraction``.
    Different tenants must never share a cache entry for the same
    ``event_id``.
    """

    def prepare(self, reactor, clock, hs):
        self.store = hs.get_datastores().main

    def test_cross_tenant_isolation(self):
        invocations: list[str | None] = []

        async def fake_run_interaction(desc, func, *args, **kwargs):
            tenant = get_current_tenant()
            invocations.append(tenant.server_name if tenant else None)
            if tenant and tenant.server_name == "a.test":
                return "$thread-a"
            return "$thread-b"

        self.store.db_pool.runInteraction = fake_run_interaction  # type: ignore[method-assign]

        a, b = _tenant("a.test"), _tenant("b.test")

        tok_a = set_current_tenant(a)
        try:
            ra = self.get_success(self.store.get_thread_id_for_receipts("$evt1"))
        finally:
            reset_current_tenant(tok_a)

        tok_b = set_current_tenant(b)
        try:
            rb = self.get_success(self.store.get_thread_id_for_receipts("$evt1"))
        finally:
            reset_current_tenant(tok_b)

        self.assertEqual(ra, "$thread-a")
        self.assertEqual(rb, "$thread-b")
        self.assertEqual(invocations, ["a.test", "b.test"])
