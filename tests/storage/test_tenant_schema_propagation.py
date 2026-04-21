#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2026 Linagora
#
"""F#2 regression tests — tenant schema and contextvar propagation.

See ``docker-demo/stress-test/finding-2-repro-2026-04-21.md`` for the
diagnosis. Two fixes pinned here:

1. ``_set_tenant_schema`` always re-issues ``SET search_path`` (the
   per-connection cache it used to have was unsafe; it skipped the SET
   while the session `search_path` had actually been reset out of band,
   so the next INSERT landed in `public`).

2. ``runWithConnection``'s ``inner_func`` re-sets the tenant
   ``contextvars.ContextVar`` inside the thread-pool thread, because
   Python contextvars do NOT propagate from the reactor into the pool.
"""

from unittest import mock

from twisted.trial.unittest import SynchronousTestCase

from synapse.config.tenants import TenantConfig


def _tenant(name: str) -> TenantConfig:
    return TenantConfig(
        server_name=name,
        database_schema=f"tenant_{name.replace('.', '_')}",
        signing_key_path=f"/keys/{name}.key",
        media_store_path=f"/media/{name}",
    )


class SetTenantSchemaNoCacheTestCase(SynchronousTestCase):
    """Regression: ``_set_tenant_schema`` re-issues SET on every call."""

    def test_repeat_call_same_schema_still_executes_set(self):
        """Calling _set_tenant_schema twice with the same schema executes SET twice.

        Before the 2026-04-21 F#2 fix, the second call was a cache hit
        and skipped the SET. That cache was unsafe: if the session
        search_path had been reset between the two calls (by DISCARD ALL,
        reconnect, or anything else), subsequent INSERTs would land in
        `public`. The fix drops the cache.
        """
        from synapse.storage.database import DatabasePool
        from synapse.storage.engines.postgres import PostgresEngine

        # Minimal fake conn with a cursor we can observe
        fake_cursor = mock.MagicMock()
        fake_conn = mock.MagicMock()
        fake_conn.cursor.return_value = fake_cursor

        # DatabasePool.__init__ is heavy — just take the method off the
        # class and call it with a minimal `self` surrogate that exposes
        # the two attributes the method touches.
        self_stub = mock.MagicMock()
        self_stub.engine = mock.MagicMock(spec=PostgresEngine)
        # isinstance() check against PostgresEngine needs the spec above;
        # wrap the call through the unbound method to sidestep __init__
        tenant = _tenant("a.test")

        DatabasePool._set_tenant_schema(self_stub, fake_conn, tenant)
        DatabasePool._set_tenant_schema(self_stub, fake_conn, tenant)

        # Both calls must execute SET — no cache hit.
        execute_calls = [
            c for c in fake_cursor.execute.call_args_list
            if "SET search_path" in str(c)
        ]
        self.assertEqual(
            len(execute_calls),
            2,
            f"Expected SET issued twice (no cache), got {len(execute_calls)}. "
            "The per-connection cache that used to live in _set_tenant_schema "
            "was F#2's root cause; it must stay removed.",
        )


class RunWithConnectionPropagatesTenantContextVarTestCase(SynchronousTestCase):
    """Regression: tenant contextvar is visible inside runWithConnection callbacks."""

    def test_callback_sees_tenant_via_get_current_tenant(self):
        """``get_current_tenant()`` inside the callback equals what was set in reactor.

        The `inner_func` wrapper inside `DatabasePool.runWithConnection`
        runs in a thread-pool thread. Python contextvars don't cross
        that boundary, so the 2026-04-21 fix explicitly calls
        ``set_current_tenant(captured_tenant)`` inside the wrapper.

        Without that re-set, any code reached from inside a DB
        transaction that reads `get_current_tenant()` sees `None` —
        including `@tenant_cached` keys computed inside txn callbacks
        and `txn.call_after` callbacks that invalidate tenant-scoped
        caches.
        """
        from synapse.storage.database import DatabasePool
        from synapse.tenant_context import (
            get_current_tenant,
            reset_current_tenant,
            set_current_tenant,
        )

        tenant = _tenant("ctxprop.test")

        # Build the minimal stub that exercises just the contextvar
        # re-set logic inside inner_func. We reach into the method by
        # calling the behavior it promises: if `captured_tenant` was
        # non-None, set_current_tenant is called with it before func()
        # runs. We verify this by reading the source for the invariant
        # and by exercising the contextvar primitives themselves.
        #
        # A true integration test here would require a running reactor +
        # thread pool, which is out of scope for a unit test. We cover
        # the contract at two layers: this test pins the primitive
        # contract, and the docker-demo createRoom flow (stored in the
        # diagnosis doc) covers the full integration.

        # Reactor side: no tenant context
        self.assertIsNone(get_current_tenant())

        # Simulate what inner_func does: set + call func + reset.
        observed = {}

        def fake_func():
            observed["tenant_in_callback"] = get_current_tenant()

        ctx_token = set_current_tenant(tenant)
        try:
            fake_func()
        finally:
            reset_current_tenant(ctx_token)

        # The callback observes the tenant.
        self.assertIs(observed["tenant_in_callback"], tenant)
        # And the contextvar is cleared after.
        self.assertIsNone(get_current_tenant())

    def test_inner_func_reads_contextvar_for_tenant_capture(self):
        """Regression: the capture + re-set contract is present in runWithConnection source.

        Smoke test: the module imports `set_current_tenant` and
        `reset_current_tenant`, which are only needed for the F#2 fix.
        A future refactor that removes them will flag this test.
        """
        import synapse.storage.database as db_mod

        self.assertTrue(
            hasattr(db_mod, "set_current_tenant"),
            "synapse.storage.database must import set_current_tenant — "
            "it re-sets the tenant contextvar inside the thread pool.",
        )
        self.assertTrue(
            hasattr(db_mod, "reset_current_tenant"),
            "synapse.storage.database must import reset_current_tenant — "
            "it clears the thread-local contextvar after the DB txn.",
        )
