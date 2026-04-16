#
# Copyright 2026 Linagora
#
"""Red/green probes for phase OIDC — per-tenant SSO login bridge."""

from unittest import TestCase
from unittest.mock import ANY, MagicMock, patch

from synapse.config.tenants import TenantConfig, TenantOidcConfig
from synapse.tenant_context import set_current_tenant, reset_current_tenant


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
            OP.assert_any_call(
                hs,
                ANY,
                ANY,
                tenant_server_name="acme.localhost",
                tenant_public_baseurl=ANY,
            )


class TestSsoHandlerTenantAware(TestCase):
    """SsoHandler must accept per-tenant IdP registrations and filter
    get_identity_providers() by the current tenant ContextVar."""

    def _make_sso_handler(self):
        from synapse.handlers.sso import SsoHandler

        hs = MagicMock()
        hs.config.server.server_name = "main.localhost"
        hs.config.consent.user_consent_at_registration = False
        hs.get_clock.return_value = MagicMock()
        hs.get_auth_handler.return_value = MagicMock()
        hs.get_registration_handler.return_value = MagicMock()
        hs.get_datastores.return_value = MagicMock()
        hs.get_server_notices_manager.return_value = MagicMock()
        hs.get_replication_command_handler.return_value = MagicMock()
        return SsoHandler(hs)

    def _make_idp(self, idp_id: str):
        idp = MagicMock()
        idp.idp_id = idp_id
        return idp

    def test_two_tenants_same_idp_id_no_collision(self) -> None:
        sso = self._make_sso_handler()
        idp_a = self._make_idp("lemonldap")
        idp_b = self._make_idp("lemonldap")
        sso.register_tenant_identity_provider(idp_a, "acme.localhost")
        sso.register_tenant_identity_provider(idp_b, "corp.localhost")
        # Nothing raised = pass.

    def test_get_identity_providers_filters_by_tenant(self) -> None:
        sso = self._make_sso_handler()
        idp_a = self._make_idp("lemonldap")
        idp_b = self._make_idp("lemonldap")
        sso.register_tenant_identity_provider(idp_a, "acme.localhost")
        sso.register_tenant_identity_provider(idp_b, "corp.localhost")

        acme = _make_tenant_with_oidc("acme.localhost", {"idp_id": "lemonldap"})
        token = set_current_tenant(acme)
        try:
            providers = sso.get_identity_providers()
            self.assertIn("lemonldap", providers)
            self.assertIs(providers["lemonldap"], idp_a)
        finally:
            reset_current_tenant(token)

    def test_global_providers_visible_when_no_tenant_context(self) -> None:
        sso = self._make_sso_handler()
        idp_global = self._make_idp("saml-global")
        sso.register_identity_provider(idp_global)
        providers = sso.get_identity_providers()
        self.assertIn("saml-global", providers)

    def test_deregister_tenant_identity_provider(self) -> None:
        sso = self._make_sso_handler()
        idp = self._make_idp("lemonldap")
        sso.register_tenant_identity_provider(idp, "acme.localhost")
        sso.deregister_tenant_identity_provider("acme.localhost", "lemonldap")
        acme = _make_tenant_with_oidc("acme.localhost", {"idp_id": "lemonldap"})
        token = set_current_tenant(acme)
        try:
            self.assertNotIn("lemonldap", sso.get_identity_providers())
        finally:
            reset_current_tenant(token)


class TestOidcProviderTenantScopedSkipsAutoRegister(TestCase):
    """OidcProvider.__init__ must NOT call register_identity_provider when
    tenant_server_name is set — the handler registers explicitly via
    register_tenant_identity_provider."""

    def test_tenant_scoped_oidc_provider_does_not_auto_register(self) -> None:
        """Invoke OidcProvider.__init__ in tenant-scoped mode and assert
        sso.register_identity_provider was NOT called.

        __init__ does a lot of work; we patch out the heavy collaborators
        (ClientAuth, JwtClientSecret, RetryOnExceptionCachedCall) and use
        a real OidcProviderConfig-shaped MagicMock so we reach the
        register guard at the tail of __init__.
        """
        import synapse.handlers.oidc as oidc_mod
        from synapse.handlers.oidc import OidcProvider

        hs = MagicMock()
        hs.config.server.server_name = "main.localhost"
        hs.config.server.public_baseurl = "https://main.localhost/"
        hs.get_proxied_http_client.return_value = MagicMock()
        sso = MagicMock()
        hs.get_sso_handler.return_value = sso
        hs.get_device_handler.return_value = MagicMock()

        provider_cfg = MagicMock()
        provider_cfg.idp_id = "lemonldap"
        provider_cfg.idp_name = "LemonLDAP"
        provider_cfg.idp_icon = None
        provider_cfg.idp_brand = None
        provider_cfg.redirect_uri = None
        provider_cfg.client_secret = "secret"
        provider_cfg.client_secret_jwt_key = None
        provider_cfg.additional_authorization_parameters = ()
        provider_cfg.passthrough_authorization_parameters = ()

        # Make user_mapping_provider_class.__init__ have 2 params so the
        # __init__ branch at line ~508 takes the short form.
        class _FakeUMP:
            def __init__(self, cfg):
                pass
        provider_cfg.user_mapping_provider_class = _FakeUMP
        provider_cfg.user_mapping_provider_config = MagicMock()

        with patch.object(oidc_mod, "ClientAuth") as _CA, \
             patch.object(oidc_mod, "RetryOnExceptionCachedCall") as _RC:
            _CA.return_value = MagicMock()
            _RC.return_value = MagicMock()
            OidcProvider(
                hs,
                MagicMock(),
                provider_cfg,
                tenant_server_name="acme.localhost",
                tenant_public_baseurl="https://acme.localhost/",
            )
        sso.register_identity_provider.assert_not_called()


class TestLoginAdvertisesTenantSso(TestCase):
    """LoginRestServlet.on_GET must advertise m.login.sso for a tenant
    whose OIDC config has providers, even when the global OIDC is off."""

    def _make_login_servlet(self, *, global_oidc_enabled: bool,
                            tenant_providers: dict):
        from synapse.rest.client.login import LoginRestServlet

        hs = MagicMock()
        hs.config.jwt.jwt_enabled = False
        hs.config.cas.cas_enabled = False
        hs.config.saml2.saml2_enabled = False
        hs.config.oidc.oidc_enabled = global_oidc_enabled
        hs.config.registration.refreshable_access_token_lifetime = None
        hs.config.experimental.msc3866.enabled = False
        hs.config.experimental.msc3866.require_approval_for_new_accounts = False
        hs.config.auth.login_via_existing_enabled = False
        hs.get_datastores.return_value.main = MagicMock()
        hs.get_auth.return_value = MagicMock()
        hs.get_clock.return_value = MagicMock()
        hs.get_auth_handler.return_value = MagicMock()
        hs.get_registration_handler.return_value = MagicMock()

        sso = MagicMock()
        sso.get_identity_providers.return_value = tenant_providers
        hs.get_sso_handler.return_value = sso

        oidc_handler = MagicMock()
        oidc_handler._get_providers.return_value = tenant_providers
        oidc_handler.has_providers.return_value = bool(tenant_providers)
        hs.get_oidc_handler.return_value = oidc_handler

        hs.get_module_api_callbacks.return_value = MagicMock()
        hs.get_account_validity_handler.return_value = MagicMock()
        hs.get_tenant_ratelimiter_registry.return_value = MagicMock()

        return LoginRestServlet(hs)

    def test_tenant_with_oidc_gets_sso_flow(self) -> None:
        idp = MagicMock()
        idp.idp_id = "lemonldap"
        idp.idp_name = "LemonLDAP SSO"
        idp.idp_icon = None
        idp.idp_brand = None
        servlet = self._make_login_servlet(
            global_oidc_enabled=False,
            tenant_providers={"lemonldap": idp},
        )
        request = MagicMock()
        _, body = servlet.on_GET(request)
        flows = body["flows"]
        sso_flows = [f for f in flows if f.get("type") == "m.login.sso"]
        self.assertEqual(len(sso_flows), 1)
        ids = [p["id"] for p in sso_flows[0]["identity_providers"]]
        self.assertIn("lemonldap", ids)

    def test_no_sso_when_neither_global_nor_tenant_configured(self) -> None:
        servlet = self._make_login_servlet(
            global_oidc_enabled=False,
            tenant_providers={},
        )
        request = MagicMock()
        _, body = servlet.on_GET(request)
        flows = body["flows"]
        sso_flows = [f for f in flows if f.get("type") == "m.login.sso"]
        self.assertEqual(sso_flows, [])
