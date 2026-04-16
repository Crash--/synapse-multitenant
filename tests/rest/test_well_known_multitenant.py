#
# Copyright 2026 Linagora
#
"""
Probes for tenant-aware .well-known/matrix/server responses.

Bug: ServerWellKnownResource previously cached one response at __init__
based on hs.config.server.server_name. Multi-tenant hosts all got the
global server name. We want per-tenant responses based on ContextVar.
"""

from unittest import TestCase
from unittest.mock import MagicMock

from synapse.config.tenants import TenantConfig
from synapse.tenant_context import set_current_tenant, reset_current_tenant


def _make_tenant(name: str) -> TenantConfig:
    return TenantConfig(
        server_name=f"{name}.localhost",
        database_schema=f"tenant_{name}",
        signing_key_path=f"/keys/{name}.key",
        media_store_path=f"/media/{name}",
    )


def _make_hs() -> MagicMock:
    hs = MagicMock()
    hs.config.server.server_name = "main.localhost"
    hs.config.server.serve_server_wellknown = True
    return hs


class TestWellKnownTenantAware(TestCase):
    def test_returns_tenant_server_name_when_context_set(self) -> None:
        from synapse.rest.well_known import ServerWellKnownResource
        import json

        hs = _make_hs()
        resource = ServerWellKnownResource(hs)

        acme = _make_tenant("acme")
        token = set_current_tenant(acme)
        try:
            request = MagicMock()
            request.setResponseCode = MagicMock()
            request.setHeader = MagicMock()
            body = resource.render_GET(request)
            payload = json.loads(body)
            self.assertEqual(payload["m.server"], "acme.localhost:443")
        finally:
            reset_current_tenant(token)

    def test_falls_back_to_global_without_context(self) -> None:
        from synapse.rest.well_known import ServerWellKnownResource
        import json

        hs = _make_hs()
        resource = ServerWellKnownResource(hs)

        clear_token = set_current_tenant(None)
        try:
            request = MagicMock()
            request.setResponseCode = MagicMock()
            request.setHeader = MagicMock()
            body = resource.render_GET(request)
            payload = json.loads(body)
            self.assertIn("m.server", payload)
            self.assertTrue(payload["m.server"].startswith("main.localhost"))
        finally:
            reset_current_tenant(clear_token)
