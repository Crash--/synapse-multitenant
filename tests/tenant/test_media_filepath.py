#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2026 Linagora
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#
#

"""
Tests for multi-tenant media file paths.

These tests verify that:
1. Media paths are correctly prefixed with tenant server name
2. Paths fall back to default when no tenant is set
3. Path isolation is maintained between tenants
"""

import os
from unittest import TestCase

from synapse.config.tenants import TenantConfig
from synapse.media.filepath import MediaFilePaths
from synapse.media.multitenant_filepath import (
    MultiTenantMediaFilePaths,
    create_media_file_paths,
)
from synapse.tenant_context import reset_current_tenant, tenant_context


class TestMultiTenantMediaFilePaths(TestCase):
    """Tests for MultiTenantMediaFilePaths class."""

    def setUp(self):
        """Reset tenant context and create file paths instance."""
        reset_current_tenant()
        self.base_path = "/var/synapse/media"
        self.file_paths = MultiTenantMediaFilePaths(self.base_path)

    def tearDown(self):
        """Reset tenant context after each test."""
        reset_current_tenant()

    def _create_test_tenant(self, server_name: str) -> TenantConfig:
        """Create a test tenant configuration."""
        return TenantConfig(
            server_name=server_name,
            database_schema=f"tenant_{server_name.replace('.', '_')}",
            signing_key_path=f"/keys/{server_name}.key",
            media_store_path=f"/media/{server_name}",
        )

    def test_local_media_filepath_with_tenant(self):
        """Test local media path includes tenant prefix."""
        tenant = self._create_test_tenant("acme.com")
        media_id = "abcdefghijkl"

        with tenant_context(tenant):
            path = self.file_paths.local_media_filepath(media_id)

        expected = os.path.join(
            self.base_path,
            "acme.com",
            "local_content",
            "ab",
            "cd",
            "efghijkl",
        )
        self.assertEqual(path, expected)

    def test_local_media_filepath_without_tenant(self):
        """Test local media path without tenant falls back to base path."""
        media_id = "abcdefghijkl"

        path = self.file_paths.local_media_filepath(media_id)

        expected = os.path.join(
            self.base_path,
            "local_content",
            "ab",
            "cd",
            "efghijkl",
        )
        self.assertEqual(path, expected)

    def test_local_media_thumbnail_with_tenant(self):
        """Test local thumbnail path includes tenant prefix."""
        tenant = self._create_test_tenant("corp.io")
        media_id = "abcdefghijkl"

        with tenant_context(tenant):
            path = self.file_paths.local_media_thumbnail(
                media_id, 96, 96, "image/png", "scale"
            )

        expected = os.path.join(
            self.base_path,
            "corp.io",
            "local_thumbnails",
            "ab",
            "cd",
            "efghijkl",
            "96-96-image-png-scale",
        )
        self.assertEqual(path, expected)

    def test_remote_media_filepath_with_tenant(self):
        """Test remote media path includes tenant prefix."""
        tenant = self._create_test_tenant("acme.com")
        server_name = "matrix.org"
        file_id = "xyzabcdefgh"

        with tenant_context(tenant):
            path = self.file_paths.remote_media_filepath(server_name, file_id)

        expected = os.path.join(
            self.base_path,
            "acme.com",
            "remote_content",
            server_name,
            "xy",
            "za",
            "bcdefgh",
        )
        self.assertEqual(path, expected)

    def test_url_cache_filepath_with_tenant(self):
        """Test URL cache path includes tenant prefix."""
        tenant = self._create_test_tenant("startup.co")
        media_id = "abcdefghijkl"

        with tenant_context(tenant):
            path = self.file_paths.url_cache_filepath(media_id)

        expected = os.path.join(
            self.base_path,
            "startup.co",
            "url_cache",
            "ab",
            "cd",
            "efghijkl",
        )
        self.assertEqual(path, expected)

    def test_url_cache_filepath_new_format_with_tenant(self):
        """Test URL cache path with new date format includes tenant prefix."""
        tenant = self._create_test_tenant("acme.com")
        media_id = "2024-01-15-randomstring"

        with tenant_context(tenant):
            path = self.file_paths.url_cache_filepath(media_id)

        expected = os.path.join(
            self.base_path,
            "acme.com",
            "url_cache",
            "2024-01-15",
            "randomstring",
        )
        self.assertEqual(path, expected)

    def test_path_isolation_between_tenants(self):
        """Test that different tenants have different paths."""
        tenant1 = self._create_test_tenant("tenant1.com")
        tenant2 = self._create_test_tenant("tenant2.com")
        media_id = "abcdefghijkl"

        with tenant_context(tenant1):
            path1 = self.file_paths.local_media_filepath(media_id)

        with tenant_context(tenant2):
            path2 = self.file_paths.local_media_filepath(media_id)

        self.assertNotEqual(path1, path2)
        self.assertIn("tenant1.com", path1)
        self.assertIn("tenant2.com", path2)

    def test_relative_path_methods(self):
        """Test that relative path methods work correctly."""
        tenant = self._create_test_tenant("acme.com")
        media_id = "abcdefghijkl"

        with tenant_context(tenant):
            rel_path = self.file_paths.local_media_filepath_rel(media_id)

        # Relative path should not include base_path or tenant
        expected = os.path.join(
            "local_content",
            "ab",
            "cd",
            "efghijkl",
        )
        self.assertEqual(rel_path, expected)


class TestMediaFilePathsFactory(TestCase):
    """Tests for the create_media_file_paths factory function."""

    def test_creates_regular_paths_when_disabled(self):
        """Test factory creates regular MediaFilePaths when multi-tenant disabled."""
        file_paths = create_media_file_paths("/media", multi_tenant_enabled=False)
        self.assertIsInstance(file_paths, MediaFilePaths)
        self.assertNotIsInstance(file_paths, MultiTenantMediaFilePaths)

    def test_creates_multi_tenant_paths_when_enabled(self):
        """Test factory creates MultiTenantMediaFilePaths when multi-tenant enabled."""
        file_paths = create_media_file_paths("/media", multi_tenant_enabled=True)
        self.assertIsInstance(file_paths, MultiTenantMediaFilePaths)


class TestTenantMediaPathDirectory(TestCase):
    """Tests for tenant media directory methods."""

    def setUp(self):
        """Reset tenant context and create file paths instance."""
        reset_current_tenant()
        self.base_path = "/var/synapse/media"
        self.file_paths = MultiTenantMediaFilePaths(self.base_path)

    def tearDown(self):
        """Reset tenant context after each test."""
        reset_current_tenant()

    def _create_test_tenant(self, server_name: str) -> TenantConfig:
        """Create a test tenant configuration."""
        return TenantConfig(
            server_name=server_name,
            database_schema=f"tenant_{server_name.replace('.', '_')}",
            signing_key_path=f"/keys/{server_name}.key",
            media_store_path=f"/media/{server_name}",
        )

    def test_local_media_thumbnail_dir_with_tenant(self):
        """Test local thumbnail directory includes tenant prefix."""
        tenant = self._create_test_tenant("acme.com")
        media_id = "abcdefghijkl"

        with tenant_context(tenant):
            path = self.file_paths.local_media_thumbnail_dir(media_id)

        expected = os.path.join(
            self.base_path,
            "acme.com",
            "local_thumbnails",
            "ab",
            "cd",
            "efghijkl",
        )
        self.assertEqual(path, expected)

    def test_remote_media_thumbnail_dir_with_tenant(self):
        """Test remote thumbnail directory includes tenant prefix."""
        tenant = self._create_test_tenant("corp.io")
        server_name = "matrix.org"
        file_id = "xyzabcdefgh"

        with tenant_context(tenant):
            path = self.file_paths.remote_media_thumbnail_dir(server_name, file_id)

        expected = os.path.join(
            self.base_path,
            "corp.io",
            "remote_thumbnail",
            server_name,
            "xy",
            "za",
            "bcdefgh",
        )
        self.assertEqual(path, expected)

    def test_url_cache_filepath_dirs_to_delete_with_tenant(self):
        """Test URL cache dirs to delete includes tenant prefix."""
        tenant = self._create_test_tenant("acme.com")
        media_id = "abcdefghijkl"

        with tenant_context(tenant):
            dirs = self.file_paths.url_cache_filepath_dirs_to_delete(media_id)

        tenant_base = os.path.join(self.base_path, "acme.com")
        self.assertTrue(all(d.startswith(tenant_base) for d in dirs))
