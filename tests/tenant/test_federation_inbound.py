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
