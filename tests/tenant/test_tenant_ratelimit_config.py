#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2026 Linagora
#

from twisted.trial import unittest

from synapse.config.tenants import TenantConfig, TenantRatelimitConfig


class TenantRatelimitConfigFromDictTestCase(unittest.TestCase):
    """Parse TenantRatelimitConfig from a YAML-style dict."""

    def test_full_config_round_trip(self):
        d = {
            "rc_message": {"per_second": 0.5, "burst_count": 20},
            "rc_login": {
                "address": {"per_second": 0.01, "burst_count": 10},
                "account": {"per_second": 0.01, "burst_count": 10},
            },
            "rc_joins": {
                "local": {"per_second": 0.2, "burst_count": 15},
            },
        }
        cfg = TenantRatelimitConfig.from_dict(d)
        self.assertIsNotNone(cfg.rc_message)
        self.assertAlmostEqual(cfg.rc_message.per_second, 0.5)
        self.assertEqual(cfg.rc_message.burst_count, 20)
        self.assertIsNotNone(cfg.rc_login_address)
        self.assertAlmostEqual(cfg.rc_login_address.per_second, 0.01)
        self.assertIsNotNone(cfg.rc_joins_local)
        self.assertAlmostEqual(cfg.rc_joins_local.per_second, 0.2)
        # Omitted keys are None (inherit global)
        self.assertIsNone(cfg.rc_registration)
        self.assertIsNone(cfg.rc_invites_per_room)

    def test_empty_dict_all_none(self):
        cfg = TenantRatelimitConfig.from_dict({})
        self.assertIsNone(cfg.rc_message)
        self.assertIsNone(cfg.rc_login_address)
        self.assertIsNone(cfg.rc_joins_local)

    def test_partial_login_only_address(self):
        d = {
            "rc_login": {
                "address": {"per_second": 0.05, "burst_count": 3},
            },
        }
        cfg = TenantRatelimitConfig.from_dict(d)
        self.assertIsNotNone(cfg.rc_login_address)
        self.assertAlmostEqual(cfg.rc_login_address.per_second, 0.05)
        # account not specified → None
        self.assertIsNone(cfg.rc_login_account)


class TenantConfigRatelimitFieldTestCase(unittest.TestCase):
    """TenantConfig.ratelimit field parsing via from_dict."""

    def test_ratelimit_present(self):
        cfg = TenantConfig.from_dict({
            "server_name": "acme.com",
            "signing_key_path": "/tmp/acme.key",
            "ratelimit": {
                "rc_message": {"per_second": 1.0, "burst_count": 50},
            },
        })
        self.assertIsNotNone(cfg.ratelimit)
        self.assertAlmostEqual(cfg.ratelimit.rc_message.per_second, 1.0)

    def test_ratelimit_absent_is_none(self):
        cfg = TenantConfig.from_dict({
            "server_name": "acme.com",
            "signing_key_path": "/tmp/acme.key",
        })
        self.assertIsNone(cfg.ratelimit)

    def test_app_service_config_files_present(self):
        cfg = TenantConfig.from_dict({
            "server_name": "acme.com",
            "signing_key_path": "/tmp/acme.key",
            "app_service_config_files": ["/etc/as/bridge.yaml"],
        })
        self.assertEqual(cfg.app_service_config_files, ["/etc/as/bridge.yaml"])

    def test_app_service_config_files_absent_is_none(self):
        cfg = TenantConfig.from_dict({
            "server_name": "acme.com",
            "signing_key_path": "/tmp/acme.key",
        })
        self.assertIsNone(cfg.app_service_config_files)
