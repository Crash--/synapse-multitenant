# Phase 5 — File Storage Providers Design Spec

**Date:** 2026-04-09
**Branch:** `feature/multi-tenant`
**Phase:** 5 of `docs/multi_tenant_roadmap.md`

---

## Problem

`FileStorageProviderBackend` stores and fetches media from a single global `base_directory`. In multi-tenant mode, all tenants share the same filesystem namespace — tenant A's media is visible to tenant B's provider paths. Additionally, `MediaRepository.__init__` constructs a plain `MediaFilePaths` (line 110), so the existing `MultiTenantMediaFilePaths` class and its `create_media_file_paths()` factory are dead code.

The `MediaStorage` class also joins `self.local_media_directory + path` at 4 sites without tenant-prefix awareness, meaning even with tenant-aware filepaths, the local write/read paths would land in the wrong directory.

This phase makes the local filesystem storage provider tenant-aware and audits the URL previewer and thumbnailer to confirm they flow through the now-tenant-aware `MediaStorage` pipeline.

## Scope

**In scope:**
- Wire `MultiTenantMediaFilePaths` into `MediaRepository`
- Make `FileStorageProviderBackend` resolve tenant prefix in `store_file`/`fetch`
- Make `MediaStorage` local path joins tenant-aware
- Audit URL previewer and thumbnailer for tenant-bypassing paths
- Probes for tenant isolation of stored/fetched media

**Out of scope (deferred):**
- S3 storage provider (see Future S3 Model below)
- Per-tenant storage provider config lists
- Hot-add/remove of storage providers

## Approach

**Context-var resolution inside providers** (Approach A from brainstorming). The `FileStorageProviderBackend` calls `get_current_tenant()` at invocation time and inserts `tenant.server_name` into the path. No changes to the `StorageProvider` ABC signature. Consistent with phases 2-4 pattern.

Alternatives considered and rejected:
- **Per-tenant `MediaStorage` instances** — significant refactor, breaks single-`MediaStorage` assumption, doesn't match context-var pattern
- **Tenant field on `FileInfo`** — muddles the DTO's purpose, still populated from context var, just adds indirection

## Components

### 1. `MediaRepository.__init__` — wire tenant-aware filepaths

**File:** `synapse/media/media_repository.py:110`

Replace:
```python
self.filepaths: MediaFilePaths = MediaFilePaths(self.primary_base_path)
```

With:
```python
from synapse.media.multitenant_filepath import create_media_file_paths
self.filepaths: MediaFilePaths = create_media_file_paths(
    self.primary_base_path,
    multi_tenant_enabled=hs.config.multi_tenant.enabled,
)
```

This activates the `MultiTenantMediaFilePaths` subclass, which overrides all `_rel` and absolute path methods with tenant-prefixed versions.

### 2. `FileStorageProviderBackend` — tenant-aware store/fetch

**File:** `synapse/media/storage_provider.py:137-197`

In `store_file` and `fetch`, resolve the tenant and insert `server_name` between the base directory and the relative path:

```python
from synapse.tenant_context import get_current_tenant

def _tenant_base(self, base: str) -> str:
    tenant = get_current_tenant()
    if tenant is not None:
        return os.path.join(base, tenant.server_name)
    return base
```

- `store_file`: `primary_fname = os.path.join(self._tenant_base(self.cache_directory), path)` and `backup_fname = os.path.join(self._tenant_base(self.base_directory), path)`
- `fetch`: `backup_fname = os.path.join(self._tenant_base(self.base_directory), path)`

No ABC signature change. No impact on third-party providers.

### 3. `MediaStorage` — tenant-aware local path joins

**File:** `synapse/media/media_storage.py`

Four sites join `self.local_media_directory + path`:
- Line 232 (`store_into_file`): write path for new uploads
- Line 294 (`fetch_media`): URL cache local lookup
- Line 358 (`ensure_media_is_in_local_cache`): local cache check
- Line 374 (`ensure_media_is_in_local_cache`): legacy thumbnail fallback

Add a helper method:
```python
def _local_path(self, rel_path: str) -> str:
    """Join local_media_directory with rel_path, inserting tenant prefix if active."""
    tenant = get_current_tenant()
    base = self.local_media_directory
    if tenant is not None:
        base = os.path.join(base, tenant.server_name)
    return os.path.join(base, rel_path)
```

Replace all 4 `os.path.join(self.local_media_directory, path)` calls with `self._local_path(path)`.

### 4. URL previewer — audit

**File:** `synapse/media/url_previewer.py`

The URL previewer stores preview images via `self.media_storage.store_into_file(file_info)` (line 604). This flows through `_file_info_to_path` → `filepaths.url_cache_filepath_rel` → the `_rel` method. The `_rel` methods on `MultiTenantMediaFilePaths` include jail checks that validate against the tenant base path. The local path join in `store_into_file` goes through the now-tenant-aware `_local_path` helper.

`self.primary_base_path` (line 183) is used only for constructing `MediaFilePaths` directly — verify this is only used for path existence checks, not for storage.

**Expected result:** No changes needed. Audit confirms tenant isolation flows through.

### 5. Thumbnailer — audit

**File:** `synapse/media/thumbnailer.py`

The thumbnailer uses `self.media_storage.fetch_media(file_info)` and `self.media_storage.store_into_file(file_info)`. Both flow through the same tenant-aware pipeline as above.

**Expected result:** No changes needed. Audit confirms tenant isolation flows through.

## Future S3 Model (deferred, not phase 5)

The future S3 provider will follow the identical pattern:
- **Single shared bucket** — one set of credentials, one bucket name in `homeserver.yaml`
- **Tenant `server_name` as key prefix** — e.g. `s3://synapse-media/acme.com/local_content/aa/bb/media_id`
- **Resolution via `get_current_tenant()`** — the provider calls it inside `store_file`/`fetch` to construct the S3 key, exactly like `FileStorageProviderBackend._tenant_base()`
- **No per-tenant bucket config** — tenant isolation is by key prefix, not by bucket. Mirrors the local filesystem layout from `TenantMediaFilePaths`
- **`boto3` as optional dependency** — not required for local-only deployments
- **Minimal in-tree implementation** — not vendoring `synapse-s3-storage-provider`

## Testing

### Probes (written first, red until implementation)

1. **Store isolation:** Set tenant context to `acme.com`, store a file via `FileStorageProviderBackend.store_file`, assert the file lands at `<base>/acme.com/local_content/...`
2. **Fetch isolation:** Store as `acme.com`, set context to `corp.com`, call `fetch` — returns `None`
3. **No-tenant fallback:** With no tenant context, store/fetch works at `<base>/local_content/...` (single-tenant compatibility)
4. **`MediaStorage._local_path` isolation:** Verify the helper returns tenant-prefixed paths when context is set
5. **URL preview path isolation:** Store a URL preview image with tenant context, verify path includes tenant prefix
6. **Thumbnail path isolation:** Verify thumbnail fetch/store paths include tenant prefix
7. **`create_media_file_paths` factory:** Verify it returns `MultiTenantMediaFilePaths` when multi-tenant is enabled

### Existing coverage

`tests/tenant/test_media_filepath.py` covers `MultiTenantMediaFilePaths` path generation and jail checks. Extend if gaps found during audit.

## Definition of done

- `MediaRepository` uses `MultiTenantMediaFilePaths` in multi-tenant mode via the factory
- `FileStorageProviderBackend` stores/fetches under `<base>/<tenant_server_name>/` when tenant context is set
- `MediaStorage` local path joins are tenant-aware via `_local_path` helper
- URL previewer and thumbnailer confirmed flowing through tenant-aware paths (audit, no code changes expected)
- All probes green
- Design doc notes future S3 model (single bucket, `server_name` prefix)

## Risks

| Risk | Mitigation |
|---|---|
| Existing single-tenant deployments break if tenant context is unexpectedly set | `_tenant_base` and `_local_path` fall back to global base when `get_current_tenant()` returns `None` |
| `_rel` methods on `MultiTenantMediaFilePaths` include jail checks that may reject paths in tests | Test fixtures must set tenant context before calling path methods |
| `url_previewer.py` uses `self.primary_base_path` directly for non-storage purposes | Audit will identify any direct path construction bypasses |
