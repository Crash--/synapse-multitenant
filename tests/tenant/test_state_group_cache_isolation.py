#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
"""Regression tests for multi-tenant state_group cache isolation.

These tests pin the behavior fixed in sub-phase 2a of the phase-2 close:
`StateGroupDataStore._state_group_cache` and `_state_group_members_cache`
are both `DictionaryCache[tuple[str, int], StateKey, str]`, keyed on a
`(effective_server_name, state_group)` tuple. Per-tenant Postgres
sequences mint colliding `state_group` IDs across tenant schemas, so if
the outer key drops back to a bare `int` a read from one tenant will
return cached state written under a different tenant's schema.

The docker-multitenant rig exercises this end-to-end via
`test_create_room_per_tenant` (the 2a gate probe). These unit tests are
the in-tree backstop: they call the real
`StateGroupDataStore._get_state_for_group_using_cache` and
`_insert_into_cache` methods against a minimal stub `self`, switching
the active tenant via `tenant_context()` between write and read, and
assert cross-tenant reads return empty.

If you are here because a future refactor dropped the tenant component
from the cache key, the fix is in
`synapse/storage/databases/state/store.py` — every `cache.get(...)`,
`cache.update(...)`, and `txn.call_after(cache.update, ...)` on either
state-group cache must pass `(effective_server_name, state_group)`, not
a bare `state_group`.
"""

from types import SimpleNamespace
from unittest import TestCase

from synapse.config.tenants import TenantConfig
from synapse.storage.databases.state.store import StateGroupDataStore
from synapse.tenant_context import tenant_context
from synapse.types.state import StateFilter
from synapse.util.caches.dictionary_cache import DictionaryCache
from synapse.util.clock import Clock
from twisted.internet import reactor


def _make_tenant(server_name: str) -> TenantConfig:
    return TenantConfig(
        server_name=server_name,
        database_schema=f"tenant_{server_name.replace('.', '_')}",
        signing_key_path=f"/keys/{server_name}.key",
        media_store_path=f"/media/{server_name}",
    )


class _FakeHomeServer:
    """Minimal `hs` stub: the two cache methods only call
    `self.hs.effective_server_name()`, which itself reads from the
    per-request tenant contextvar and falls back to `self.hostname`."""

    def __init__(self, hostname: str) -> None:
        self.hostname = hostname

    # Mirror of the real synapse.server.HomeServer.effective_server_name
    # — we import the same underlying lookup so behavior stays in sync if
    # the contextvar API changes.
    def effective_server_name(self) -> str:
        from synapse.tenant_context import get_current_tenant

        tenant = get_current_tenant()
        if tenant is not None:
            return tenant.server_name
        return self.hostname


class StateGroupCacheIsolationTestCase(TestCase):
    """The two `StateGroupDataStore` caches must be tenant-keyed."""

    def setUp(self) -> None:
        # Build caches matching the real store: outer key is
        # (server_name, state_group). We use a small clock here — the
        # cache only needs it for expiry bookkeeping.
        clock = Clock(reactor, server_name="test")
        self._non_member_cache: DictionaryCache = DictionaryCache(
            name="*stateGroupCache*",
            clock=clock,
            server_name="test",
            max_entries=100,
        )
        self._member_cache: DictionaryCache = DictionaryCache(
            name="*stateGroupMembersCache*",
            clock=clock,
            server_name="test",
            max_entries=100,
        )

        # Minimal stub `self` that the unbound store methods can bind
        # against. They touch: self.hs.effective_server_name(),
        # self._state_group_cache, self._state_group_members_cache.
        self._store_stub = SimpleNamespace(
            hs=_FakeHomeServer("primary.localhost"),
            _state_group_cache=self._non_member_cache,
            _state_group_members_cache=self._member_cache,
        )

        self._acme = _make_tenant("acme.localhost")
        self._corp = _make_tenant("corp.localhost")

    def _insert(self, group: int, state: dict) -> None:
        """Call the real `_insert_into_cache` against the stub."""
        StateGroupDataStore._insert_into_cache(
            self._store_stub,
            group_to_state_dict={group: state},
            state_filter=StateFilter.all(),
            cache_seq_num_members=self._member_cache.sequence,
            cache_seq_num_non_members=self._non_member_cache.sequence,
        )

    def _read(self, group: int) -> tuple[dict, bool]:
        """Call the real `_get_state_for_group_using_cache` (non-member
        cache) against the stub. Returns (state_dict, got_all)."""
        return StateGroupDataStore._get_state_for_group_using_cache(
            self._store_stub,
            cache=self._non_member_cache,
            group=group,
            state_filter=StateFilter.all(),
        )

    def test_colliding_state_group_int_does_not_cross_tenants(self) -> None:
        """Writes under acme's state_group=42 must not be visible to
        corp's state_group=42 — this is the exact cross-tenant leak the
        docker probe `test_create_room_per_tenant` caught in 2a."""

        group = 42
        acme_state = {
            ("m.room.name", ""): "$acme_name_event:acme.localhost",
            ("m.room.topic", ""): "$acme_topic_event:acme.localhost",
        }
        corp_state = {
            ("m.room.name", ""): "$corp_name_event:corp.localhost",
        }

        with tenant_context(self._acme):
            self._insert(group, acme_state)

        with tenant_context(self._corp):
            self._insert(group, corp_state)

        # Cross-read: under acme, should only see acme's state.
        with tenant_context(self._acme):
            got_acme, acme_full = self._read(group)
        self.assertEqual(got_acme, acme_state)
        self.assertTrue(acme_full)

        # Cross-read: under corp, should only see corp's state.
        with tenant_context(self._corp):
            got_corp, corp_full = self._read(group)
        self.assertEqual(got_corp, corp_state)
        self.assertTrue(corp_full)

        # Neither tenant's state should contain the other's event IDs.
        acme_event_ids = set(got_acme.values())
        corp_event_ids = set(got_corp.values())
        self.assertFalse(
            acme_event_ids & corp_event_ids,
            "cross-tenant leak: acme and corp state share an event_id "
            f"(acme={acme_event_ids}, corp={corp_event_ids}) — the "
            "state_group cache key is probably no longer tenant-prefixed",
        )

    def test_read_under_different_tenant_than_write_misses(self) -> None:
        """An insert under acme must be a cache MISS for a read under
        corp (even though both see the same bare state_group int).
        Catches the case where write uses the tuple key but read still
        uses bare int — the cache would appear empty but a future probe
        could still slip through."""

        group = 7
        acme_state = {("m.room.create", ""): "$acme_create:acme.localhost"}

        with tenant_context(self._acme):
            self._insert(group, acme_state)

        # Read under corp: expect empty state (miss), not acme's data.
        with tenant_context(self._corp):
            got, full = self._read(group)
        self.assertEqual(
            got,
            {},
            "cross-tenant leak: corp read returned acme's cached state",
        )
        # `full` is False because the cache has no entry for corp's
        # (corp.localhost, 7) key.
        self.assertFalse(full)

    def test_fallback_hostname_used_outside_tenant_context(self) -> None:
        """With no tenant bound, both writes and reads use the primary
        hostname — so they must be mutually visible."""

        group = 99
        state = {("m.room.name", ""): "$primary_name:primary.localhost"}

        # Both under the fallback (no tenant context bound).
        self._insert(group, state)
        got, full = self._read(group)

        self.assertEqual(got, state)
        self.assertTrue(full)
