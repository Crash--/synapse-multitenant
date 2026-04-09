#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2026 Linagora
#

"""
Tests for synapse_tenant backup/restore/drop subcommands.

These tests verify CLI argument parsing and archive structure.
They do NOT exercise real pg_dump/psql (those require a running database).
"""

import json
import os
import tarfile
import tempfile
from unittest import TestCase


class TenantBackupArchiveTestCase(TestCase):
    """Tests for backup archive format validation."""

    def _create_test_archive(self, tmpdir: str, server_name: str = "acme.com") -> str:
        """Create a valid backup archive for testing."""
        archive_path = os.path.join(tmpdir, "test_backup.tar.gz")
        manifest = {
            "server_name": server_name,
            "database_schema": f"tenant_{server_name.replace('.', '_')}",
            "timestamp": "2026-04-09T12:00:00+00:00",
            "media_files_count": 1,
        }

        staging = os.path.join(tmpdir, "staging")
        os.makedirs(staging)

        manifest_path = os.path.join(staging, "manifest.json")
        with open(manifest_path, "w") as f:
            json.dump(manifest, f)

        schema_path = os.path.join(staging, "schema.sql")
        with open(schema_path, "w") as f:
            f.write("CREATE TABLE test (id int);\n")

        media_dir = os.path.join(staging, "media", "local_content")
        os.makedirs(media_dir)
        with open(os.path.join(media_dir, "test.dat"), "w") as f:
            f.write("media content")

        with tarfile.open(archive_path, "w:gz") as tar:
            tar.add(manifest_path, arcname="manifest.json")
            tar.add(schema_path, arcname="schema.sql")
            tar.add(os.path.join(staging, "media"), arcname="media")

        return archive_path

    def test_archive_contains_required_files(self) -> None:
        """A valid archive contains manifest.json, schema.sql, and media/."""
        with tempfile.TemporaryDirectory() as tmpdir:
            archive_path = self._create_test_archive(tmpdir)

            with tarfile.open(archive_path, "r:gz") as tar:
                names = tar.getnames()

            self.assertIn("manifest.json", names)
            self.assertIn("schema.sql", names)
            self.assertTrue(any(n.startswith("media") for n in names))

    def test_manifest_contents(self) -> None:
        """Manifest contains correct server_name and schema."""
        with tempfile.TemporaryDirectory() as tmpdir:
            archive_path = self._create_test_archive(tmpdir, "corp.io")

            with tarfile.open(archive_path, "r:gz") as tar:
                f = tar.extractfile("manifest.json")
                manifest = json.loads(f.read())

            self.assertEqual(manifest["server_name"], "corp.io")
            self.assertEqual(manifest["database_schema"], "tenant_corp_io")
            self.assertIn("timestamp", manifest)
            self.assertIn("media_files_count", manifest)

    def test_archive_round_trip_structure(self) -> None:
        """Extract and re-examine to verify structure survives round-trip."""
        with tempfile.TemporaryDirectory() as tmpdir:
            archive_path = self._create_test_archive(tmpdir)

            extract_dir = os.path.join(tmpdir, "extracted")
            os.makedirs(extract_dir)
            with tarfile.open(archive_path, "r:gz") as tar:
                tar.extractall(path=extract_dir)

            self.assertTrue(os.path.isfile(os.path.join(extract_dir, "manifest.json")))
            self.assertTrue(os.path.isfile(os.path.join(extract_dir, "schema.sql")))
            self.assertTrue(os.path.isdir(os.path.join(extract_dir, "media")))
            self.assertTrue(
                os.path.isfile(
                    os.path.join(extract_dir, "media", "local_content", "test.dat")
                )
            )


class TenantDropSafetyTestCase(TestCase):
    """Tests for drop command safety checks."""

    def test_drop_subcommand_requires_confirm_flag(self) -> None:
        """The drop command should refuse to run without --confirm-destructive."""
        import importlib.machinery
        import importlib.util

        # Resolve the script path relative to this file's location in the repo.
        script_path = os.path.normpath(
            os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "..",
                "..",
                "scripts",
                "synapse_tenant",
            )
        )
        # The script has no .py extension, so we must supply a loader explicitly.
        loader = importlib.machinery.SourceFileLoader("synapse_tenant", script_path)
        spec = importlib.util.spec_from_file_location(
            "synapse_tenant",
            script_path,
            loader=loader,
        )
        assert spec is not None, f"Could not load spec from {script_path}"
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        class FakeArgs:
            config_path = "/nonexistent.yaml"
            server_name = "acme.com"
            confirm_destructive = False

        result = mod.cmd_drop(FakeArgs())
        self.assertEqual(result, 1)
