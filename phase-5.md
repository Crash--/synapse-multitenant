# Phase 5 kickoff prompt

Paste this into a fresh Claude Code session on the `feature/multi-tenant` branch.

---

## Prompt

You are continuing multi-tenant Synapse work on the `feature/multi-tenant` branch at `/home/monta/Documents/workspace/synapse-multitenant`.

**Read these first (narrow slices, not full files):**
1. `CLAUDE.md` — project rules, token discipline, subagent commit hygiene, test environment, where-to-look table
2. `docs/multi_tenant_roadmap.md` lines 403-409 — phase 5 scope
3. `roadmap-progess.md` — executive summary (phases 1-4 are ✅, phase 5 is ⏸)
4. `docs/superpowers/plans/2026-04-09-phase-4-per-tenant-rate-limiting-appservices.md` lines 1-15 — structural template for the plan doc

**Phase 4 is ✅ Complete.** 28-test probe suite. `TenantRatelimitConfig` + `TenantRatelimiterRegistry` (12 handler files), `TenantAppServiceRegistry` (store methods tenant-aware). Branch is clean except tracker files (uncommitted by design).

**Phase 5 — File storage providers.**

This is the phase that unblocks any deployment beyond a single host. Without it, k8s pods don't share disks, restarts lose local media, and there's no path to S3/object storage per tenant.

### Scope from the roadmap

Extend the storage-provider interface to take a `TenantConfig`; ship an S3-per-tenant implementation; route the thumbnailer / URL-preview cache through `multitenant_filepath`. Unblocks any deployment beyond a single host.

### What I know about the codebase shape

**Storage provider interface:**
- `synapse/media/storage_provider.py:41` — `StorageProvider` ABC with `store_file(path, file_info)` and `fetch(path, file_info)`. No tenant awareness.
- `synapse/media/storage_provider.py:70` — `StorageProviderWrapper` wraps a backend with `store_local`/`store_remote`/`store_synchronous` config. Filters URL-preview cache from offloading.
- `synapse/media/storage_provider.py:137` — `FileStorageProviderBackend` stores to a filesystem directory (`self.base_directory`). Single global directory.
- No S3 provider in-tree — the upstream S3 provider (`synapse-s3-storage-provider`) is a separate pip package.

**Media repository:**
- `synapse/media/media_repository.py:95` — `MediaRepository.__init__` reads `hs.config.media.media_store_path` as `self.primary_base_path`, creates a single `MediaFilePaths(self.primary_base_path)`, a single `FileStorageProviderBackend(hs, self.primary_base_path)`, and a single `MediaStorage(...)` with one global list of `storage_providers`.
- `synapse/media/media_repository.py:131-143` — storage providers loaded from `hs.config.media.media_storage_providers` (a list of `(class, config, wrapper_config)` tuples).

**Media storage:**
- `synapse/media/media_storage.py:163` — `MediaStorage.__init__` takes `filepaths`, `storage_providers`, `local_provider`. All are global singletons.
- `MediaStorage.store_file()`, `store_into_file()`, `fetch_media()` — all operate on the global provider list.

**Tenant-aware filepath (already done):**
- `synapse/media/multitenant_filepath.py` — `TenantMediaFilePaths` subclass of `MediaFilePaths`. Rewrites paths under `<base>/<tenant_server_name>/`. Uses `get_current_tenant()` from context var. This is already wired in.
- `synapse/media/media_repository.py:110` — `self.filepaths` is already a `MediaFilePaths` (but the *storage providers* don't know about tenants).

**Config:**
- `synapse/config/repository.py:188` — `media_storage_providers` is a global list read from `homeserver.yaml`. No per-tenant config.
- `TenantConfig.media_store_path` already exists — it's the per-tenant media directory for the local filesystem provider.

**URL preview + thumbnailer:**
- `synapse/media/url_previewer.py` — downloads URLs, stores preview images. Uses `self.media_repo.media_storage` (global).
- `synapse/media/thumbnailer.py` — generates thumbnails. Operates on file paths from `MediaFilePaths`.

### Task

Use the `/brainstorm` skill first, then the `/write-plan` skill to produce a plan doc at `docs/superpowers/plans/2026-04-09-phase-5-file-storage-providers.md`. Follow the phase-4 plan structure (context → decomposition → sub-phases → task lists → definition of done → risks).

Key design decisions to resolve in brainstorming:
1. **Tenant-aware StorageProvider interface.** The current `store_file(path, file_info)` and `fetch(path, file_info)` methods don't know which tenant the media belongs to. Options: (a) add `tenant: TenantConfig | None` param to the ABC, (b) rely on `get_current_tenant()` inside provider implementations (like phases 3-4 did), (c) embed tenant info in `FileInfo`. Option (b) is most consistent with the existing pattern and requires zero changes to the ABC signature.
2. **Per-tenant storage provider config.** Should `TenantConfig` carry a `storage_providers` list? Or is the global provider list sufficient if each provider implementation resolves its tenant-specific path/bucket internally? The global list + tenant-aware implementations is simpler and avoids duplicating the provider config per tenant.
3. **S3 tenant isolation model.** One shared S3 bucket, tenant `server_name` as a key prefix (e.g. `s3://synapse-media/acme.com/local_content/...`). This mirrors the local filesystem layout from `TenantMediaFilePaths`. No per-tenant bucket config needed — just the global S3 config + tenant-aware key prefixing via `get_current_tenant()`. The upstream S3 provider (`synapse-s3-storage-provider`) is a separate pip package; we write a new minimal tenant-aware S3 provider in-tree rather than vendoring.
4. **URL preview cache + thumbnails.** These use `MediaFilePaths` for path resolution, which is already tenant-aware via `TenantMediaFilePaths`. Are there remaining sites where the URL previewer or thumbnailer bypasses the tenant-aware filepath? Need to audit.
5. **Decomposition.** Natural axis: 5a = make existing local FS provider + MediaStorage tenant-aware, 5b = tenant-aware S3 provider (single bucket, server_name prefix), 5c = URL preview/thumbnail audit.

Constraints:
- Token discipline (see CLAUDE.md § "Token discipline")
- Subagent commit hygiene (see CLAUDE.md § "Subagent commit hygiene")
- Test environment (see CLAUDE.md § "Test environment")
- Probes-first: Task 0 of every sub-phase writes red probes
- Execute recommended choices autonomously (see memory)
- Run `/sync-roadmap` after the plan is approved and after each sub-phase ships
