#
# Copyright 2026 Linagora
#
"""Red/green probes for phase OIDC — per-tenant SSO login bridge."""

from unittest import TestCase

from synapse.config.tenants import TenantOidcConfig


class TestTenantOidcConfigFromDbValue(TestCase):
    """from_db_value must accept both envelope and bare-list shapes."""

    _PROVIDER = {
        "idp_id": "lemonldap",
        "idp_name": "LemonLDAP SSO",
        "issuer": "https://lemonldap.localhost/",
        "client_id": "synapse-demo",
        "client_secret": "demo-secret-change-in-prod",
        "scopes": ["openid", "profile", "email"],
    }

    def test_envelope_enabled(self) -> None:
        """Envelope {'enabled': True, 'providers': [...]} parses to a
        TenantOidcConfig whose providers tuple contains the provider dict."""
        raw = {"enabled": True, "providers": [self._PROVIDER]}
        cfg = TenantOidcConfig.from_db_value(raw)
        self.assertEqual(len(cfg.providers), 1)
        self.assertEqual(cfg.providers[0]["idp_id"], "lemonldap")

    def test_envelope_disabled(self) -> None:
        """Envelope with enabled=False yields an empty tuple."""
        raw = {"enabled": False, "providers": [self._PROVIDER]}
        cfg = TenantOidcConfig.from_db_value(raw)
        self.assertEqual(cfg.providers, ())

    def test_bare_list_backwards_compatible(self) -> None:
        """A bare list of provider dicts still works (legacy shape)."""
        raw = [self._PROVIDER]
        cfg = TenantOidcConfig.from_db_value(raw)
        self.assertEqual(len(cfg.providers), 1)
        self.assertEqual(cfg.providers[0]["idp_id"], "lemonldap")

    def test_envelope_missing_providers_key(self) -> None:
        """Envelope without 'providers' key yields empty tuple."""
        raw = {"enabled": True}
        cfg = TenantOidcConfig.from_db_value(raw)
        self.assertEqual(cfg.providers, ())


from unittest.mock import MagicMock, patch
from synapse.config.tenants import TenantConfig


def _make_tenant_with_oidc(
    server_name: str,
    provider: dict,
) -> TenantConfig:
    return TenantConfig(
        server_name=server_name,
        database_schema=f"tenant_{server_name.replace('.', '_')}",
        signing_key_path=f"/keys/{server_name}.key",
        media_store_path=f"/media/{server_name}",
        oidc=TenantOidcConfig(providers=(provider,)),
    )


class TestBuildTenantProvidersReadsRegistry(TestCase):
    """_build_tenant_providers must iterate the live registry, not the YAML
    dict (Pattern A — see critical-e2e-bugs.md)."""

    _PROVIDER = {
        "idp_id": "lemonldap",
        "idp_name": "LemonLDAP SSO",
        "issuer": "https://lemonldap.localhost/",
        "client_id": "synapse-demo",
        "client_secret": "demo-secret-change-in-prod",
        "scopes": ["openid"],
        "discover": False,
        "authorization_endpoint": "https://lemonldap.localhost/oauth2/authorize",
        "token_endpoint": "https://lemonldap.localhost/oauth2/token",
        "userinfo_endpoint": "https://lemonldap.localhost/oauth2/userinfo",
        "jwks_uri": "https://lemonldap.localhost/oauth2/jwks",
    }

    def test_iterates_registry_not_yaml_dict(self) -> None:
        """DB-driven mode: YAML dict empty, registry populated. Tenant
        providers must be built from the registry."""
        from synapse.handlers.oidc import OidcHandler
        import synapse.handlers.oidc as oidc_mod

        acme = _make_tenant_with_oidc("acme.localhost", self._PROVIDER)
        hs = MagicMock()
        # YAML dict is empty (DB-driven mode).
        hs.config.multi_tenant = MagicMock(enabled=True, tenants={})
        # Registry populated.
        registry = MagicMock()
        registry.get_all_tenants.return_value = [acme]
        hs.get_tenant_registry.return_value = registry
        # Global providers must exist for OidcHandler construction, stub them.
        hs.config.oidc.oidc_providers = [MagicMock(idp_id="global")]
        hs.get_sso_handler.return_value = MagicMock()
        hs.get_macaroon_generator.return_value = MagicMock()

        with patch.object(oidc_mod, "OidcProvider") as OP, \
             patch.object(oidc_mod, "_parse_oidc_provider_configs",
                          return_value=[MagicMock(idp_id="lemonldap")]):
            handler = OidcHandler(hs)
            self.assertIn("acme.localhost", handler._tenant_providers)
            self.assertIn("lemonldap", handler._tenant_providers["acme.localhost"])
            OP.assert_called()
