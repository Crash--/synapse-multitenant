#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2026 Linagora
#

"""Tests for TenantConfig.from_db_row() and database-source config parsing."""

from twisted.trial import unittest

from synapse.config._base import ConfigError
from synapse.config.tenants import (
    MultiTenantConfig,
    TenantConfig,
    TenantsConfig,
)


class TenantConfigFromDbRowTestCase(unittest.TestCase):
    """Test TenantConfig.from_db_row() constructs correctly from DB rows."""

    def _make_row(self, **overrides):
        """Build a minimal valid DB row dict."""
        base = {
            "server_name": "acme.localhost",
            "database_schema": "tenant_acme_localhost",
            "media_store_path": "/media/acme.localhost",
            "signing_key_data": "ed25519 a_acme dGVzdGtleQ",
            "registration_enabled": False,
            "enable_federation": True,
            "max_mau_value": 0,
        }
        base.update(overrides)
        return base

    def test_basic_round_trip(self):
        row = self._make_row()
        cfg = TenantConfig.from_db_row(row)
        self.assertEqual(cfg.server_name, "acme.localhost")
        self.assertEqual(cfg.database_schema, "tenant_acme_localhost")
        self.assertEqual(cfg.media_store_path, "/media/acme.localhost")
        self.assertEqual(cfg.signing_key_data, "ed25519 a_acme dGVzdGtleQ")
        self.assertIsNone(cfg.signing_key_path)

    def test_signing_key_path_is_none(self):
        """DB-sourced tenants must have signing_key_path=None."""
        cfg = TenantConfig.from_db_row(self._make_row())
        self.assertIsNone(cfg.signing_key_path)

    def test_missing_server_name_raises(self):
        row = self._make_row()
        del row["server_name"]
        self.assertRaises(ConfigError, TenantConfig.from_db_row, row)

    def test_missing_database_schema_raises(self):
        row = self._make_row()
        del row["database_schema"]
        self.assertRaises(ConfigError, TenantConfig.from_db_row, row)

    def test_missing_signing_key_data_raises(self):
        row = self._make_row()
        del row["signing_key_data"]
        self.assertRaises(ConfigError, TenantConfig.from_db_row, row)

    def test_missing_media_store_path_raises(self):
        row = self._make_row()
        del row["media_store_path"]
        self.assertRaises(ConfigError, TenantConfig.from_db_row, row)

    def test_optional_fields_default(self):
        cfg = TenantConfig.from_db_row(self._make_row())
        self.assertIsNone(cfg.macaroon_secret_key)
        self.assertIsNone(cfg.form_secret)
        self.assertIsNone(cfg.registration_shared_secret)
        self.assertIsNone(cfg.public_baseurl)
        self.assertIsNone(cfg.email)
        self.assertIsNone(cfg.oidc)
        self.assertIsNone(cfg.cas)
        self.assertIsNone(cfg.saml)
        self.assertIsNone(cfg.push)
        self.assertIsNone(cfg.ratelimit)
        self.assertIsNone(cfg.app_service_config_files)

    def test_email_config_parsed(self):
        row = self._make_row(
            email_config={"notif_from": "noreply@acme.com", "smtp_host": "mail.acme.com"}
        )
        cfg = TenantConfig.from_db_row(row)
        self.assertIsNotNone(cfg.email)
        self.assertEqual(cfg.email.notif_from, "noreply@acme.com")
        self.assertEqual(cfg.email.smtp_host, "mail.acme.com")

    def test_push_config_parsed(self):
        row = self._make_row(push_config={"enabled": False, "include_content": False})
        cfg = TenantConfig.from_db_row(row)
        self.assertIsNotNone(cfg.push)
        self.assertFalse(cfg.push.enabled)
        self.assertFalse(cfg.push.include_content)

    def test_ratelimit_config_parsed(self):
        row = self._make_row(
            ratelimit_config={"rc_message": {"per_second": 5, "burst_count": 20}}
        )
        cfg = TenantConfig.from_db_row(row)
        self.assertIsNotNone(cfg.ratelimit)
        self.assertIsNotNone(cfg.ratelimit.rc_message)
        self.assertEqual(cfg.ratelimit.rc_message.per_second, 5.0)

    def test_frozen_immutability(self):
        cfg = TenantConfig.from_db_row(self._make_row())
        with self.assertRaises(AttributeError):
            cfg.server_name = "other.localhost"


class MultiTenantConfigSourceTestCase(unittest.TestCase):
    """Test MultiTenantConfig source and reload_secret fields."""

    def test_default_source_is_yaml(self):
        mt = MultiTenantConfig(enabled=True)
        self.assertEqual(mt.source, "yaml")

    def test_database_source(self):
        mt = MultiTenantConfig(enabled=True, source="database")
        self.assertEqual(mt.source, "database")

    def test_reload_secret(self):
        mt = MultiTenantConfig(
            enabled=True, source="database", reload_secret="s3cret"
        )
        self.assertEqual(mt.reload_secret, "s3cret")


class TenantsConfigDatabaseSourceTestCase(unittest.TestCase):
    """Test that TenantsConfig.read_config handles source='database'."""

    def test_database_source_allows_zero_tenants(self):
        """With source=database, no tenants in YAML is valid."""
        tc = TenantsConfig(None)
        tc.read_config({
            "multi_tenant": {
                "enabled": True,
                "source": "database",
                "reload_secret": "test",
            },
        })
        self.assertTrue(tc.multi_tenant.enabled)
        self.assertEqual(tc.multi_tenant.source, "database")
        self.assertEqual(len(tc.multi_tenant.tenants), 0)

    def test_yaml_source_requires_tenants(self):
        """With source=yaml (default), empty tenants raises ConfigError."""
        tc = TenantsConfig(None)
        self.assertRaises(
            ConfigError,
            tc.read_config,
            {"multi_tenant": {"enabled": True, "source": "yaml"}},
        )

    def test_invalid_source_raises(self):
        tc = TenantsConfig(None)
        self.assertRaises(
            ConfigError,
            tc.read_config,
            {"multi_tenant": {"enabled": True, "source": "redis"}},
        )
