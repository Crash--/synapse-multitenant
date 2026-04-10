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
