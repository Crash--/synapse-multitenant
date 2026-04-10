# Phase 9 — Federation Outbound Design

**Date:** 2026-04-10
**Branch:** `feature/multi-tenant`
**Goal:** Enable outbound federation for all tenants, not just the default.
Currently all outbound federation traffic is signed with a single key and
stamped with a single `origin` (`hs.hostname`), so only the default tenant
can federate. Phase 9 makes the federation sender tenant-aware so every
tenant can talk to the public Matrix network independently.

**Companion docs:**
- `docs/multi_tenant_roadmap.md` lines 265-292 — federation gap description
- `docs/multi_tenant_roadmap.md` lines 453-456 — phase 9 scope
- `docs/multi_tenant.md` — overall design reference

---

## 1. Problem Statement

`FederationSender` maintains a single per-destination queue keyed only by
remote server name. With N tenants talking to the same remote server:

- **PDUs and EDUs get co-mingled** in the same `PerDestinationQueue`.
- **Transactions are signed** with `hs.signing_key` (the global key), not the
  tenant's key from `MultiTenantKeyring`.
- **Transaction `origin`** is always `hs.hostname`, not the tenant's
  `server_name`.
- **EDU `origin`** fields are stamped with `hs.hostname`.
- **Retry/back-off state** is queried with `our_server_name=hs.hostname`
  instead of the tenant's server name.
- **The event processing loop** and catchup loop run once globally with no
  tenant awareness.

Result: only the default tenant can federate. Other tenants' events are
either not sent, or sent with the wrong origin/signature — rejected by
remote servers.

## 2. Design Decisions

### 2.1 Tenant-keyed queues (not per-tenant FederationSender)

**Choice:** Single `FederationSender` with `_per_destination_queues` keyed by
`(tenant_server_name, destination)` tuple instead of just `destination`.

**Rejected alternatives:**
- *Per-tenant FederationSender:* N senders x M destinations = N*M queues.
  At 100 tenants this is a memory explosion. Workers would need N sender
  instances per worker process.
- *Resolve tenant from event origin at send time:* Lightest change but no
  tenant state on the queue, fragile plumbing, hard to shard in worker mode.

**Worker impact:** The `(tenant, destination)` tuple is the natural sharding
unit. A federation sender worker can handle any mix of tenants — the queue
key carries the tenant identity.

### 2.2 Explicit parameter passing for signing

**Choice:** Pass `origin` (tenant server name) and `signing_key` explicitly
through the call chain: `PerDestinationQueue` -> `TransactionManager` ->
`MatrixFederationHttpClient`.

**Rejected alternative:** Resolve from `get_current_tenant()` context var
inside `MatrixFederationHttpClient`. Rejected because the context var may
not be set in background process threads, and workers would need context
propagation through the replication layer.

**Security rationale:** Federation is a security boundary. Signing with the
wrong key is a catastrophic failure. Explicit parameters make the signing
identity auditable at every call site.

### 2.3 EDU origin stamping

Falls out of decision 2.1: each `PerDestinationQueue` stores its tenant's
`server_name` as `self.server_name`. The 4+ sites that stamp
`origin=self.server_name` onto EDUs work unchanged — they just use the
tenant's name instead of `hs.hostname`.

### 2.4 Event queue loop: per-event-batch tenant dispatch

**Choice:** Single global loop that groups events by origin server name and
sets tenant context per batch before dispatching to `(tenant, dest)` queues.

**Rejected alternative:** One loop per tenant (context set once at loop
start). Rejected because it creates N parallel loops and breaks if a worker
handles events from multiple tenants (destination-sharded workers).

**Worker impact:** The loop handles mixed-tenant event streams whether events
come from the local DB or from replication. No restructuring needed in
phase 12.

- `_last_poked_id` stays global (stream position is shared across tenants).
- `_is_processing` stays global (one loop, one flag).
- Catchup wakeup loop iterates all tenant schemas to find stale
  destinations, then wakes the appropriate `(tenant, dest)` queue.

### 2.5 `is_mine_id` / `is_mine_server_name`

Already multi-tenant-aware (`server.py:794-816`). No changes needed for the
"is this our event?" check. A new helper `_tenant_for_event(event)` extracts
the tenant server name from the event sender's server part to route events
to the correct `(tenant, dest)` queue.

## 3. Architecture

### 3.1 Data flow (outbound federation, multi-tenant)

```
Event persisted
    |
    v
FederationSender.notify_new_events(max_token)
    |
    v
_process_event_queue_loop()
    |  pulls events from stream
    |  groups by origin server_name
    |
    v  for each (tenant_server_name, events) batch:
    |    set tenant context + search_path
    |    for each event:
    |      resolve destinations (rooms the event is in)
    |      dispatch to PerDestinationQueue[(tenant, dest)]
    |
    v
PerDestinationQueue(tenant_server_name, destination)
    |  self.server_name = tenant_server_name
    |  EDU origin = self.server_name (tenant)
    |  retry state queried with our_server_name = tenant_server_name
    |
    v
TransactionManager.send_new_transaction(tenant_server_name, signing_key, ...)
    |  Transaction(origin=tenant_server_name, ...)
    |
    v
MatrixFederationHttpClient._send_request(...)
    |  sign_json(request, origin, signing_key)  <-- explicit params
    |  Authorization: X-Matrix origin=<tenant_server_name>,...
    |
    v
Remote server receives transaction signed by correct tenant key
```

### 3.2 Components modified

| Component | File | Change |
|---|---|---|
| `FederationSender` | `synapse/federation/sender/__init__.py` | Queue dict keyed by `(tenant, dest)`. `_get_per_destination_queue` takes tenant param. `_process_event_queue_loop` groups events by tenant. `_tenant_for_event` helper. Catchup loop iterates tenant schemas. |
| `PerDestinationQueue` | `synapse/federation/sender/per_destination_queue.py` | Constructor takes `tenant_server_name`. `self.server_name` set to tenant. All EDU origin sites already use `self.server_name`. `get_retry_limiter` uses tenant server_name. |
| `TransactionManager` | `synapse/federation/sender/transaction_manager.py` | `send_new_transaction` accepts `origin` + `signing_key` params. `Transaction(origin=origin)`. Passes signing params to transport layer. |
| `MatrixFederationHttpClient` | `synapse/http/matrixfederationclient.py` | `_send_request` accepts optional `origin` + `signing_key` override params. Falls back to `self.server_name` / `self.signing_key` for non-federation-sender callers. `sign_json` uses overrides. |
| `_TransactionQueueManager` | `synapse/federation/sender/per_destination_queue.py` | Presence EDU at line 783 already uses `self.queue.server_name` — works via 2.3. |

### 3.3 Components NOT modified

- `MultiTenantKeyring` — already has `get_signing_key(server_name)`. No changes.
- `destinations` DB table — already per-tenant-schema. No schema changes.
- `is_mine_id` / `is_mine_server_name` — already multi-tenant-aware.
- Federation allow-lists — remain global. Per-tenant allow-lists are phase 10.
- Federation inbound — phase 10.

## 4. Sub-phases

### 4a — Queue keying + PerDestinationQueue tenant identity

**Scope:**
- Change `_per_destination_queues` dict key from `str` to `tuple[str, str]`
  (tenant_server_name, destination).
- `_get_per_destination_queue(tenant_server_name, destination)` takes both params.
- `PerDestinationQueue.__init__` takes `tenant_server_name` param, stores as
  `self.server_name` (replacing `hs.hostname`).
- `TransactionManager.send_new_transaction` accepts `origin` param, uses it
  in `Transaction(origin=origin)`.
- Add `_tenant_for_event(event) -> str | None` helper.
- Update all callers of `_get_per_destination_queue` to pass tenant.

**Tests (red probes first):**
- Queue created with tenant server_name, `self.server_name` matches tenant.
- Two tenants sending to same destination get separate queues.
- `_tenant_for_event` returns correct tenant for tenant user, `None` for
  unknown origin.
- Transaction built with tenant origin, not `hs.hostname`.

### 4b — Tenant-aware signing in MatrixFederationHttpClient

**Scope:**
- `_send_request` gains optional `origin: str | None` and
  `signing_key: SigningKey | None` params.
- When both are provided, `sign_json` uses them instead of
  `self.server_name` / `self.signing_key`.
- Authorization header uses the provided `origin`.
- `TransactionManager` passes `origin` + `signing_key` (looked up via
  `MultiTenantKeyring.get_signing_key(origin)`) to the HTTP client.
- The `PerDestinationQueue` resolves the signing key from its
  `self.server_name` (tenant) and passes it to `TransactionManager`.

**Tests (red probes first):**
- Transaction to remote server is signed with tenant's key, not global key.
- Authorization header contains tenant's server_name as origin.
- Fallback: non-federation-sender callers (e.g., key fetches) still use
  global `hs.signing_key`.

### 4c — Event queue loop tenant dispatch + catchup

**Scope:**
- `_process_event_queue_loop`: after pulling events, group by
  `_tenant_for_event`. For each tenant batch, set tenant context
  (`set_current_tenant`) and `search_path`, then dispatch.
- `_wake_destinations_needing_catchup`: iterate all active tenants, set
  context per tenant, query `destinations` table for stale entries, wake
  `(tenant, dest)` queues.
- `notify_new_events` unchanged (it just pokes `_last_poked_id` and starts
  the loop).

**Tests (red probes first):**
- Events from tenant A dispatched to `(acme, matrix.org)` queue, not
  `(corp, matrix.org)`.
- Events from tenant B dispatched to `(corp, matrix.org)` queue.
- Mixed-tenant event batch correctly separated.
- Catchup loop wakes correct `(tenant, dest)` queues.

### 4d — EDU origin stamping verification + integration

**Scope:**
- Verify all EDU origin sites use `self.server_name` (should already work
  from 4a). Specifically check:
  - `_get_receipt_edus` (line 657)
  - `_get_device_update_edus` (line 683)
  - `_get_to_device_message_edus` (line 710)
  - Presence EDU in `_TransactionQueueManager.__aenter__` (line 783)
- Integration test: two tenants send events to the same remote — verify
  separate transactions with correct origins and signatures.
- `get_retry_limiter` calls use tenant server_name (from `self.server_name`).

**Tests (red probes first):**
- EDUs from tenant A carry `origin=acme.localhost`.
- EDUs from tenant B carry `origin=corp.localhost`.
- Retry limiter called with tenant server_name, not `hs.hostname`.
- Integration: full send path from event → queue → transaction → signed
  request, verified per tenant.

## 5. Definition of Done

- [ ] All outbound federation traffic (PDUs, EDUs) uses the correct tenant
      `origin` and is signed with the correct tenant key.
- [ ] Two tenants sending to the same remote server get independent queues,
      independent retry/back-off state, and independent transactions.
- [ ] No regression: single-tenant deployments (no `multi_tenant` config)
      work exactly as before — all fallbacks use `hs.hostname` / `hs.signing_key`.
- [ ] All red probes from 4a-4d turn green.
- [ ] No cross-tenant data leakage in the federation sender path.

## 6. Risks and Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Signing with wrong tenant key | Remote server rejects transaction; potential impersonation | Explicit parameter passing (decision 2.2); probe verifies signature per tenant |
| Memory growth from (tenant, dest) queues | At 100 tenants x 1000 destinations = 100k queues | Queues are lazy (created on first send); idle tenants create no queues. Monitor gauge already exists. |
| Event loop performance with tenant grouping | Extra grouping step per iteration | Grouping is O(n) on event count per batch (max 100); negligible vs. DB I/O |
| Catchup loop iterating all tenant schemas | Slow at high tenant count | One query per tenant; can be parallelized later. Phase 8 connection caching minimizes `SET search_path` overhead. |
| Breaking non-federation-sender callers of MatrixFederationHttpClient | Key fetches, well-known lookups use wrong signing | Optional params with fallback to global `hs.signing_key` / `hs.hostname` |
| Worker compatibility (phase 12) | Design choices that block future worker sharding | All choices validated for worker compatibility: tuple queue keys shard naturally, explicit signing params don't need context propagation, single loop handles mixed-tenant streams |

## 7. Out of Scope

- Federation inbound routing (phase 10)
- Per-tenant federation allow-lists/blocklists (phase 10)
- Federation workers (phase 12)
- Server ACLs and event auth `hs.hostname` reads (phase 10/12)
- `.well-known/matrix/server` per-tenant (nginx concern)
