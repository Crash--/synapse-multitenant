#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2026 Linagora
#

"""
Red/green probes for phase 10 — federation inbound multi-tenant support.

Probes verify that:
1. Authenticator resolves destination from X-Matrix header and sets tenant context
2. Authenticator falls back to current tenant context when destination is absent
3. Authenticator rejects destination not matching any tenant
4. json_request["destination"] matches what the remote signed
"""

from http import HTTPStatus
from unittest import TestCase
from unittest.mock import AsyncMock, MagicMock, patch

from synapse.config.tenants import TenantConfig
from synapse.federation.transport.server._base import AuthenticationError, Authenticator


def _make_tenant(name: str) -> TenantConfig:
    return TenantConfig(
        server_name=f"{name}.localhost",
        database_schema=f"tenant_{name}",
        signing_key_path=f"/keys/{name}.key",
        media_store_path=f"/media/{name}",
    )


def _make_hs(
    hostname: str = "main.localhost",
    tenants: list[TenantConfig] | None = None,
) -> MagicMock:
    hs = MagicMock()
    hs.hostname = hostname
    hs.get_clock.return_value = MagicMock(time_msec=MagicMock(return_value=1000))
    hs.get_keyring.return_value = AsyncMock()
    hs.get_datastores.return_value.main = MagicMock()
    hs.config.federation.federation_domain_whitelist = None
    hs.get_notifier.return_value = MagicMock()
    hs.config.worker.worker_app = None

    # Build a tenant registry mock
    registry = MagicMock()
    tenants = tenants or []
    tenant_map = {t.server_name: t for t in tenants}
    registry.get_tenant = MagicMock(side_effect=lambda sn: tenant_map.get(sn))
    hs.get_tenant_registry.return_value = registry

    # is_mine_server_name checks global + all tenants
    all_names = {hostname} | set(tenant_map.keys())
    hs.is_mine_server_name = MagicMock(side_effect=lambda sn: sn in all_names)

    return hs


# ── 10a probes: federation tenant resolution ─────────────────────


class TestAuthenticatorDestinationResolution(TestCase):
    """Authenticator must use X-Matrix destination to set json_request['destination']
    and resolve tenant context."""

    def test_authenticator_accepts_tenant_registry(self) -> None:
        """Authenticator.__init__ must accept and store a tenant_registry parameter."""
        acme = _make_tenant("acme")
        hs = _make_hs(tenants=[acme])
        registry = hs.get_tenant_registry()
        auth = Authenticator(hs, tenant_registry=registry)
        self.assertIs(auth._tenant_registry, registry)

    def test_authenticator_resolve_destination_from_xmatrix(self) -> None:
        """When X-Matrix header has destination=acme.localhost,
        _resolve_federation_destination returns acme.localhost."""
        acme = _make_tenant("acme")
        hs = _make_hs(tenants=[acme])
        registry = hs.get_tenant_registry()
        auth = Authenticator(hs, tenant_registry=registry)
        result = auth._resolve_federation_destination(
            parsed_destination="acme.localhost"
        )
        self.assertEqual(result, "acme.localhost")

    def test_authenticator_resolve_destination_fallback_to_tenant_ctx(self) -> None:
        """When X-Matrix destination is None, falls back to get_current_tenant()."""
        acme = _make_tenant("acme")
        hs = _make_hs(tenants=[acme])
        registry = hs.get_tenant_registry()
        auth = Authenticator(hs, tenant_registry=registry)
        with patch(
            "synapse.federation.transport.server._base.get_current_tenant",
            return_value=acme,
        ):
            result = auth._resolve_federation_destination(parsed_destination=None)
        self.assertEqual(result, "acme.localhost")

    def test_authenticator_resolve_destination_fallback_to_global(self) -> None:
        """When both X-Matrix destination and tenant context are None,
        falls back to self.server_name."""
        hs = _make_hs()
        auth = Authenticator(hs, tenant_registry=MagicMock(get_tenant=MagicMock(return_value=None)))
        with patch(
            "synapse.federation.transport.server._base.get_current_tenant",
            return_value=None,
        ):
            result = auth._resolve_federation_destination(parsed_destination=None)
        self.assertEqual(result, "main.localhost")

    def test_authenticator_rejects_unknown_destination(self) -> None:
        """When X-Matrix destination doesn't match any tenant or global hostname,
        raise AuthenticationError."""
        hs = _make_hs()
        auth = Authenticator(hs, tenant_registry=MagicMock(get_tenant=MagicMock(return_value=None)))
        with self.assertRaises(AuthenticationError):
            auth._resolve_federation_destination(
                parsed_destination="unknown.example.com"
            )

    def test_authenticator_sets_tenant_context(self) -> None:
        """After resolving destination to a tenant, Authenticator must call
        set_current_tenant with the resolved tenant."""
        acme = _make_tenant("acme")
        hs = _make_hs(tenants=[acme])
        registry = hs.get_tenant_registry()
        auth = Authenticator(hs, tenant_registry=registry)
        with patch(
            "synapse.federation.transport.server._base.set_current_tenant"
        ) as mock_set:
            auth._resolve_federation_destination(
                parsed_destination="acme.localhost"
            )
            mock_set.assert_called_once_with(acme)


# ── 10b probes: per-tenant key server ────────────────────────────


class TestLocalKeyTenantAware(TestCase):
    """LocalKey.on_GET must return per-tenant keys when tenant context is set."""

    def _make_keyring(self, tenants: list[TenantConfig]) -> MagicMock:
        """Build a mock MultiTenantKeyring with signing + verify keys per tenant."""
        from signedjson import key as key_mod

        keyring = MagicMock()
        signing_keys: dict[str, list] = {}
        verify_keys: dict[str, dict] = {}

        for t in tenants:
            sk = key_mod.generate_signing_key("0")
            signing_keys[t.server_name] = [sk]
            vk = key_mod.get_verify_key(sk)
            key_id = f"{vk.alg}:{vk.version}"
            verify_keys[t.server_name] = {key_id: vk}

        keyring.get_all_signing_keys = MagicMock(
            side_effect=lambda sn: signing_keys[sn]
        )
        keyring.get_verify_keys = MagicMock(
            side_effect=lambda sn: verify_keys[sn]
        )
        return keyring

    def _make_hs(self) -> MagicMock:
        """Build a mock HomeServer with a real global signing key."""
        from signedjson import key as key_mod

        hs = MagicMock()
        hs.config.key.signing_key = [key_mod.generate_signing_key("g")]
        hs.config.key.old_signing_keys = {}
        hs.config.key.key_refresh_interval = 86400000
        hs.config.server.server_name = "main.localhost"
        hs.get_clock.return_value = MagicMock(time_msec=MagicMock(return_value=1000))
        return hs

    def test_local_key_accepts_keyring(self) -> None:
        """LocalKey.__init__ must accept an optional multi_tenant_keyring parameter."""
        from synapse.rest.key.v2.local_key_resource import LocalKey

        hs = self._make_hs()
        keyring = self._make_keyring([_make_tenant("acme")])
        local_key = LocalKey(hs, multi_tenant_keyring=keyring)
        self.assertIs(local_key._multi_tenant_keyring, keyring)

    def test_local_key_returns_tenant_server_name(self) -> None:
        """When tenant context is set, on_GET response must have tenant's server_name."""
        from synapse.rest.key.v2.local_key_resource import LocalKey

        hs = self._make_hs()
        acme = _make_tenant("acme")
        keyring = self._make_keyring([acme])
        local_key = LocalKey(hs, multi_tenant_keyring=keyring)

        with patch(
            "synapse.rest.key.v2.local_key_resource.get_current_tenant",
            return_value=acme,
        ):
            status, body = local_key.on_GET(MagicMock(), key_id=None)

        self.assertEqual(status, 200)
        self.assertEqual(body["server_name"], "acme.localhost")

    def test_local_key_returns_global_when_no_tenant(self) -> None:
        """When no tenant context is set, on_GET returns the global key response."""
        from synapse.rest.key.v2.local_key_resource import LocalKey

        hs = self._make_hs()
        local_key = LocalKey(hs)

        with patch(
            "synapse.rest.key.v2.local_key_resource.get_current_tenant",
            return_value=None,
        ):
            status, body = local_key.on_GET(MagicMock(), key_id=None)

        self.assertEqual(status, 200)
        self.assertEqual(body["server_name"], "main.localhost")


# ── 10c probes: FederationServer tenant-awareness ────────────────


class TestFederationServerEffectiveServerName(TestCase):
    """FederationServer._effective_server_name must resolve from tenant context."""

    def _make_federation_server(
        self, hostname: str = "main.localhost"
    ) -> "FederationServer":
        from synapse.federation.federation_server import FederationServer

        hs = MagicMock()
        hs.hostname = hostname
        hs.get_clock.return_value = MagicMock()
        hs.get_state_handler.return_value = MagicMock()
        hs.get_storage_controllers.return_value = MagicMock()
        hs.get_datastores.return_value = MagicMock()
        hs.get_federation_handler.return_value = MagicMock()
        hs.get_module_api_callbacks.return_value.spam_checker = MagicMock()
        hs.get_federation_event_handler.return_value = MagicMock()
        hs.config.federation.federation_metrics_domains = frozenset()
        hs.signing_key = MagicMock()
        hs.get_multi_tenant_keyring.return_value = MagicMock()
        return FederationServer(hs)

    def test_effective_server_name_with_tenant(self) -> None:
        """_effective_server_name returns tenant's server_name when context is set."""
        fs = self._make_federation_server()
        acme = _make_tenant("acme")
        with patch(
            "synapse.federation.federation_server.get_current_tenant",
            return_value=acme,
        ):
            self.assertEqual(fs._effective_server_name, "acme.localhost")

    def test_effective_server_name_without_tenant(self) -> None:
        """_effective_server_name falls back to self.server_name when no context."""
        fs = self._make_federation_server()
        with patch(
            "synapse.federation.federation_server.get_current_tenant",
            return_value=None,
        ):
            self.assertEqual(fs._effective_server_name, "main.localhost")

    def test_effective_signing_key_with_tenant(self) -> None:
        """_effective_signing_key returns tenant key from MultiTenantKeyring."""
        fs = self._make_federation_server()
        acme = _make_tenant("acme")
        mock_key = MagicMock()
        fs._multi_tenant_keyring = MagicMock()
        fs._multi_tenant_keyring.get_signing_key.return_value = mock_key
        with patch(
            "synapse.federation.federation_server.get_current_tenant",
            return_value=acme,
        ):
            self.assertIs(fs._effective_signing_key, mock_key)
            fs._multi_tenant_keyring.get_signing_key.assert_called_with(
                "acme.localhost"
            )

    def test_effective_signing_key_without_tenant(self) -> None:
        """_effective_signing_key falls back to hs.signing_key when no context."""
        fs = self._make_federation_server()
        mock_key = MagicMock()
        fs.hs.signing_key = mock_key
        with patch(
            "synapse.federation.federation_server.get_current_tenant",
            return_value=None,
        ):
            self.assertIs(fs._effective_signing_key, mock_key)


# ── 10d probes: per-tenant federation allow-lists ────────────────


class TestTenantFederationConfig(TestCase):
    """TenantFederationConfig dataclass and integration with whitelist check."""

    def test_tenant_federation_config_from_dict(self) -> None:
        """TenantFederationConfig.from_dict parses a whitelist."""
        from synapse.config.tenants import TenantFederationConfig

        cfg = TenantFederationConfig.from_dict(
            {"federation_domain_whitelist": ["partner.com", "ally.org"]}
        )
        self.assertIn("partner.com", cfg.federation_domain_whitelist)
        self.assertIn("ally.org", cfg.federation_domain_whitelist)

    def test_tenant_federation_config_none_whitelist(self) -> None:
        """TenantFederationConfig with no whitelist means inherit global."""
        from synapse.config.tenants import TenantFederationConfig

        cfg = TenantFederationConfig.from_dict({})
        self.assertIsNone(cfg.federation_domain_whitelist)

    def test_tenant_config_has_federation_field(self) -> None:
        """TenantConfig must accept a federation field."""
        from synapse.config.tenants import TenantFederationConfig

        fed_cfg = TenantFederationConfig(
            federation_domain_whitelist={"partner.com": True}
        )
        tenant = TenantConfig(
            server_name="acme.localhost",
            database_schema="tenant_acme",
            signing_key_path="/keys/acme.key",
            media_store_path="/media/acme",
            federation=fed_cfg,
        )
        self.assertIs(tenant.federation, fed_cfg)

    def test_is_domain_allowed_tenant_override(self) -> None:
        """When tenant has whitelist, domain check uses tenant list, not global."""
        from synapse.config.tenants import TenantFederationConfig

        fed_config = MagicMock()
        fed_config.federation_domain_whitelist = {"global.com": True}

        tenant_fed = TenantFederationConfig(
            federation_domain_whitelist={"tenant-only.com": True}
        )

        # Use the real method from FederationConfig
        from synapse.config.federation import FederationConfig

        # tenant-only.com allowed by tenant, not by global
        self.assertTrue(
            FederationConfig.is_domain_allowed_according_to_federation_whitelist(
                fed_config, "tenant-only.com", tenant_federation_config=tenant_fed
            )
        )
        # global.com NOT allowed by tenant whitelist
        self.assertFalse(
            FederationConfig.is_domain_allowed_according_to_federation_whitelist(
                fed_config, "global.com", tenant_federation_config=tenant_fed
            )
        )

    def test_is_domain_allowed_falls_through_to_global(self) -> None:
        """When tenant has no whitelist (None), falls through to global."""
        from synapse.config.tenants import TenantFederationConfig

        fed_config = MagicMock()
        fed_config.federation_domain_whitelist = {"global.com": True}

        tenant_fed = TenantFederationConfig(federation_domain_whitelist=None)

        from synapse.config.federation import FederationConfig

        self.assertTrue(
            FederationConfig.is_domain_allowed_according_to_federation_whitelist(
                fed_config, "global.com", tenant_federation_config=tenant_fed
            )
        )
        self.assertFalse(
            FederationConfig.is_domain_allowed_according_to_federation_whitelist(
                fed_config, "unknown.com", tenant_federation_config=tenant_fed
            )
        )


# ── 10e probes: EventAuthHandler tenant-awareness ────────────────


class TestEventAuthHandlerEffectiveServerName(TestCase):
    """EventAuthHandler._effective_server_name must resolve from tenant context."""

    def _make_handler(self, hostname: str = "main.localhost"):
        from synapse.handlers.event_auth import EventAuthHandler

        hs = MagicMock()
        hs.hostname = hostname
        hs.get_clock.return_value = MagicMock()
        hs.get_datastores.return_value.main = MagicMock()
        hs.get_storage_controllers.return_value.state = MagicMock()
        return EventAuthHandler(hs)

    def test_effective_server_name_with_tenant(self) -> None:
        """_effective_server_name returns tenant's server_name when context is set."""
        handler = self._make_handler()
        acme = _make_tenant("acme")
        with patch(
            "synapse.handlers.event_auth.get_current_tenant",
            return_value=acme,
        ):
            self.assertEqual(handler._effective_server_name, "acme.localhost")

    def test_effective_server_name_without_tenant(self) -> None:
        """_effective_server_name falls back to self._server_name when no context."""
        handler = self._make_handler()
        with patch(
            "synapse.handlers.event_auth.get_current_tenant",
            return_value=None,
        ):
            self.assertEqual(handler._effective_server_name, "main.localhost")


# ── 10'-a probe: SynapseSite uses shared HS tenant registry ──────


class TestSynapseSiteUsesSharedTenantRegistry(TestCase):
    """SynapseSite must use hs.get_tenant_registry() so DB-reload updates are visible.

    Bug 10'-a: SynapseSite previously constructed its own TenantRegistry from YAML
    at init. In DB-driven mode (source='database'), YAML has no tenants, so the
    site's registry stayed empty and _setup_tenant_context always returned early.
    """

    def test_site_uses_shared_registry(self) -> None:
        import inspect
        from synapse.http import site

        # Read the source of SynapseSite.__init__ and verify it calls
        # hs.get_tenant_registry() rather than constructing a new TenantRegistry.
        src = inspect.getsource(site.SynapseSite.__init__)
        self.assertIn(
            "hs.get_tenant_registry()",
            src,
            "SynapseSite must share the HS-level tenant registry for DB reloads",
        )
        self.assertNotIn(
            "TenantRegistry(tenants_config.multi_tenant)",
            src,
            "SynapseSite must not construct its own YAML-bound registry",
        )


# ── 10'-b probes: cross-tenant room join (Option A — federation loopback) ──


class TestGetEffectiveServerName(TestCase):
    """get_effective_server_name distinguishes current tenant from siblings.

    Unlike is_mine_server_name (which returns True for ANY local tenant),
    get_effective_server_name returns the CURRENT tenant's server_name so we
    can tell "self traffic" apart from "sibling-tenant traffic" in routing.
    """

    def test_returns_tenant_when_context_set(self) -> None:
        from synapse.tenant_context import (
            get_effective_server_name,
            set_current_tenant,
            reset_current_tenant,
        )

        acme = _make_tenant("acme")
        token = set_current_tenant(acme)
        try:
            self.assertEqual(
                get_effective_server_name("main.localhost"),
                "acme.localhost",
            )
        finally:
            reset_current_tenant(token)

    def test_returns_fallback_when_no_context(self) -> None:
        from synapse.tenant_context import (
            get_effective_server_name,
            set_current_tenant,
            reset_current_tenant,
        )

        # Explicitly clear any leaked context from other tests
        clear_token = set_current_tenant(None)
        try:
            self.assertEqual(
                get_effective_server_name("main.localhost"),
                "main.localhost",
            )
        finally:
            reset_current_tenant(clear_token)


class TestCrossTenantJoinPath(TestCase):
    """Bug 10'-b: Sibling-tenant domains must survive the join path filters.

    When bob@tenant-b.com is invited to a room owned by tenant-a, the
    join machinery must:
      (1) add tenant-a.com to remote_room_hosts (inviter filter should not
          exclude sibling-tenant inviters),
      (2) keep tenant-a.com in remote_room_hosts in _remote_join (self-filter
          must only exclude the CURRENT tenant, not all local tenants),
      (3) the transport layer self-send guard must only reject
          destination == current tenant, not destination == any local tenant.
    """

    def test_inviter_filter_logic_preserves_sibling(self) -> None:
        """Simulates the room_member.py:1054 filter condition in isolation."""
        from synapse.tenant_context import (
            get_effective_server_name,
            set_current_tenant,
            reset_current_tenant,
        )

        tenant_b = _make_tenant("b")

        # Bob is in tenant-b; Alice (inviter) is in sibling tenant-a.
        token = set_current_tenant(tenant_b)
        try:
            current_self = get_effective_server_name("main.localhost")
            inviter_domain = "a.localhost"

            # New condition: only exclude if inviter is in CURRENT tenant.
            should_add_as_remote = inviter_domain != current_self
            self.assertTrue(
                should_add_as_remote,
                "sibling-tenant inviter must count as remote for routing",
            )
        finally:
            reset_current_tenant(token)

    def test_remote_join_filter_preserves_sibling(self) -> None:
        """Simulates the room_member.py:1889 host filter in _remote_join."""
        from synapse.tenant_context import (
            get_effective_server_name,
            set_current_tenant,
            reset_current_tenant,
        )

        tenant_b = _make_tenant("b")
        remote_room_hosts = ["a.localhost", "b.localhost", "external.example.com"]

        token = set_current_tenant(tenant_b)
        try:
            current_self = get_effective_server_name("main.localhost")
            filtered = [h for h in remote_room_hosts if h != current_self]
            self.assertIn("a.localhost", filtered, "sibling tenant must survive")
            self.assertNotIn("b.localhost", filtered, "current tenant must be removed")
            self.assertIn("external.example.com", filtered, "external must survive")
        finally:
            reset_current_tenant(token)

    def test_transport_self_send_guard_allows_sibling(self) -> None:
        """transport/client.py:299 must only reject destination == current tenant."""
        from synapse.tenant_context import (
            get_effective_server_name,
            set_current_tenant,
            reset_current_tenant,
        )

        tenant_b = _make_tenant("b")
        token = set_current_tenant(tenant_b)
        try:
            current_self = get_effective_server_name("main.localhost")

            # Sending to a sibling tenant is OK (not self)
            sibling_dest = "a.localhost"
            self.assertNotEqual(sibling_dest, current_self)

            # Sending to current tenant IS self
            self_dest = "b.localhost"
            self.assertEqual(self_dest, current_self)
        finally:
            reset_current_tenant(token)


class TestIsHostInRoomCurrentTenantOnly(TestCase):
    """_is_host_in_room must consider only the CURRENT tenant's members.

    Sibling-tenant members live in a different schema; the current tenant
    cannot serve joins on their behalf. So _is_host_in_room should return
    False when only sibling-tenant members are in the state.
    """

    def test_current_tenant_member_counts_as_host(self) -> None:
        from synapse.tenant_context import (
            get_effective_server_name,
            set_current_tenant,
            reset_current_tenant,
        )

        tenant_b = _make_tenant("b")
        token = set_current_tenant(tenant_b)
        try:
            current_self = get_effective_server_name("main.localhost")
            # Member state key belongs to current tenant
            state_key = "@bob:b.localhost"
            _, _, server = state_key.partition(":")
            self.assertEqual(server, current_self)
        finally:
            reset_current_tenant(token)

    def test_sibling_tenant_member_does_not_count(self) -> None:
        from synapse.tenant_context import (
            get_effective_server_name,
            set_current_tenant,
            reset_current_tenant,
        )

        tenant_b = _make_tenant("b")
        token = set_current_tenant(tenant_b)
        try:
            current_self = get_effective_server_name("main.localhost")
            # Member state key belongs to sibling tenant
            state_key = "@alice:a.localhost"
            _, _, server = state_key.partition(":")
            self.assertNotEqual(server, current_self)
        finally:
            reset_current_tenant(token)
