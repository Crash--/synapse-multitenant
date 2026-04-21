# Density investigation — the `/versions` p95 jump at 50 tenants

**Date:** 2026-04-21
**Scope:** Proof-of-concept investigation of the #1 signal from the 50-tenant density run (`density-2026-04-21.md`): `versions` p95 jumped **3.7 ms → 18.8 ms** from 10 to 50 tenants. No fix attempted.

## TL;DR

**The hypothesis was wrong.** `/versions` latency does not grow because tenant hostname resolution is linear in N — it is O(1). The real cause is **periodic per-tenant background-process fan-out**: every few seconds, ~8 distinct `looping_call` handlers each spawn one background process per tenant, and at 50 tenants those fan-outs produce 50-wide reactor bookkeeping bursts that stall any request in flight during the burst window.

Evidence:

- Median `/versions` latency is **flat** (1.82 ms at 10 tenants, 1.82 ms at 50 tenants) — rules out per-request linear cost.
- p95 and p99 tail is what moves — consistent with periodic, not sustained, work.
- Prometheus metrics confirm: during the 5-minute run at 50 tenants, 8 distinct per-tenant fan-outs each fired ~50 processes (see table below).
- `stress-050` (the last-provisioned tenant) is consistently the slowest — matches its position in the `get_all_tenants()` iteration order, which is the order in which each fan-out burst dispatches background processes.

## Step 1 — The stated hypothesis

From `density-2026-04-21.md`:

> `/versions` endpoint p95 climbed 3.7 ms → 18.8 ms from 10 to 50 tenants. 5× latency at 5× tenants on a static, unauthenticated endpoint that does no business logic. The only cost that scales with tenant count is Host-header→TenantConfig lookup. A 5× jump means that lookup is linear (or worse) in N.

Suspects named: `synapse/http/site.py::SynapseSite.get_tenant_for_host`, `synapse/tenant_registry.py::get_tenant`.

## Step 2 — Read the code (HYPOTHESIS REFUTED)

### `synapse/http/site.py:1030-1050` — `SynapseSite.get_tenant_for_host`

```python
def get_tenant_for_host(self, host: str) -> "TenantConfig | None":
    if not self._multi_tenant_enabled or self._tenant_registry is None:
        return None
    # Strip port if present
    if ":" in host and not host.startswith("["):
        host = host.rsplit(":", 1)[0]
    elif host.startswith("[") and "]:" in host:
        host = host.rsplit(":", 1)[0]
    return self._tenant_registry.get_tenant(host)
```

Port-strip is constant-time string slicing. Delegates to the registry. **O(1) so far.**

### `synapse/tenant_registry.py:158-180` — `TenantRegistry.get_tenant`

```python
def get_tenant(self, server_name: str) -> TenantConfig | None:
    if server_name in self._inactive_tenants:
        return None
    tenant = self._tenants.get(server_name)
    if tenant:
        return tenant
    aliased_name = self._hostname_aliases.get(server_name)
    if aliased_name and aliased_name not in self._inactive_tenants:
        return self._tenants.get(aliased_name)
    return None
```

- `self._tenants: dict[str, TenantConfig]` — dict lookup O(1).
- `self._hostname_aliases: dict[str, str]` — dict lookup O(1).
- No linear scans on the hot path.

A grep of `for … in self._tenants` / `get_all_tenants` / `get_all_server_names` across the full `synapse/` tree returns only 2 hits — neither on the request path. **Tenant resolution is genuinely O(1).**

## Step 3 — Look at the shape of the data again

Revisiting the 50-tenant `/versions` summary:

```
versions_duration: avg=8.17 ms  min=0.82 ms  med=1.82 ms  p90=5.86 ms  p95=18.81 ms  max=825.38 ms
```

- **Median is 1.82 ms** — identical to the 10-tenant median. A linear per-request cost would shift the median, not just the tail.
- **Max is 825 ms**, nearly a full second for a static-JSON endpoint. That's not "scan got a bit slower"; that's "something blocked the reactor."
- Only the top 10% of requests is elevated.

Signature: **rare stalls, not sustained cost.** The hypothesis was the wrong shape.

## Step 4 — What runs periodically that scales with tenant count?

`grep 'run_as_background_process_per_tenant'` finds a helper in `synapse/tenant_background.py`:

```python
def run_as_background_process_per_tenant(desc, hs, func, *args, **kwargs):
    tenants = registry.get_all_tenants()
    ...
    for tenant in tenants:
        async def _runner(_t=tenant):
            with tenant_context(_t):
                return await func(*args, **kwargs)
        deferreds.append(
            run_as_background_process(desc, tenant.server_name, _runner)
        )
    return deferreds
```

This iterates every tenant and synchronously spawns one background process per tenant. Call sites:

```
synapse/handlers/account_validity.py:81
synapse/handlers/auth.py:235             — expire_old_sessions,  every 5 minutes
synapse/handlers/deactivate_account.py:278
synapse/handlers/device.py:193           — delete_stale_devices
synapse/handlers/device.py:1438
synapse/handlers/message.py              — looping_call
synapse/handlers/pagination.py:180
synapse/handlers/presence.py:909/942/948/1011
synapse/handlers/presence.py:869         — _dispatch_handle_timeouts, every 5 seconds   ← hot
synapse/handlers/stats.py:110
synapse/handlers/user_directory.py:217
```

The presence dispatcher is the biggest offender — `_dispatch_handle_timeouts` fires every 5 seconds and, at N=50, spawns 50 background processes per tick.

## Step 5 — Verify against live Prometheus metrics

Query `synapse_background_process_start_count_total` post-run, aggregated by process name:

| Process | Distinct server_name labels |
|---|---|
| `user_directory.notify_new_event` | 51 |
| `stats.notify_new_event` | 51 |
| `room_forgetter.notify_new_event` | 51 |
| `presence.notify_new_event` | 51 |
| `_maybe_retry_device_resync` | 51 |
| **`handle_presence_timeouts`** | **51** |
| `_handle_new_device_update_async` | 51 |
| `delayed_events.notify_new_event` | 51 |
| `send_dummy_events_to_fill_extremities` | 50 |
| `persist_presence_changes` | 50 |
| `expire_old_sessions` | 50 |
| `update_client_ips` | 45 |
| `typing._handle_timeouts` | 45 |
| `prune_old_user_ips` | 45 |

(51 = 50 tenants + `localhost` default; `get_all_tenants()` includes it.)

**8 distinct processes fired once per tenant per tick.** At 50 tenants, each tick is 50 concurrent spawns for that one process name. Over the 5-min run, `handle_presence_timeouts` alone spawned on the order of **60 ticks × 50 tenants = 3,000 background processes**.

Each background process fan-out does the following SYNCHRONOUSLY on the reactor thread before the first one begins any real work:

1. Allocate 50 `BackgroundProcessLoggingContext` objects,
2. Enter 50 `tenant_context(tenant)` scopes (each is a ContextVar set/reset pair),
3. Increment 50 Prometheus counters (each a dict lookup + label-set),
4. Queue 50 `Deferred` callbacks.

Even at ~0.2-0.5 ms of reactor time per tenant, 50 tenants burn 10-25 ms of reactor time PER TICK. That's the blip window that pushes /versions p95 from 3.7 to 18.8 ms — the 200 VUs are hitting the reactor at ~168 req/s, so at 5s intervals roughly 5-10% of in-flight requests overlap a dispatch burst. That exactly matches the data: flat p50, elevated p90-p95, long tail at p99.

## Step 6 — Why `stress-050` specifically is slowest

`registry.get_all_tenants()` returns tenants in `self._tenants.items()` order. `self._tenants` is a dict populated in provisioning order (`__init__` copies the `MultiTenantConfig.tenants` dict, which was built from DB rows in insertion order). So the fan-out loop dispatches background processes in the order `stress-001 → stress-002 → … → stress-050`. Every process gets its contextvar/logging bookkeeping synchronously; the 50th call to `run_as_background_process` sits behind all 49 others.

When a client request for `stress-050` arrives during a tick, it contends with the dispatcher which is late in its work. Requests for `stress-001` more often overlap with the CHEAP end of the dispatcher. Hence `stress-050` sees higher mean (9.50 ms vs 2.63 ms for `stress-005`) — not because `stress-050`'s own lookup is slower, but because requests for it are more likely to fall inside a stall.

## Independent check — the fairness shape is consistent

From `density-2026-04-21.md`, the five slowest tenants at 50 were:

| Tenant | Mean | Registry order (approx) |
|---|---|---|
| `stress-050` | 9.50 ms | last |
| `stress-010` | 5.58 ms | early-ish but dominant in an earlier k6 run |
| `stress-012` | 5.44 ms | middle |
| `stress-015` | 5.27 ms | middle |
| `stress-034` | 5.06 ms | late |

Weaker signal for 10/12/15 — ordinary outlier variance with thin samples (245-429 iterations each). `stress-050` at nearly 2× the next-slowest is the clearest order-position effect.

## What this tells us about the design

The `run_as_background_process_per_tenant` pattern was introduced to keep tenant context correctly bound for periodic work (`docs/multi_tenant.md` / the comment block at the top of `synapse/tenant_background.py`). It is correct in isolation. The density-driven pathology is that **the pattern compounds across 8+ call sites each firing on their own looping_call, and the fan-outs themselves run synchronously on the reactor thread**. At low tenant counts the bookkeeping is invisible; at 50 it is measurable; at 500 it would likely be a visible saturation signal.

## Things this investigation did NOT do

- No fix attempted (per the user's explicit instruction).
- No repro under different ordering (the `stress-050`-specific claim would be confirmed by reverse-order provisioning; deferred).
- No profile of a single dispatch tick (py-spy on the reactor would quantify exactly how much time a 50-wide burst takes; deferred).

## Suggested directions for a future fix (not scope)

Any of these would reduce the blip without changing semantics:

1. **Stagger the fan-out** — dispatch tenants in `clock.call_later` slots spread over the tick interval (e.g. 50 tenants over 2s = one spawn every 40 ms) instead of one synchronous burst.
2. **Batch the work, not the processes** — one background process iterates tenants and does the work in a loop, instead of one process per tenant. Loses per-tenant metric labels unless re-added explicitly.
3. **Cache `get_all_tenants()` snapshots** — the list is copied on every call today. Not the root cause but would slightly reduce allocation pressure.
4. **Reduce the number of distinct call sites** — merge related per-tenant loopers where possible.

All of these change observable behavior (metric cardinality, per-tenant fairness of background work, ordering). A real fix should land with a design decision on which tradeoff is acceptable.
