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

    def test_local_key_accepts_keyring(self) -> None:
        """LocalKey.__init__ must accept an optional multi_tenant_keyring parameter."""
        from synapse.rest.key.v2.local_key_resource import LocalKey

        hs = MagicMock()
        hs.config.key.signing_key = [MagicMock()]
        hs.config.key.old_signing_keys = {}
        hs.config.key.key_refresh_interval = 86400000
        hs.config.server.server_name = "main.localhost"
        hs.get_clock.return_value = MagicMock(time_msec=MagicMock(return_value=1000))

        keyring = self._make_keyring([_make_tenant("acme")])
        local_key = LocalKey(hs, multi_tenant_keyring=keyring)
        self.assertIs(local_key._multi_tenant_keyring, keyring)

    def test_local_key_returns_tenant_server_name(self) -> None:
        """When tenant context is set, on_GET response must have tenant's server_name."""
        from synapse.rest.key.v2.local_key_resource import LocalKey

        hs = MagicMock()
        hs.config.key.signing_key = [MagicMock()]
        hs.config.key.old_signing_keys = {}
        hs.config.key.key_refresh_interval = 86400000
        hs.config.server.server_name = "main.localhost"
        hs.get_clock.return_value = MagicMock(time_msec=MagicMock(return_value=1000))

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
        from signedjson import key as key_mod
        from synapse.rest.key.v2.local_key_resource import LocalKey

        # Need a real signing key so response_json_object() works
        sk = key_mod.generate_signing_key("0")

        hs = MagicMock()
        hs.config.key.signing_key = [sk]
        hs.config.key.old_signing_keys = {}
        hs.config.key.key_refresh_interval = 86400000
        hs.config.server.server_name = "main.localhost"
        hs.get_clock.return_value = MagicMock(time_msec=MagicMock(return_value=1000))

        local_key = LocalKey(hs)

        with patch(
            "synapse.rest.key.v2.local_key_resource.get_current_tenant",
            return_value=None,
        ):
            status, body = local_key.on_GET(MagicMock(), key_id=None)

        self.assertEqual(status, 200)
        self.assertEqual(body["server_name"], "main.localhost")
