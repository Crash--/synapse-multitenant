# Phase 4 — Per-Tenant Rate Limiting + App Services Design Spec

**Date:** 2026-04-09
**Branch:** `feature/multi-tenant`
**Roadmap ref:** `docs/multi_tenant_roadmap.md` line 399-402
**Predecessor:** Phase 3 (SSO / email / push) — ✅ Complete

---

## Goal

Noisy-neighbor protection: each tenant gets its own rate-limit buckets with independently tunable settings. Per-tenant app service registration so bridges and bots are scoped to a single tenant.

## Decomposition

Two independent sub-phases:

- **4a — Per-tenant rate limiting**
- **4b — Per-tenant app services**

They share no code paths and can be implemented/tested independently.

---

## Sub-phase 4a — Per-tenant rate limiting

### Problem

All ~15 `Ratelimiter` instances are created once at startup from global `hs.config.ratelimiting.*` (`synapse/config/ratelimiting.py:83`). Keys are `user_id` or `ip_address` — users on different tenants share the same buckets and the same `per_second`/`burst_count` settings. A noisy tenant exhausts limits that punish quiet tenants.

### Solution: per-tenant `Ratelimiter` instances via lazy resolution

#### 1. `TenantRatelimitConfig` dataclass

Added to `synapse/config/tenants.py`. Frozen attrs class holding the client-facing `RatelimitSettings` fields that mirror `RatelimitConfig`:

| Field | Global equivalent | Default |
|---|---|---|
| `rc_message` | `RatelimitConfig.rc_message` | `0.2/s, burst 10` |
| `rc_registration` | `RatelimitConfig.rc_registration` | `0.17/s, burst 3` |
| `rc_registration_token_validity` | same | `0.1/s, burst 5` |
| `rc_login_address` | `RatelimitConfig.rc_login_address` | `0.003/s, burst 5` |
| `rc_login_account` | `RatelimitConfig.rc_login_account` | `0.003/s, burst 5` |
| `rc_joins_local` | `RatelimitConfig.rc_joins_local` | `0.1/s, burst 10` |
| `rc_joins_remote` | `RatelimitConfig.rc_joins_remote` | `0.01/s, burst 10` |
| `rc_joins_per_room` | same | `1/s, burst 10` |
| `rc_invites_per_room` | same | `0.3/s, burst 10` |
| `rc_invites_per_user` | same | `0.003/s, burst 5` |
| `rc_invites_per_issuer` | same | `0.3/s, burst 10` |
| `rc_3pid_validation` | same | `0.003/s, burst 5` |
| `rc_media_create` | same | `10/s, burst 50` |

Each field is `RatelimitSettings | None`. `None` = inherit from the global `RatelimitConfig` for that specific limiter. Parsed via `TenantRatelimitConfig.from_dict(d)` which calls `RatelimitSettings.parse()` per key present in the dict.

Field on `TenantConfig`:
```python
ratelimit: TenantRatelimitConfig | None = None
```

#### 2. `TenantRatelimiterRegistry`

New class in `synapse/api/tenant_ratelimiting.py`:

```python
class TenantRatelimiterRegistry:
    """Caches per-tenant Ratelimiter instances, creating them lazily."""

    def __init__(self, store, clock, global_config: RatelimitConfig):
        self._store = store
        self._clock = clock
        self._global = global_config
        # (server_name, limiter_key) -> Ratelimiter
        self._cache: dict[tuple[str, str], Ratelimiter] = {}

    def get(self, limiter_key: str, tenant: TenantConfig | None = None) -> Ratelimiter:
        """Return the Ratelimiter for the given limiter key and tenant.
        Falls back to global config when tenant is None or tenant.ratelimit is None."""
```

Registered on `HomeServer` as a singleton (`hs.get_tenant_ratelimiter_registry()`).

#### 3. Handler conversion pattern

Each handler currently does:
```python
# At __init__:
self._limiter = Ratelimiter(store, clock, cfg=hs.config.ratelimiting.rc_joins_local)

# At request time:
await self._limiter.ratelimit(requester)
```

Converts to:
```python
# At __init__:
self._tenant_rl_registry = hs.get_tenant_ratelimiter_registry()

# At request time:
tenant = get_current_tenant()  # may be None in non-tenant context
limiter = self._tenant_rl_registry.get("rc_joins_local", tenant)
await limiter.ratelimit(requester)
```

#### 4. Files to convert (~13 sites)

| File | Limiters |
|---|---|
| `synapse/handlers/room_member.py:134-179` | `rc_joins_local`, `rc_joins_remote`, `rc_joins_per_room`, `rc_invites_per_room`, `rc_invites_per_user` |
| `synapse/rest/client/login.py:123-131` | `rc_login_address`, `rc_login_account` |
| `synapse/rest/client/register.py:404` | `rc_registration_token_validity` |
| `synapse/rest/client/sync.py:135` | presence limiter |
| `synapse/rest/client/presence.py:55` | presence limiter |
| `synapse/handlers/identity.py:75-80` | `rc_3pid_validation` (ip + address) |
| `synapse/handlers/devicemessage.py:82` | device message limiter |
| `synapse/rest/client/user_directory.py:50` | user directory limiter |
| `synapse/rest/media/create_resource.py:51` | `rc_media_create` |
| `synapse/media/media_repository.py:124` | download limiter |
| `synapse/rest/client/login_token_request.py:81` | login token limiter |
| `synapse/api/ratelimiting.py:392` (`RequestRatelimiter`) | `rc_message`, `rc_admin_redaction` |

#### 5. Key isolation

Each tenant's `Ratelimiter` is a separate instance with its own `self.actions` dict. User IDs like `@alice:acme.com` and `@alice:corp.com` naturally land in different instances — no key-prefixing needed.

---

## Sub-phase 4b — Per-tenant app services

### Problem

`ApplicationServiceWorkerStore.__init__` (`synapse/storage/databases/main/appservice.py:82`) calls `load_appservices(hs.hostname, config_files)` once at startup into `self.services_cache`. All tenants share the global AS list. A bridge registered for acme.com receives events for corp.com users.

### Solution: tenant-scoped AS registry

#### 1. Config field

```python
# On TenantConfig:
app_service_config_files: list[str] | None = None
```

`None` means no app services for this tenant. Unlike other config fields, there is no "inherit global" semantic — AS namespaces are bound to a specific `server_name`, so inheriting makes no sense.

#### 2. `TenantAppServiceRegistry`

New class or method set that holds:
```python
# server_name -> list[ApplicationService]
_tenant_services: dict[str, list[ApplicationService]]
# server_name -> compiled exclusive regex
_tenant_exclusive_regex: dict[str, re.Pattern | None]
```

Loaded at startup by iterating all tenants and calling `load_appservices(tenant.server_name, tenant.app_service_config_files or [])` for each.

#### 3. Store method conversion

These methods in `ApplicationServiceWorkerStore` become tenant-aware:

| Method | Change |
|---|---|
| `get_app_services()` | Check `get_current_tenant()`, return tenant's AS list |
| `get_app_service_by_user_id(user_id)` | Look up from tenant's AS list |
| `get_if_app_services_interested_in_user(user_id)` | Check tenant's exclusive regex |
| `get_app_service_by_token(token)` | Search tenant's AS list |

When no tenant context is set (admin API, background recovery), fall back to the union of all tenant AS lists or an empty list depending on the call site.

#### 4. Event dispatch

The critical path is `synapse/handlers/appservice.py` → `_notify_interested_services` which calls `self.store.get_app_services()`. Since this runs in request context (or event-persistence context where tenant is set), the tenant-aware `get_app_services()` returns only that tenant's services.

#### 5. Scheduler

`ApplicationServiceScheduler.enqueue_for_appservice` receives a specific `ApplicationService` object — it doesn't need to know which tenant it belongs to. The scheduler, transaction controller, and recoverer operate on individual AS objects and are already correctly scoped.

The one exception: `ApplicationServiceScheduler.start()` queries `get_appservices_by_state(DOWN)` which returns AS objects from the DB transaction state table. These are AS IDs, not tenant-scoped. The recoverer should work without changes since it pushes transactions to a specific AS endpoint — it doesn't need tenant context for the outbound HTTP call.

#### 6. Namespace isolation

`load_appservices()` already validates uniqueness of user/room/alias namespaces within a single call. Since each tenant gets its own `load_appservices(tenant.server_name, ...)` invocation, namespaces are naturally isolated per-tenant. Two tenants can register overlapping regex patterns without conflict.

---

## Config YAML shape

```yaml
multi_tenant:
  tenants:
    - server_name: acme.com
      database_schema: tenant_acme
      signing_key_path: /keys/acme.signing.key
      media_store_path: /media/acme
      ratelimit:
        rc_message:
          per_second: 0.5
          burst_count: 20
        rc_login:
          address:
            per_second: 0.01
            burst_count: 10
          account:
            per_second: 0.01
            burst_count: 10
        # Omitted keys (rc_joins, rc_invites, etc.) inherit global defaults
      app_service_config_files:
        - /etc/synapse/appservices/acme-irc-bridge.yaml
        - /etc/synapse/appservices/acme-slack-bridge.yaml

    - server_name: corp.com
      database_schema: tenant_corp
      signing_key_path: /keys/corp.signing.key
      media_store_path: /media/corp
      # ratelimit: omitted → inherits all global rc_* settings
      # app_service_config_files: omitted → no app services
```

---

## Testing strategy

### Probes-first (task 0 of each sub-phase)

**4a probes** (`tests/tenant/test_tenant_ratelimit_config.py`):
- Parse `TenantRatelimitConfig` from dict with partial keys, verify inheritance from global
- Two tenants with different `rc_message` settings get different `Ratelimiter` instances from registry
- A tenant with `ratelimit: None` gets the global limiter settings

**4b probes** (`tests/tenant/test_tenant_appservice.py`):
- Tenant A has AS config files, tenant B has none → `get_app_services()` returns A's list in A's context, empty in B's context
- AS user lookup is tenant-scoped

### Regression

- `tests/api/test_ratelimiting.py` — existing tests pass (no tenant context = global behavior)
- `tests/handlers/test_appservice.py` — existing tests pass

---

## Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Memory from per-tenant Ratelimiter instances (15 × N) | Low | Each instance is ~200 bytes + a dict + WheelTimer. 100 tenants = 1500 instances = negligible. |
| `looping_call` proliferation (15 prune timers × N tenants) | Medium | Option A: accept it for small N. Option B: share a single prune loop that iterates all tenant instances. Start with A, optimize later if N > 50. |
| AS transaction state table not schema-scoped | Low | Transactions keyed by AS `id` which is unique per-tenant after scoped loading. No cross-tenant leakage possible. |
| Background AS recovery needs tenant context | Low | Recoverer pushes HTTP to a specific AS URL — doesn't need tenant context for the outbound call. If DB reads are needed, set context from AS → tenant mapping. |
| Handler conversion touches many files | Medium | Mechanical change with clear pattern. Each conversion is small and independently testable. |

---

## Definition of done

- [ ] `TenantRatelimitConfig` dataclass on `TenantConfig`, parsed from YAML with per-field inheritance
- [ ] `TenantRatelimiterRegistry` creates/caches per-tenant `Ratelimiter` instances
- [ ] All ~13 handler/rest files converted to resolve limiters from tenant context
- [ ] `TenantConfig.app_service_config_files` field added and parsed
- [ ] Tenant-scoped AS registry loads per-tenant at startup
- [ ] `get_app_services()` and related lookup methods are tenant-aware
- [ ] Red→green probe suites for both 4a and 4b
- [ ] Existing rate-limiting and appservice tests still pass
- [ ] Docker demo smoke test with per-tenant rate limit config
