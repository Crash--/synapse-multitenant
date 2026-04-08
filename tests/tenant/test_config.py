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


class TenantConfigServerNoticesMxidTestCase(unittest.TestCase):
    """Phase 2c-B: per-tenant server-notices sender MXID.

    The `server_notices_mxid` field + `effective_server_notices_mxid`
    accessor is what `ServerNoticesManager` now reads via the
    ContextVar. Under multi-tenant a notice sent on `corp.localhost`
    must carry `@notices:corp.localhost` (not the primary
    `@notices:acme.localhost`) as its `sender` — otherwise the stored
    event is corrupted with a wrong-tenant author.
    """

    def test_server_notices_mxid_round_trip(self):
        cfg = TenantConfig.from_dict({
            "server_name": "corp.localhost",
            "signing_key_path": "/tmp/corp.key",
            "server_notices_mxid": "@server:corp.localhost",
        })
        self.assertEqual(cfg.server_notices_mxid, "@server:corp.localhost")
        self.assertEqual(
            cfg.effective_server_notices_mxid, "@server:corp.localhost"
        )

    def test_server_notices_mxid_absent_defaults_to_tenant_domain(self):
        """An unset field must default to `@notices:{server_name}`
        so the MXID lives on the sending tenant's own domain instead
        of the primary tenant's."""
        cfg = TenantConfig.from_dict({
            "server_name": "corp.localhost",
            "signing_key_path": "/tmp/corp.key",
        })
        self.assertIsNone(cfg.server_notices_mxid)
        self.assertEqual(
            cfg.effective_server_notices_mxid, "@notices:corp.localhost"
        )

    def test_server_notices_mxid_default_is_tenant_specific(self):
        """Two tenants without an explicit MXID must resolve to two
        different MXIDs -- the whole point of phase 2c-B is that the
        primary tenant's MXID can no longer leak into a non-primary
        tenant's stored events."""
        acme = TenantConfig.from_dict({
            "server_name": "acme.localhost",
            "signing_key_path": "/tmp/acme.key",
        })
        corp = TenantConfig.from_dict({
            "server_name": "corp.localhost",
            "signing_key_path": "/tmp/corp.key",
        })
        self.assertNotEqual(
            acme.effective_server_notices_mxid,
            corp.effective_server_notices_mxid,
        )
