# Phase 10 — Federation Inbound Design

**Date:** 2026-04-10
**Branch:** `feature/multi-tenant`
**Goal:** Enable inbound federation for all tenants, not just the default.
Currently all inbound federation traffic is authenticated against a single
`destination` (`hs.hostname`) and the key server returns only the global key,
so only the default tenant can receive federation traffic. Phase 10 makes the
inbound federation path tenant-aware so every tenant can receive events,
EDUs, and key queries from the public Matrix network independently.

**Prerequisite:** Phase 9 (federation outbound) is complete — outbound queues
are keyed by `(tenant_server_name, destination)`, per-tenant signing works,
and EDU origin stamping is verified.

---

## Context

### The problem

When a remote Matrix server sends a transaction to `acme.localhost`, the
request arrives at Synapse's shared federation endpoint via SRV resolution.
Three things go wrong:

1. **`Authenticator.authenticate_request`** (`_base.py:86`) sets
   `json_request["destination"] = self.server_name` — the global hostname.
   The remote signed the request with `destination=acme.localhost`, so
   signature verification fails.

2. **`LocalKey.on_GET`** (`local_key_resource.py:109`) returns a cached
   response containing the global `server_name` and global signing key.
   Remote servers querying `/_matrix/key/v2/server` for `acme.localhost`
   get the wrong key.

3. **`FederationServer`** constructs EDUs, signs events, and builds
   responses using `self.server_name` (the global hostname) — so even if
   authentication were bypassed, downstream processing would stamp
   everything with the wrong origin.

### B2B deployment model

This fork serves a B2B platform where each tenant is a client's chat
instance (e.g., `acme.com`, `corp.com`). The design preserves full tenant
isolation:

- Each client is a distinct `server_name` on the Matrix network
- Per-tenant federation allow-lists let operators control federation scope
  per client (open, closed, partner-only)
- Per-tenant signing keys provide cryptographic isolation
- Destination-based routing (not Host header) is the security boundary

---

## Sub-phase Decomposition

| Sub-phase | Scope | Key files |
|---|---|---|
| **10a** | Federation tenant resolution | `synapse/federation/transport/server/_base.py` |
| **10b** | Per-tenant key server | `synapse/rest/key/v2/local_key_resource.py` |
| **10c** | FederationServer tenant-awareness | `synapse/federation/federation_server.py` |
| **10d** | Per-tenant federation allow-lists | `synapse/config/tenants.py`, `synapse/config/federation.py`, `_base.py` |
| **10e** | EventAuthHandler tenant-awareness | `synapse/handlers/event_auth.py` |

Each sub-phase follows the probes-first pattern: task 0 writes red probes,
task 1+ implements until probes go green.

---

## 10a — Federation Tenant Resolution

### Problem

`Authenticator.authenticate_request` (`_base.py:79`) hardcodes
`json_request["destination"] = self.server_name` at line 86. The `destination`
field from the X-Matrix Authorization header is parsed (line 106) but only
used for a validation check — it's never written into `json_request` or
used to set tenant context.

### Design

Modify `authenticate_request` to resolve the effective destination from a
three-tier chain:

1. **X-Matrix `destination`** (primary) — extracted from `_parse_auth_header`
   return value. This is what the remote server signed against.
2. **`get_current_tenant().server_name`** — if `_setup_tenant_context`
   already resolved a tenant from Host/X-Matrix-Server-Name headers.
3. **`self.server_name`** — global default for backwards compatibility
   with single-tenant deployments and older remote servers that don't send
   `destination`.

After resolving the effective destination:

- Set `json_request["destination"]` to the resolved value (fixes signature
  verification).
- Look up the tenant via `tenant_registry.get_tenant(destination)`.
- If found, call `set_current_tenant(tenant)` — this overrides any
  Host-based resolution. The Authenticator is authoritative for federation
  paths.
- If `destination` is present but doesn't match any tenant, raise
  `AuthenticationError` — we don't serve that `server_name`.

### Existing code preserved

- `_is_mine_server_name(destination)` check at line 111 already validates
  against all tenants — keep it as-is.
- `_parse_auth_header` return signature unchanged.
- `_setup_tenant_context` in `site.py:521` still runs as best-effort
  pre-resolver. No changes needed.

### Changes

| File | Change |
|---|---|
| `_base.py` Authenticator | New `_resolve_federation_destination()` method. Called after auth header loop. Sets `json_request["destination"]` and tenant context. |
| `_base.py` Authenticator.__init__ | Accept `tenant_registry` parameter for tenant lookup. |

---

## 10b — Per-Tenant Key Server

### Problem

`LocalKey` (`local_key_resource.py:40`) caches a single response at startup
containing the global `server_name` and global signing key. All tenants
return the same key response.

### Design

Make `on_GET` tenant-aware with dynamic per-tenant responses.

- `on_GET`: Check `get_current_tenant()`. If a tenant is set, build the
  response using that tenant's `server_name` and signing keys from
  `MultiTenantKeyring`.
- If no tenant context, fall back to the existing global response
  (backwards compat for single-tenant mode).
- Replace the single cached `self.response_body` with a dict keyed by
  `server_name`. Each entry uses the same expiry-based refresh logic
  (half-interval check).
- Sign the response JSON with the tenant's signing key.

### Changes

| File | Change |
|---|---|
| `local_key_resource.py` LocalKey.__init__ | Accept `MultiTenantKeyring` (optional). Init `_tenant_responses: dict[str, tuple[int, JsonDict]]` cache (`server_name → (valid_until_ts, response_body)`). |
| `local_key_resource.py` LocalKey.on_GET | Tenant-aware dispatch: tenant cache hit → return, miss → `_build_tenant_response()`. |
| `local_key_resource.py` | New `_build_tenant_response(tenant_config, keyring, time_now)` method. |

---

## 10c — FederationServer Tenant-Awareness

### Problem

`FederationServer` (`federation_server.py:136`) uses `self.server_name`
(`hs.hostname`) in EDU construction (line 568), event signing (line 1004),
and transaction building (line 1141).

### Design

Add two properties to `FederationServer`:

```python
@property
def _effective_server_name(self) -> str:
    tenant = get_current_tenant()
    return tenant.server_name if tenant else self.server_name

@property
def _effective_signing_key(self) -> SigningKey:
    tenant = get_current_tenant()
    if tenant:
        return self._keyring.get_signing_key(tenant.server_name)
    return self.hs.signing_key
```

### Sites to patch

| Location | Current | After |
|---|---|---|
| `_handle_edus_in_txn` line 568 | `destination=self.server_name` | `destination=self._effective_server_name` |
| `_handle_edus_in_txn` line 564 | Prometheus label `self.server_name` | `self._effective_server_name` |
| `on_send_join_request` line 1004 | `self.hs.hostname` + `self.hs.signing_key` | `self._effective_server_name` + `self._effective_signing_key` |
| `_build_get_missing_events_response` line 1141 | `origin=self.server_name` | `origin=self._effective_server_name` |

---

## 10d — Per-Tenant Federation Allow-Lists

### Problem

`federation_domain_whitelist` (`config/federation.py:35`) is global. All
tenants share one allow-list.

### Design

New `TenantFederationConfig` dataclass (follows `TenantEmailConfig`,
`TenantRatelimitConfig` pattern from phases 3-4):

```python
@dataclass
class TenantFederationConfig:
    federation_domain_whitelist: dict[str, bool] | None = None
```

Resolution order:
1. Tenant-specific whitelist (if configured and not None)
2. Global `federation_domain_whitelist` (existing behavior)
3. No filtering (both are None)

### Changes

| File | Change |
|---|---|
| `synapse/config/tenants.py` | Add `TenantFederationConfig` dataclass. Add `federation` field to `TenantConfig`. |
| `synapse/config/federation.py` | `is_domain_allowed_according_to_federation_whitelist` gains optional `tenant` parameter. Checks tenant whitelist first. |
| `_base.py` Authenticator | After resolving tenant, check tenant whitelist before global whitelist. |
| `synapse/federation/sender` | Outbound path also checks tenant whitelist (symmetry). |

---

## 10e — EventAuthHandler Tenant-Awareness

### Problem

`EventAuthHandler._server_name` (`event_auth.py:58`) is `hs.hostname`.
`is_host_joined(room_id, self._server_name)` at line 277 checks if the
global hostname is in the room, not the current tenant.

### Design

Add `_effective_server_name` property (same pattern as 10c):

```python
@property
def _effective_server_name(self) -> str:
    tenant = get_current_tenant()
    return tenant.server_name if tenant else self._server_name
```

Replace `self._server_name` with `self._effective_server_name` at line 277.

---

## Out of Scope

| Topic | Reason |
|---|---|
| **Well-known per-tenant** | Roadmap says this is an nginx/reverse-proxy concern. The reverse proxy already routes `.well-known` paths by Host header. |
| **Federation workers** | Worker-based federation reader/sender needs full context propagation — separate phase (roadmap item). |
| **Hot-add tenant federation** | Hot-add (phase 6) triggers `MultiTenantKeyring.reload()`. The per-tenant key cache (10b) and tenant registry lookup (10a) already handle newly added tenants after reload. No extra work needed. |

---

## Risks and Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Older remote servers omit `destination` from X-Matrix header | Tenant resolution falls back to Host/global — single-tenant behavior preserved | Three-tier resolution chain with global fallback |
| Per-tenant key cache memory growth | One cached response per tenant (~2KB each) | Negligible for expected tenant counts (<1000). Entries expire naturally. |
| `_effective_server_name` called outside request context | Returns global default (no tenant set) | Same behavior as pre-phase-10 code — no regression |
| Tenant whitelist misconfiguration blocks federation | Client can't receive events | Operator tooling (`synapse_tenant validate`) should warn on empty whitelists. Fallback to global means omitting tenant config = inheriting global behavior. |

---

## Definition of Done

- [ ] Inbound federation transactions for non-default tenants pass signature
      verification (destination matches what remote signed)
- [ ] `/_matrix/key/v2/server` returns correct keys per tenant
- [ ] EDUs received for a tenant are constructed with correct destination
- [ ] Event signatures use tenant's server_name and signing key
- [ ] Per-tenant federation allow-lists filter inbound and outbound traffic
- [ ] `EventAuthHandler.is_host_joined` checks correct tenant server_name
- [ ] All existing federation tests remain green
- [ ] Minimum 15 new probes covering the 5 sub-phases
