#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2026 Linagora
#

from twisted.trial import unittest

from synapse.config.tenants import TenantConfig, TenantEmailConfig


class TenantEmailConfigTestCase(unittest.TestCase):
    """Tests for TenantEmailConfig dataclass and TenantConfig.email parsing."""

    def test_email_config_round_trip(self):
        """A tenant with an email block should parse into TenantEmailConfig."""
        cfg = TenantConfig.from_dict({
            "server_name": "acme.com",
            "signing_key_path": "/tmp/acme.key",
            "email": {
                "smtp_host": "smtp.acme.com",
                "smtp_port": 587,
                "smtp_user": "noreply@acme.com",
                "smtp_pass": "secret",
                "notif_from": "Acme Chat <noreply@acme.com>",
                "force_tls": True,
                "enable_tls": True,
                "require_transport_security": False,
                "app_name": "AcmeChat",
            },
        })
        self.assertIsNotNone(cfg.email)
        self.assertEqual(cfg.email.smtp_host, "smtp.acme.com")
        self.assertEqual(cfg.email.smtp_port, 587)
        self.assertEqual(cfg.email.smtp_user, "noreply@acme.com")
        self.assertEqual(cfg.email.smtp_pass, "secret")
        self.assertEqual(cfg.email.notif_from, "Acme Chat <noreply@acme.com>")
        self.assertTrue(cfg.email.force_tls)
        self.assertTrue(cfg.email.enable_tls)
        self.assertFalse(cfg.email.require_transport_security)
        self.assertEqual(cfg.email.app_name, "AcmeChat")

    def test_email_config_absent_is_none(self):
        """A tenant without an email block should have email=None (inherit global)."""
        cfg = TenantConfig.from_dict({
            "server_name": "corp.com",
            "signing_key_path": "/tmp/corp.key",
        })
        self.assertIsNone(cfg.email)

    def test_email_config_defaults(self):
        """Minimal email block should fill defaults."""
        cfg = TenantConfig.from_dict({
            "server_name": "acme.com",
            "signing_key_path": "/tmp/acme.key",
            "email": {
                "notif_from": "Acme <noreply@acme.com>",
            },
        })
        self.assertIsNotNone(cfg.email)
        self.assertEqual(cfg.email.smtp_host, "localhost")
        self.assertEqual(cfg.email.smtp_port, 25)
        self.assertIsNone(cfg.email.smtp_user)
        self.assertIsNone(cfg.email.smtp_pass)
        self.assertFalse(cfg.email.force_tls)
        self.assertTrue(cfg.email.enable_tls)
        self.assertEqual(cfg.email.app_name, "Matrix")

    def test_email_config_force_tls_default_port(self):
        """force_tls should default smtp_port to 465."""
        cfg = TenantConfig.from_dict({
            "server_name": "acme.com",
            "signing_key_path": "/tmp/acme.key",
            "email": {
                "notif_from": "Acme <noreply@acme.com>",
                "force_tls": True,
            },
        })
        self.assertEqual(cfg.email.smtp_port, 465)
