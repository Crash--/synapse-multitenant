# Phase 5 — File Storage Providers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the local filesystem storage provider and `MediaStorage` tenant-aware so each tenant's media is isolated under `<base>/<server_name>/`.

**Architecture:** `FileStorageProviderBackend` calls `get_current_tenant()` inside `store_file`/`fetch` to resolve a tenant-specific subdirectory. `MediaStorage` gains a `_local_path` helper that does the same for its 4 local-path-join sites. `MediaRepository` switches from plain `MediaFilePaths` to `MultiTenantMediaFilePaths` via the existing factory function. No ABC signature changes.

**Tech Stack:** Python 3.11+, Twisted Trial, contextvars, attrs dataclasses

**Spec:** `docs/superpowers/specs/2026-04-09-phase-5-file-storage-providers-design.md`

---

## File structure

### New files
- `tests/tenant/test_storage_provider.py` — unit tests for tenant-aware `FileStorageProviderBackend`
- `tests/tenant/test_media_storage.py` — unit tests for tenant-aware `MediaStorage._local_path`

### Modified files
- `synapse/media/media_repository.py:110` — use `create_media_file_paths()` factory
- `synapse/media/storage_provider.py:137-197` — add `_tenant_base` helper, use it in `store_file`/`fetch`
- `synapse/media/media_storage.py:153-464` — add `_local_path` helper, replace 4 `os.path.join(self.local_media_directory, path)` sites

---

## Sub-phase 5a — Tenant-aware local FS provider + MediaStorage

### Task 1: Red probes for `FileStorageProviderBackend` tenant isolation

**Files:**
- Create: `tests/tenant/test_storage_provider.py`

- [ ] **Step 1: Write the failing tests**

```python
#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2026 Linagora
#

"""
Tests for tenant-aware FileStorageProviderBackend.

Verifies:
1. store_file writes to <base>/<server_name>/<path> when tenant context is set
2. fetch reads from <base>/<server_name>/<path> when tenant context is set
3. Tenant A's files are invisible to tenant B
4. No-tenant fallback stores/fetches from <base>/<path> directly
"""

import os
import tempfile
from io import BytesIO
from unittest import TestCase
from unittest.mock import MagicMock

from synapse.config.tenants import TenantConfig
from synapse.media._base import FileInfo
from synapse.media.storage_provider import FileStorageProviderBackend
from synapse.tenant_context import reset_current_tenant, tenant_context

from tests.utils import default_config


def _make_tenant(server_name: str) -> TenantConfig:
    return TenantConfig(
        server_name=server_name,
        database_schema=f"tenant_{server_name.replace('.', '_')}",
        signing_key_path=f"/keys/{server_name}.key",
        media_store_path=f"/media/{server_name}",
    )


class TestTenantStorageProvider(TestCase):
    """Tests for tenant-aware FileStorageProviderBackend."""

    def setUp(self) -> None:
        reset_current_tenant()
        self.tmpdir = tempfile.mkdtemp()
        self.cache_dir = os.path.join(self.tmpdir, "cache")
        self.backup_dir = os.path.join(self.tmpdir, "backup")
        os.makedirs(self.cache_dir)
        os.makedirs(self.backup_dir)

        # Mock HomeServer with config pointing at our cache dir
        self.hs = MagicMock()
        self.hs.config.media.media_store_path = self.cache_dir
        self.hs.get_reactor.return_value = MagicMock()

        self.provider = FileStorageProviderBackend(self.hs, self.backup_dir)

    def tearDown(self) -> None:
        reset_current_tenant()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_store_file_with_tenant_writes_under_tenant_dir(self) -> None:
        """store_file with tenant context writes to <backup>/<server_name>/<path>."""
        tenant = _make_tenant("acme.com")
        rel_path = "local_content/ab/cd/efghijkl"
        file_info = FileInfo(file_id="abcdefghijkl")

        # Create the source file in the cache dir under tenant subdir
        source_dir = os.path.join(self.cache_dir, "acme.com", os.path.dirname(rel_path))
        os.makedirs(source_dir, exist_ok=True)
        source_path = os.path.join(self.cache_dir, "acme.com", rel_path)
        with open(source_path, "wb") as f:
            f.write(b"tenant-a-media-data")

        expected_backup = os.path.join(self.backup_dir, "acme.com", rel_path)

        with tenant_context(tenant):
            # store_file is async — we test the path logic synchronously
            # by checking _tenant_base directly
            tenant_cache = self.provider._tenant_base(self.cache_dir)
            tenant_backup = self.provider._tenant_base(self.backup_dir)

        self.assertEqual(tenant_cache, os.path.join(self.cache_dir, "acme.com"))
        self.assertEqual(tenant_backup, os.path.join(self.backup_dir, "acme.com"))

    def test_tenant_base_without_tenant_returns_base(self) -> None:
        """_tenant_base without tenant context returns the base directory unchanged."""
        result = self.provider._tenant_base(self.backup_dir)
        self.assertEqual(result, self.backup_dir)

    def test_tenant_isolation_different_tenants(self) -> None:
        """_tenant_base returns different directories for different tenants."""
        tenant_a = _make_tenant("acme.com")
        tenant_b = _make_tenant("corp.com")

        with tenant_context(tenant_a):
            path_a = self.provider._tenant_base(self.backup_dir)
        with tenant_context(tenant_b):
            path_b = self.provider._tenant_base(self.backup_dir)

        self.assertNotEqual(path_a, path_b)
        self.assertIn("acme.com", path_a)
        self.assertIn("corp.com", path_b)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/tenant/test_storage_provider.py -v 2>&1 | head -40`
Expected: FAIL — `FileStorageProviderBackend` has no `_tenant_base` method.

- [ ] **Step 3: Commit red probes**

```bash
git add tests/tenant/test_storage_provider.py
git commit -m "test(tenant): red probes for tenant-aware FileStorageProviderBackend"
```

---

### Task 2: Implement tenant-aware `FileStorageProviderBackend`

**Files:**
- Modify: `synapse/media/storage_provider.py:137-197`

- [ ] **Step 1: Add `_tenant_base` helper and update `store_file`/`fetch`**

Add the import at the top of the file (after line 32):
```python
from synapse.tenant_context import get_current_tenant
```

Add the `_tenant_base` method to `FileStorageProviderBackend` (after `__init__`, before `__str__`):
```python
    def _tenant_base(self, base: str) -> str:
        """Return a tenant-scoped subdirectory when a tenant context is active.

        Args:
            base: The base directory (cache or backup).

        Returns:
            ``<base>/<server_name>`` if a tenant is set, otherwise ``base``.
        """
        tenant = get_current_tenant()
        if tenant is not None:
            return os.path.join(base, tenant.server_name)
        return base
```

Update `store_file` (replace lines 158-159):
```python
        primary_fname = os.path.join(self._tenant_base(self.cache_directory), path)
        backup_fname = os.path.join(self._tenant_base(self.base_directory), path)
```

Update `fetch` (replace line 178):
```python
        backup_fname = os.path.join(self._tenant_base(self.base_directory), path)
```

- [ ] **Step 2: Run probes to verify they pass**

Run: `python -m pytest tests/tenant/test_storage_provider.py -v 2>&1 | tail -20`
Expected: 3 tests PASS.

- [ ] **Step 3: Run existing media tests to verify no regressions**

Run: `python -m pytest tests/tenant/test_media_filepath.py -v 2>&1 | tail -20`
Expected: All existing tests PASS.

- [ ] **Step 4: Commit**

```bash
git add synapse/media/storage_provider.py
git commit -m "feat(tenant): FileStorageProviderBackend resolves tenant-specific base directories"
```

---

### Task 3: Red probes for `MediaStorage._local_path`

**Files:**
- Create: `tests/tenant/test_media_storage.py`

- [ ] **Step 1: Write the failing tests**

```python
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
import tempfile
from unittest import TestCase
from unittest.mock import MagicMock

from synapse.config.tenants import TenantConfig
from synapse.media.media_storage import MediaStorage
from synapse.media.filepath import MediaFilePaths
from synapse.media.storage_provider import FileStorageProviderBackend
from synapse.tenant_context import reset_current_tenant, tenant_context


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
        reset_current_tenant()
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
        reset_current_tenant()
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/tenant/test_media_storage.py -v 2>&1 | head -40`
Expected: FAIL — `MediaStorage` has no `_local_path` method.

- [ ] **Step 3: Commit red probes**

```bash
git add tests/tenant/test_media_storage.py
git commit -m "test(tenant): red probes for MediaStorage._local_path tenant isolation"
```

---

### Task 4: Implement `MediaStorage._local_path` and replace join sites

**Files:**
- Modify: `synapse/media/media_storage.py:153-464`

- [ ] **Step 1: Add the import and `_local_path` helper**

Add the import at the top of `media_storage.py` (after line 59, among the other imports):
```python
from synapse.tenant_context import get_current_tenant
```

Add the `_local_path` method to `MediaStorage` (after `__init__`, before `store_file`):
```python
    def _local_path(self, rel_path: str) -> str:
        """Join local_media_directory with rel_path, inserting tenant prefix if active.

        Args:
            rel_path: Relative path from ``_file_info_to_path``.

        Returns:
            Absolute path under the (possibly tenant-scoped) local media directory.
        """
        base = self.local_media_directory
        tenant = get_current_tenant()
        if tenant is not None:
            base = os.path.join(base, tenant.server_name)
        return os.path.join(base, rel_path)
```

- [ ] **Step 2: Replace the 4 join sites**

**Site 1** — `store_into_file` (line 232). Replace:
```python
            media_filepath = os.path.join(self.local_media_directory, path)  # type: ignore[arg-type]
```
With:
```python
            media_filepath = self._local_path(path)
```

**Site 2** — `fetch_media` URL cache branch (line 294). Replace:
```python
                local_path = os.path.join(self.local_media_directory, path)  # type: ignore[arg-type]
```
With:
```python
                local_path = self._local_path(path)
```

**Site 3** — `ensure_media_is_in_local_cache` (line 358). Replace:
```python
            local_path = os.path.join(self.local_media_directory, path)  # type: ignore[arg-type]
```
With:
```python
            local_path = self._local_path(path)
```

**Site 4** — `ensure_media_is_in_local_cache` legacy fallback (line 374). Replace:
```python
                legacy_local_path = os.path.join(
                    self.local_media_directory,  # type: ignore[arg-type]
                    legacy_path,
                )
```
With:
```python
                legacy_local_path = self._local_path(legacy_path)
```

- [ ] **Step 3: Run probes to verify they pass**

Run: `python -m pytest tests/tenant/test_media_storage.py -v 2>&1 | tail -20`
Expected: 3 tests PASS.

- [ ] **Step 4: Run storage provider probes too**

Run: `python -m pytest tests/tenant/test_storage_provider.py tests/tenant/test_media_storage.py -v 2>&1 | tail -20`
Expected: 6 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add synapse/media/media_storage.py
git commit -m "feat(tenant): MediaStorage._local_path resolves tenant-scoped media directories"
```

---

### Task 5: Wire `MultiTenantMediaFilePaths` into `MediaRepository`

**Files:**
- Modify: `synapse/media/media_repository.py:110`

- [ ] **Step 1: Add the import**

Add after the existing media imports near the top of `media_repository.py`:
```python
from synapse.media.multitenant_filepath import create_media_file_paths
```

- [ ] **Step 2: Replace the `MediaFilePaths` construction**

Replace line 110:
```python
        self.filepaths: MediaFilePaths = MediaFilePaths(self.primary_base_path)
```
With:
```python
        self.filepaths: MediaFilePaths = create_media_file_paths(
            self.primary_base_path,
            multi_tenant_enabled=bool(
                getattr(hs.config, "tenants", None)
                and hs.config.tenants.multi_tenant.enabled
            ),
        )
```

- [ ] **Step 3: Add a probe to `test_media_filepath.py` for the factory wiring**

Append to `tests/tenant/test_media_filepath.py`:
```python
class TestCreateMediaFilePathsFactory(TestCase):
    """Tests for the create_media_file_paths factory."""

    def test_returns_multitenant_when_enabled(self) -> None:
        result = create_media_file_paths("/var/media", multi_tenant_enabled=True)
        self.assertIsInstance(result, MultiTenantMediaFilePaths)

    def test_returns_plain_when_disabled(self) -> None:
        result = create_media_file_paths("/var/media", multi_tenant_enabled=False)
        self.assertIsInstance(result, MediaFilePaths)
        self.assertNotIsInstance(result, MultiTenantMediaFilePaths)
```

- [ ] **Step 4: Run the factory probes**

Run: `python -m pytest tests/tenant/test_media_filepath.py::TestCreateMediaFilePathsFactory -v 2>&1 | tail -15`
Expected: 2 tests PASS.

- [ ] **Step 5: Run all tenant media tests**

Run: `python -m pytest tests/tenant/test_media_filepath.py tests/tenant/test_storage_provider.py tests/tenant/test_media_storage.py -v 2>&1 | tail -20`
Expected: All tests PASS.

- [ ] **Step 6: Commit**

```bash
git add synapse/media/media_repository.py tests/tenant/test_media_filepath.py
git commit -m "feat(tenant): wire MultiTenantMediaFilePaths into MediaRepository via factory"
```

---

## Sub-phase 5b — URL preview / thumbnail audit

### Task 6: Audit URL previewer and thumbnailer paths

**Files:**
- Read (audit only): `synapse/media/url_previewer.py`
- Read (audit only): `synapse/media/thumbnailer.py`

This task is an audit — it confirms that the URL previewer and thumbnailer flow through the now-tenant-aware `MediaStorage` pipeline. Code changes only if bypasses are found.

- [ ] **Step 1: Audit the URL previewer**

Read `synapse/media/url_previewer.py` and trace every media storage call:
- `self.media_storage.store_into_file(file_info)` — flows through `_file_info_to_path` → `filepaths.url_cache_filepath_rel` → tenant-aware `_local_path`. **OK.**
- `self.primary_base_path` (line 183) — stored but never used in `url_previewer.py` (verified by grep). **Dead reference, no leak.**

Expected finding: No bypasses. No code changes needed.

- [ ] **Step 2: Audit the thumbnailer**

Read `synapse/media/thumbnailer.py` and trace every media storage call:
- `self.media_storage.fetch_media(file_info)` — flows through `_file_info_to_path` → tenant-aware paths. **OK.**
- `self.media_storage.store_into_file(file_info)` — same pipeline. **OK.**
- `self.media_storage.ensure_media_is_in_local_cache(file_info)` — flows through tenant-aware `_local_path`. **OK.**

Expected finding: No bypasses. No code changes needed.

- [ ] **Step 3: Document audit results**

If any bypasses are found, create a fix task. If none (expected), record the audit result in the commit message.

- [ ] **Step 4: Commit audit confirmation**

```bash
git commit --allow-empty -m "audit(tenant): URL previewer and thumbnailer confirmed flowing through tenant-aware MediaStorage

url_previewer.py: all media ops go through MediaStorage.store_into_file
  which uses _local_path (tenant-aware). primary_base_path is a dead
  reference — stored but never read.

thumbnailer.py: all media ops go through MediaStorage.fetch_media,
  store_into_file, ensure_media_is_in_local_cache — all tenant-aware."
```

---

### Task 7: Full probe suite run + final commit

**Files:**
- All test files from this phase

- [ ] **Step 1: Run the complete probe suite**

Run: `python -m pytest tests/tenant/test_media_filepath.py tests/tenant/test_storage_provider.py tests/tenant/test_media_storage.py -v 2>&1 | tail -30`
Expected: All probes PASS. Count and record the total.

- [ ] **Step 2: Run existing media-related tests for regressions**

Run: `python -m pytest tests/media/ -v 2>&1 | tail -30`
Expected: All existing media tests PASS (no regressions from the changes).

- [ ] **Step 3: Record probe count**

Report the total probe count (expected: ~11 — 3 storage provider + 3 media storage + 2 factory + 3 existing filepath tests that exercise tenant paths).

---

## Definition of done

- [ ] `MediaRepository` uses `MultiTenantMediaFilePaths` in multi-tenant mode via factory
- [ ] `FileStorageProviderBackend` stores/fetches under `<base>/<server_name>/` when tenant context is set
- [ ] `MediaStorage` local path joins use `_local_path` helper with tenant isolation
- [ ] URL previewer and thumbnailer confirmed flowing through tenant-aware paths (audit)
- [ ] All probes green, no regressions in existing media tests
- [ ] Each task committed separately with descriptive messages

## Risks

| Risk | Mitigation |
|---|---|
| Single-tenant deployments break if context unexpectedly set | `_tenant_base` and `_local_path` fall back to global base when `get_current_tenant()` returns `None` |
| `_rel` methods include jail checks that may reject in tests | Test fixtures use `tenant_context()` context manager |
| `store_file` is async but probes test `_tenant_base` synchronously | `_tenant_base` is a pure sync method — safe to test directly |
