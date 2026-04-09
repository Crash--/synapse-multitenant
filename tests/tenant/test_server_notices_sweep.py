#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2026 Linagora
#

from twisted.trial import unittest

from synapse.config.tenants import TenantConfig
from synapse.tenant_context import tenant_context, get_current_tenant


class ServerNoticesMxidSweepTestCase(unittest.TestCase):
    """Phase 2c-B deferred: verify that server_notices_mxid reads
    resolve from tenant context, not global config."""

    def _make_tenants(self):
        acme = TenantConfig.from_dict({
            "server_name": "acme.localhost",
            "signing_key_path": "/tmp/acme.key",
            "server_notices_mxid": "@notices:acme.localhost",
        })
        corp = TenantConfig.from_dict({
            "server_name": "corp.localhost",
            "signing_key_path": "/tmp/corp.key",
        })
        return acme, corp

    def test_tenant_context_resolves_correct_notices_mxid(self):
        acme, corp = self._make_tenants()
        with tenant_context(acme):
            t = get_current_tenant()
            self.assertEqual(t.effective_server_notices_mxid, "@notices:acme.localhost")
        with tenant_context(corp):
            t = get_current_tenant()
            self.assertEqual(t.effective_server_notices_mxid, "@notices:corp.localhost")

    def test_no_cross_tenant_mxid_leak(self):
        acme, corp = self._make_tenants()
        with tenant_context(acme):
            mxid_a = get_current_tenant().effective_server_notices_mxid
        with tenant_context(corp):
            mxid_c = get_current_tenant().effective_server_notices_mxid
        self.assertNotEqual(mxid_a, mxid_c)
