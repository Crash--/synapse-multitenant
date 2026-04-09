#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2026 Linagora
#

from twisted.trial import unittest

from synapse.config.tenants import (
    TenantConfig,
    TenantCasConfig,
    TenantOidcConfig,
    TenantSamlConfig,
)


class TenantOidcConfigTestCase(unittest.TestCase):
    def test_oidc_providers_round_trip(self):
        cfg = TenantConfig.from_dict({
            "server_name": "acme.com",
            "signing_key_path": "/tmp/acme.key",
            "oidc_providers": [
                {
                    "idp_id": "acme-okta",
                    "idp_name": "Acme Okta",
                    "issuer": "https://acme.okta.com/",
                    "client_id": "abc",
                    "client_secret": "xyz",
                    "scopes": ["openid", "profile"],
                },
            ],
        })
        self.assertIsNotNone(cfg.oidc)
        self.assertEqual(len(cfg.oidc.providers), 1)
        self.assertEqual(cfg.oidc.providers[0]["idp_id"], "acme-okta")

    def test_oidc_absent_is_none(self):
        cfg = TenantConfig.from_dict({
            "server_name": "corp.com",
            "signing_key_path": "/tmp/corp.key",
        })
        self.assertIsNone(cfg.oidc)

    def test_multiple_oidc_providers(self):
        cfg = TenantConfig.from_dict({
            "server_name": "acme.com",
            "signing_key_path": "/tmp/acme.key",
            "oidc_providers": [
                {
                    "idp_id": "okta",
                    "idp_name": "Okta",
                    "issuer": "https://okta.example/",
                    "client_id": "a",
                    "client_secret": "b",
                },
                {
                    "idp_id": "azure",
                    "idp_name": "Azure AD",
                    "issuer": "https://login.microsoft.com/",
                    "client_id": "c",
                    "client_secret": "d",
                },
            ],
        })
        self.assertEqual(len(cfg.oidc.providers), 2)


class TenantCasConfigTestCase(unittest.TestCase):
    def test_cas_round_trip(self):
        cfg = TenantConfig.from_dict({
            "server_name": "corp.com",
            "signing_key_path": "/tmp/corp.key",
            "cas": {
                "server_url": "https://cas.corp.com",
                "protocol_version": 3,
                "displayname_attribute": "cn",
                "enable_registration": True,
            },
        })
        self.assertIsNotNone(cfg.cas)
        self.assertEqual(cfg.cas.server_url, "https://cas.corp.com")
        self.assertEqual(cfg.cas.protocol_version, 3)
        self.assertTrue(cfg.cas.enable_registration)

    def test_cas_absent_is_none(self):
        cfg = TenantConfig.from_dict({
            "server_name": "acme.com",
            "signing_key_path": "/tmp/acme.key",
        })
        self.assertIsNone(cfg.cas)


class TenantSamlConfigTestCase(unittest.TestCase):
    def test_saml_round_trip(self):
        cfg = TenantConfig.from_dict({
            "server_name": "edu.com",
            "signing_key_path": "/tmp/edu.key",
            "saml": {
                "idp_entityid": "https://idp.edu.com/saml",
                "session_lifetime": "15m",
            },
        })
        self.assertIsNotNone(cfg.saml)
        self.assertEqual(cfg.saml.idp_entityid, "https://idp.edu.com/saml")

    def test_saml_absent_is_none(self):
        cfg = TenantConfig.from_dict({
            "server_name": "acme.com",
            "signing_key_path": "/tmp/acme.key",
        })
        self.assertIsNone(cfg.saml)
