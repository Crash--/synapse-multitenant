#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2026 Linagora
#

from twisted.trial import unittest

from synapse.config.tenants import TenantConfig, TenantPushConfig


class TenantPushConfigTestCase(unittest.TestCase):
    def test_push_config_round_trip(self):
        cfg = TenantConfig.from_dict({
            "server_name": "acme.com",
            "signing_key_path": "/tmp/acme.key",
            "push": {
                "include_content": False,
                "enabled": True,
                "group_unread_count_by_room": False,
                "jitter_delay_ms": 5000,
            },
        })
        self.assertIsNotNone(cfg.push)
        self.assertFalse(cfg.push.include_content)
        self.assertTrue(cfg.push.enabled)
        self.assertFalse(cfg.push.group_unread_count_by_room)
        self.assertEqual(cfg.push.jitter_delay_ms, 5000)

    def test_push_absent_is_none(self):
        cfg = TenantConfig.from_dict({
            "server_name": "corp.com",
            "signing_key_path": "/tmp/corp.key",
        })
        self.assertIsNone(cfg.push)

    def test_push_defaults(self):
        cfg = TenantConfig.from_dict({
            "server_name": "acme.com",
            "signing_key_path": "/tmp/acme.key",
            "push": {},
        })
        self.assertIsNotNone(cfg.push)
        self.assertTrue(cfg.push.include_content)
        self.assertTrue(cfg.push.enabled)
        self.assertTrue(cfg.push.group_unread_count_by_room)
        self.assertIsNone(cfg.push.jitter_delay_ms)
