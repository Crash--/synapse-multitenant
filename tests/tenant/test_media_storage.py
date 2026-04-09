#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2026 Linagora
#

"""
Tests for tenant-aware MediaStorage._local_path.

Verifies:
1. _local_path includes tenant prefix when tenant context is set
2. _local_path returns global path when no tenant context
3. Different tenants get different paths
"""

import os
import sys
import tempfile
from unittest import TestCase
from unittest.mock import MagicMock

# Stub out heavy transitive dependencies before importing MediaStorage
for _mod in (
    "synapse.media._base",
    "synapse.logging.context",
    "synapse.logging.opentracing",
    "synapse.util.async_helpers",
    "synapse.util.file_consumer",
    "synapse.util.clock",
    "synapse.util.duration",
    "synapse.types",
    "synapse.api.errors",
    "synapse.http.server",
    "zope.interface",
    "twisted.internet",
    "twisted.internet.interfaces",
    "twisted.internet.defer",
):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()  # type: ignore[assignment]

# Provide minimal symbols needed by media_storage.py imports
sys.modules["synapse.media._base"].FileInfo = object  # type: ignore[attr-defined]
sys.modules["synapse.media._base"].Responder = object  # type: ignore[attr-defined]
sys.modules["synapse.media._base"].ThreadedFileSender = object  # type: ignore[attr-defined]
sys.modules["synapse.logging.context"].defer_to_thread = MagicMock()  # type: ignore[attr-defined]
sys.modules["synapse.logging.context"].run_in_background = MagicMock()  # type: ignore[attr-defined]
sys.modules["synapse.logging.opentracing"].start_active_span = MagicMock()  # type: ignore[attr-defined]
sys.modules["synapse.logging.opentracing"].trace = lambda f: f  # type: ignore[attr-defined]
sys.modules["synapse.logging.opentracing"].trace_with_opname = lambda *a, **kw: (lambda f: f)  # type: ignore[attr-defined]
sys.modules["synapse.util.async_helpers"].maybe_awaitable = MagicMock()  # type: ignore[attr-defined]

from synapse.config.tenants import TenantConfig
from synapse.media.media_storage import MediaStorage
from synapse.media.filepath import MediaFilePaths
from synapse.media.storage_provider import FileStorageProviderBackend
from synapse.tenant_context import set_current_tenant, tenant_context


def _make_tenant(server_name: str) -> TenantConfig:
    return TenantConfig(
        server_name=server_name,
        database_schema=f"tenant_{server_name.replace('.', '_')}",
        signing_key_path=f"/keys/{server_name}.key",
        media_store_path=f"/media/{server_name}",
    )


class TestMediaStorageLocalPath(TestCase):
    """Tests for MediaStorage._local_path tenant awareness."""

    def setUp(self) -> None:
        set_current_tenant(None)
        self.tmpdir = tempfile.mkdtemp()
        self.base_dir = os.path.join(self.tmpdir, "media")
        os.makedirs(self.base_dir)

        self.hs = MagicMock()
        self.hs.config.media.media_store_path = self.base_dir
        self.hs.get_reactor.return_value = MagicMock()
        self.hs.get_clock.return_value = MagicMock()
        self.hs.get_module_api_callbacks.return_value = MagicMock()

        filepaths = MediaFilePaths(self.base_dir)
        local_provider = FileStorageProviderBackend(self.hs, self.base_dir)

        self.media_storage = MediaStorage(
            self.hs, filepaths, [], local_provider
        )

    def tearDown(self) -> None:
        set_current_tenant(None)
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_local_path_with_tenant(self) -> None:
        """_local_path includes tenant server_name when tenant context is set."""
        tenant = _make_tenant("acme.com")
        rel_path = "local_content/ab/cd/efghijkl"

        with tenant_context(tenant):
            result = self.media_storage._local_path(rel_path)

        expected = os.path.join(self.base_dir, "acme.com", rel_path)
        self.assertEqual(result, expected)

    def test_local_path_without_tenant(self) -> None:
        """_local_path returns base + path when no tenant context is set."""
        rel_path = "local_content/ab/cd/efghijkl"
        result = self.media_storage._local_path(rel_path)
        expected = os.path.join(self.base_dir, rel_path)
        self.assertEqual(result, expected)

    def test_local_path_isolation(self) -> None:
        """Different tenants produce different local paths."""
        tenant_a = _make_tenant("acme.com")
        tenant_b = _make_tenant("corp.com")
        rel_path = "local_content/ab/cd/efghijkl"

        with tenant_context(tenant_a):
            path_a = self.media_storage._local_path(rel_path)
        with tenant_context(tenant_b):
            path_b = self.media_storage._local_path(rel_path)

        self.assertNotEqual(path_a, path_b)
        self.assertIn("acme.com", path_a)
        self.assertIn("corp.com", path_b)
