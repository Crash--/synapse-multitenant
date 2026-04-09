# Phase 6 — Hot Add/Remove + Backup/Restore Design

**Date:** 2026-04-09
**Branch:** `feature/multi-tenant`
**Roadmap ref:** `docs/multi_tenant_roadmap.md` item 6
**Depends on:** Phases 1-5 (all complete)

---

## Problem

Adding a new tenant today requires restarting the entire Synapse process, causing downtime for all tenants. There is also no way to back up or restore a single tenant's data without a full database dump. This phase closes both gaps.

## Decomposition

Two independent sub-phases:

- **6a — Hot reload (runtime):** SIGHUP triggers re-read of tenant config; registries update in-place.
- **6b — Backup/restore CLI (offline tooling):** `synapse_tenant backup` / `restore` subcommands.

---

## Sub-phase 6a — Hot Reload

### Trigger

SIGHUP, using the existing infrastructure in `synapse/app/_base.py`. The `register_sighup(hs, func)` mechanism runs callbacks on the next reactor tick via `callFromThread`. Already used for log config and TLS certificate reload.

### Reload cascade

```
SIGHUP
  -> TenantRegistry.reload(hs)
       -> re-read homeserver.yaml tenant block
       -> diff old vs new tenants
       -> for added: insert into self._tenants, log
       -> for removed: move to self._inactive_tenants, log
       -> for unchanged: no-op
       -> MultiTenantKeyring.reload(registry)
       -> TenantAppServiceRegistry.reload(registry)
       -> TenantRatelimiterRegistry.reload(registry)
  -> log summary: "SIGHUP: added N, removed M, unchanged K"
```

### Design decisions

**Mutable registries with `reload()` methods** (not cache invalidation or instance replacement). The `@cache_in_self` decorator caches the return value of `get_*` on the `HomeServer` instance with no built-in invalidation. Making registries mutable means the cached reference stays valid and internal state updates in-place. Downstream registries (`MultiTenantKeyring`, `TenantAppServiceRegistry`, `TenantRatelimiterRegistry`) also hold references to the `TenantRegistry` — replacing the instance would require updating all those references.

**Config re-parsing:** A new method `hs.config.reload_tenant_config()` re-reads just the `multi_tenant` block from the YAML file on disk and returns a fresh `MultiTenantConfig`. The existing `synapse/config/tenants.py` parser is reused — no new parser.

**Schema creation on hot-add:** Not automatic. The operator runs `synapse_tenant init-schema` before adding the tenant to config and sending SIGHUP. This keeps the reload path simple and avoids async DB operations in a signal handler context.

**Tenant removal safety:** Removing a tenant from the config and sending SIGHUP marks it as *inactive*. Requests for inactive tenants get a 404. The schema and media tree are preserved. Actual cleanup is a separate destructive command (`synapse_tenant drop`).

### Changes to `TenantRegistry`

```python
class TenantRegistry:
    def __init__(self, multi_tenant_config):
        self._config = multi_tenant_config
        self._tenants = dict(multi_tenant_config.tenants)  # mutable copy
        self._inactive_tenants: set[str] = set()
        self._signing_keys: dict[str, list] = {}
        self._hostname_aliases: dict[str, str] = {}
        # ... existing init ...

    def reload(self, hs: "HomeServer") -> dict:
        """Re-read tenant config from disk, diff, and update in-place.

        Returns a dict: {"added": [...], "removed": [...], "unchanged": [...]}
        """
        new_config = hs.config.reload_tenant_config()
        new_names = set(new_config.tenants.keys())
        old_names = set(self._tenants.keys()) - self._inactive_tenants

        added = new_names - old_names
        removed = old_names - new_names
        unchanged = old_names & new_names

        for name in added:
            self._tenants[name] = new_config.tenants[name]
            self._inactive_tenants.discard(name)  # re-activate if previously removed

        for name in removed:
            self._inactive_tenants.add(name)

        self._config = new_config
        return {"added": list(added), "removed": list(removed), "unchanged": list(unchanged)}

    def get_tenant(self, server_name: str) -> TenantConfig | None:
        if server_name in self._inactive_tenants:
            return None
        # ... existing lookup ...

    def is_inactive(self, server_name: str) -> bool:
        return server_name in self._inactive_tenants
```

### Changes to `MultiTenantKeyring`

```python
class MultiTenantKeyring:
    def reload(self, registry: "TenantRegistry") -> None:
        """Reload signing keys based on current registry state."""
        self._registry = registry
        current_names = set(t.server_name for t in registry.get_all_tenants())

        # Load keys for new tenants
        for tenant in registry.get_all_tenants():
            if tenant.server_name not in self._signing_keys:
                self._load_tenant_keys(tenant)

        # Remove keys for inactive tenants
        for name in list(self._signing_keys.keys()):
            if name not in current_names or registry.is_inactive(name):
                del self._signing_keys[name]
                self._verify_keys.pop(name, None)
```

### Changes to `TenantAppServiceRegistry`

```python
class TenantAppServiceRegistry:
    def reload(self, registry: "TenantRegistry") -> None:
        """Reload app service configs for current tenants."""
        current_names = set(t.server_name for t in registry.get_all_tenants())

        # Add new tenants
        for tenant in registry.get_all_tenants():
            if tenant.server_name not in self._tenant_services:
                files = tenant.app_service_config_files or []
                services = load_appservices(tenant.server_name, files) if files else []
                self._tenant_services[tenant.server_name] = services
                self._tenant_exclusive_regex[tenant.server_name] = _make_exclusive_regex(services)
                self._all_services.extend(services)

        # Mark removed tenants (remove from lookup, keep data intact)
        for name in list(self._tenant_services.keys()):
            if name not in current_names or registry.is_inactive(name):
                removed_services = self._tenant_services.pop(name)
                self._tenant_exclusive_regex.pop(name, None)
                for svc in removed_services:
                    if svc in self._all_services:
                        self._all_services.remove(svc)
```

### Changes to `TenantRatelimiterRegistry`

```python
class TenantRatelimiterRegistry:
    def reload(self, registry: "TenantRegistry") -> None:
        """Clear cached limiters for removed tenants. New tenants are lazy-created."""
        current_names = set(t.server_name for t in registry.get_all_tenants())
        for key in list(self._limiters.keys()):
            server_name, _ = key
            if server_name not in current_names or registry.is_inactive(server_name):
                del self._limiters[key]
```

### SIGHUP registration

In `synapse/app/_base.py`, inside the `start()` function, after the existing TLS/log reload hooks:

```python
def _reload_tenants(hs: "HomeServer") -> None:
    registry = hs.get_tenant_registry()
    if not registry.enabled:
        return
    result = registry.reload(hs)
    keyring = hs.get_multi_tenant_keyring()
    if keyring:
        keyring.reload(registry)
    hs.get_tenant_app_service_registry().reload(registry)
    hs.get_tenant_ratelimiter_registry().reload(registry)
    logger.info(
        "SIGHUP tenant reload: added=%s removed=%s unchanged=%d",
        result["added"], result["removed"], len(result["unchanged"]),
    )

register_sighup(hs, _reload_tenants, hs)
```

### Config re-read method

Added to `synapse/config/tenants.py` as a module-level function (mirrors the existing `MultiTenantConfig.from_dict` pattern):

```python
def reload_tenant_config(self) -> MultiTenantConfig:
    """Re-read the multi_tenant block from homeserver.yaml on disk."""
    config_path = self.config_path  # stored at startup
    with open(config_path) as f:
        raw = yaml.safe_load(f)
    mt_block = raw.get("multi_tenant", {})
    return MultiTenantConfig.from_dict(mt_block)
```

Error handling: if YAML is malformed or the tenant block fails validation, the method raises and the SIGHUP callback logs the error and aborts without modifying any state.

### Thread safety

SIGHUP callbacks run on the reactor thread via `callFromThread`. All registry mutations happen synchronously on the reactor thread. No concurrent mutation risk.

---

## Sub-phase 6b — Backup/Restore CLI

### Subcommands

Added to `scripts/synapse_tenant`:

#### `synapse_tenant backup`

```
synapse_tenant backup -c homeserver.yaml --server-name acme.com --output /backups/acme.tar.gz
```

Steps:
1. Load config, find tenant by `--server-name`
2. Run `pg_dump --schema=<tenant_schema> --no-owner --no-acl` -> `schema.sql`
3. Tar the tenant media tree at `<media_store_path>/<server_name>/` -> `media/` directory in archive
4. Write `manifest.json`:
   ```json
   {
     "server_name": "acme.com",
     "database_schema": "tenant_acme_com",
     "timestamp": "2026-04-09T14:30:00Z",
     "synapse_version": "1.x.y",
     "media_files_count": 42
   }
   ```
5. Package `manifest.json` + `schema.sql` + `media/` into a single `.tar.gz`

Dependencies: `pg_dump` must be in PATH. Checked at command start with a clear error message if missing.

#### `synapse_tenant restore`

```
synapse_tenant restore -c homeserver.yaml --server-name acme.com --from /backups/acme.tar.gz [--overwrite]
```

Steps:
1. Extract archive to temp directory
2. Validate `manifest.json` (check `server_name` matches `--server-name`)
3. If target schema exists and `--overwrite` not set: abort with error
4. If `--overwrite`: drop existing schema
5. Create schema: `CREATE SCHEMA <tenant_schema>`
6. Run `psql --set ON_ERROR_STOP=1` with `schema.sql` to restore tables/data
7. Extract `media/` to `<media_store_path>/<server_name>/`
8. Log row counts for key tables as a sanity check

#### `synapse_tenant drop`

```
synapse_tenant drop -c homeserver.yaml --server-name acme.com --confirm-destructive
```

Steps:
1. Require `--confirm-destructive` flag (no accidental drops)
2. Drop the PostgreSQL schema: `DROP SCHEMA <tenant_schema> CASCADE`
3. Delete the media tree: `rm -rf <media_store_path>/<server_name>/`
4. Log what was deleted

This command does NOT modify `homeserver.yaml` — the operator removes the tenant from config separately.

### Archive format

```
acme-2026-04-09.tar.gz
  manifest.json
  schema.sql
  media/
    local_content/
    local_thumbnails/
    remote_content/
    remote_thumbnails/
    url_cache/
    url_cache_thumbnails/
```

---

## Testing strategy

### 6a — Hot reload probes

| Probe | What it tests |
|---|---|
| `test_reload_adds_new_tenant` | Registry gains a new tenant after reload |
| `test_reload_removes_tenant` | Removed tenant becomes inactive, `get_tenant()` returns None |
| `test_reload_unchanged_tenants` | Existing tenants unaffected by reload |
| `test_reload_reactivates_tenant` | Re-adding a previously removed tenant reactivates it |
| `test_reload_keyring_loads_new_keys` | Keyring loads signing key for newly added tenant |
| `test_reload_keyring_removes_inactive_keys` | Keyring drops keys for inactive tenants |
| `test_reload_appservice_registry` | AS registry picks up new tenant's app services |
| `test_reload_ratelimiter_clears_removed` | Rate limiter cache cleared for removed tenant |
| `test_reload_malformed_config_aborts` | Bad YAML doesn't corrupt running state |
| `test_sighup_triggers_reload` | End-to-end: SIGHUP -> registry updated |

### 6b — Backup/restore probes

| Probe | What it tests |
|---|---|
| `test_backup_creates_valid_archive` | Archive contains manifest.json, schema.sql, media/ |
| `test_backup_manifest_contents` | Manifest has correct server_name, schema, timestamp |
| `test_restore_round_trip` | Backup -> restore -> verify schema and media exist |
| `test_restore_refuses_without_overwrite` | Restore to existing schema fails without --overwrite |
| `test_restore_with_overwrite` | --overwrite drops and recreates schema |
| `test_drop_requires_confirm_flag` | Drop without --confirm-destructive fails |
| `test_drop_removes_schema_and_media` | Drop cleans up both schema and media tree |
| `test_backup_missing_pg_dump_errors` | Clear error when pg_dump not in PATH |

---

## Risks

1. **Malformed config during SIGHUP.** If the YAML is invalid, the reload must abort without affecting running tenants. Mitigation: parse into a new `MultiTenantConfig` first; only mutate registries if parsing succeeds. The SIGHUP callback wraps everything in a try/except and logs the error.

2. **`pg_dump`/`psql` availability.** Backup/restore depends on PostgreSQL client tools. Mitigation: check at command start with `shutil.which("pg_dump")`, clear error message.

3. **Large media trees.** Tarring a multi-GB media directory could be slow. Mitigation: this is documented as an offline operation. No streaming or incremental backup in v1.

4. **Race between SIGHUP and in-flight requests.** A request that started before the reload could be using a tenant that gets removed mid-flight. Mitigation: the reload only marks tenants as inactive — it doesn't delete any data. In-flight requests that already have a `TenantConfig` bound via `tenant_context` will complete normally. Only new requests will see the 404.

---

## Files touched

### 6a — Hot reload

| File | Change |
|---|---|
| `synapse/tenant_registry.py` | Add `reload()`, `_inactive_tenants`, `is_inactive()` |
| `synapse/crypto/multitenant_keyring.py` | Add `reload()` |
| `synapse/appservice/tenant_registry.py` | Add `reload()` |
| `synapse/api/tenant_ratelimiting.py` | Add `reload()` |
| `synapse/config/tenants.py` | Add `reload_tenant_config()` method |
| `synapse/app/_base.py` | Register `_reload_tenants` SIGHUP callback in `start()` |
| `tests/tenant/test_registry_reload.py` | New: reload probes |
| `tests/tenant/test_keyring_reload.py` | New: keyring reload probes |
| `tests/tenant/test_appservice_reload.py` | New: AS registry reload probes |
| `tests/tenant/test_ratelimiter_reload.py` | New: rate limiter reload probes |

### 6b — Backup/restore

| File | Change |
|---|---|
| `scripts/synapse_tenant` | Add `backup`, `restore`, `drop` subcommands |
| `tests/tenant/test_tenant_backup.py` | New: backup/restore probes |
