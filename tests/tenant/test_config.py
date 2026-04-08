#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2026 Linagora
#

from twisted.trial import unittest

from synapse.config.tenants import TenantConfig


class TenantConfigIdentityServerTestCase(unittest.TestCase):
    def test_identity_server_field_round_trip(self):
        cfg = TenantConfig.from_dict({
            "server_name": "acme.com",
            "signing_key_path": "/tmp/acme.key",
            "identity_server": "https://matrix.org",
        })
        self.assertEqual(cfg.identity_server, "https://matrix.org")
        self.assertEqual(cfg.effective_identity_server, "https://matrix.org")

    def test_identity_server_absent_returns_none(self):
        cfg = TenantConfig.from_dict({
            "server_name": "acme.com",
            "signing_key_path": "/tmp/acme.key",
        })
        self.assertIsNone(cfg.identity_server)
        self.assertIsNone(cfg.effective_identity_server)
