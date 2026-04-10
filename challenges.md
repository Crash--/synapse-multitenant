# Multi-Tenant Synapse — Bottleneck Analysis & Scaling Challenges

**Date:** 2026-04-10
**Branch:** `feature/multi-tenant`
**Reference:** [Element blog — Scaling to millions of users requires Synapse Pro](https://element.io/blog/scaling-to-millions-of-users-requires-synapse-pro)

---

## Context

This analysis combines Element's published scaling data for community Synapse (Python) and Synapse Pro (Rust) with the actual code paths in our multi-tenant fork. The goal is to identify capacity limits, prioritize fixes, and set realistic expectations for tenant counts.

### Element's published numbers

| Metric | Community Synapse (Python) | Synapse Pro (Rust) |
|--------|---------------------------|-------------------|
| CPU overhead (idle worker, 40 evt/s server) | ~25% of 1 core | ~3% of 1 core |
| CPU at 120 evt/s | ~75% (service crashes) | Negligible |
| Max CPU cores per worker | 1 (GIL) | All available |
| Memory vs. Python baseline | 1x | ~0.2x (5x smaller) |
| Scalability improvement | — | ~500x |
| Multi-tenancy target | Not designed for it | "Thousands of tiny instances" |

Key takeaway: community Synapse hits a hard ceiling at ~50K simultaneous users (40 evt/s) per worker due to GIL. Our fork runs N tenants inside that same single-process budget.

---

## Critical bottlenecks (will hit first)

### 1. Shared connection pool

**Location:** `homeserver.yaml` (`cp_min: 5`, `cp_max: 10`), `synapse/storage/database.py`

One connection pool serves ALL tenants. Each tenant transaction performs:

```
SHOW search_path
SET search_path TO tenant_xxx, public
  ... actual queries ...
SET search_path TO <original>
```

That's **3 extra SQL round trips per transaction**, all competing for 10 connections. At 4 tenants with moderate load, the pool is already near saturation. At 20+ tenants, pool exhaustion causes visible latency and request queuing.

**Capacity estimate:** ~10-15 tenants before pool starvation.

**Fixes:**
- Raise `cp_max` to 50-100 (Postgres handles 300+ connections; add pgbouncer for higher counts)
- Per-tenant connection sub-pools with pinned `search_path` (eliminates SET per txn)
- pgbouncer with `search_path` set at session assign time

### 2. search_path SET per transaction

**Location:** `synapse/storage/database.py:1241-1263`

Every single database operation pays a 2-3 query tax. On a busy tenant doing 100 requests/second, that's 200-300 extra Postgres round trips/second — pure overhead. This is the largest performance gap vs. a dedicated Synapse process (which never switches schemas).

**Fixes (in order of effort):**
1. **`SET LOCAL search_path`** — scopes to the transaction, auto-resets on commit. Eliminates the explicit SHOW + restore. ~10 lines changed.
2. **Connection-level caching** — track which schema a connection is currently set to; skip SET if unchanged. Most requests within a burst hit the same tenant. ~50 lines.
3. **Connection-pinning** — keep connections bound to a tenant's schema for the request duration. Requires per-tenant sub-pool or connection tagging.

### 3. Python GIL (inherited from upstream)

**Location:** Inherent to CPython runtime

Element's data: one Python worker = one CPU core max. Background process fan-out (`synapse/tenant_background.py:127-137`) spawns N coroutines for N tenants. Each of the 7+ background loops runs once per tenant. At 50 tenants: 350+ concurrent coroutines fighting for one GIL.

**Capacity estimate:** Background process overhead dominates at ~30-50 tenants.

**Fix:** This is the one bottleneck we cannot fix without Rust/workers. Phase 11 (workers) is the answer. Our architecture is well-positioned for it — schema isolation, key management, and context propagation are already solved.

---

## High-severity bottlenecks (will hit at scale)

### 4. Schema cloning time

**Location:** `docker-demo/control-plane/src/services/provisioning.ts:35-92`

Provisioning clones ~120+ tables with `CREATE TABLE ... (LIKE ... INCLUDING ALL)`, plus sequences and 9 singleton seed rows. Each is a DDL operation that takes locks. Total provisioning time: 5-15 seconds depending on Postgres load. During this time, `public` schema is under shared locks.

**Fixes:**
- Pre-create a "template schema" and clone from it (pg_dump/psql from frozen template)
- Postgres 16+ `CREATE SCHEMA ... CLONE OF` (if available)
- Async provisioning with progress tracking (already partially implemented via step reporting)

### 5. Rate limiter memory growth

**Location:** `synapse/api/ratelimiting.py:97-104`

`TenantRatelimiterRegistry` creates per-tenant `Ratelimiter` instances. Each holds unbounded `actions: dict[user_id -> token_bucket]` pruned every 15 seconds. At 50 tenants x 1000 active users x 17 limiter types = ~850K entries.

**Fix:** LRU eviction instead of timer-based pruning. Or shared rate limiter with tenant-prefixed keys.

### 6. Non-atomic registry reload

**Location:** `synapse/tenant_registry.py:93-145`

Reload mutates `self._tenants` dict in-place without locking. A request reading the dict mid-reload could see partial state. This is a correctness issue — at scale with frequent provisioning, it becomes a race condition.

**Fix:** Copy-on-write: build a new dict, then atomic swap `self._tenants = new_dict`. ~20 lines.

---

## Capacity estimates

| Scenario | Tenants | Limiting factor |
|----------|---------|----------------|
| **Current (untuned, cp_max=10)** | **~10-15** | Connection pool starvation |
| **Pool tuned (cp_max=100)** | **~30-50** | GIL + search_path overhead |
| **+ search_path caching** | **~50-100** | GIL + rate limiter memory |
| **+ pgbouncer + SET LOCAL** | **~100-200** | GIL (hard ceiling) |
| **+ Rust workers (phase 11)** | **~1000+** | Postgres connection limits / sharding |

---

## Optimization priority

| # | Fix | Effort | Impact |
|---|-----|--------|--------|
| 1 | Raise `cp_max` to 50-100, add pgbouncer | Config change | 10 -> 50 tenants |
| 2 | `SET LOCAL search_path` (drop explicit restore) | 1 file, ~10 lines | 30% fewer DB round trips |
| 3 | Connection schema caching (skip SET if unchanged) | ~50 lines in `database.py` | Major reduction for bursty traffic |
| 4 | Template schema for cloning | Provisioning refactor | Faster provisioning, fewer locks |
| 5 | Atomic registry swap (copy-on-write) | ~20 lines | Correctness at scale |
| 6 | Rate limiter LRU eviction | ~30 lines | Memory stability |
| 7 | Rust workers (phase 11) | Large | 100 -> 1000+ tenants |

---

## Architecture advantages

Despite the bottlenecks, the multi-tenant architecture has fundamental advantages over running N separate Synapse processes:

- **Memory:** 1 process vs. N x ~150MB. At 100 tenants, that's ~15GB saved.
- **Connection pool:** 1 shared pool vs. N pools (each needing cp_min connections). At 100 tenants with cp_min=5, upstream needs 500 idle connections; we need 50.
- **Operational:** 1 config (or DB table), 1 deployment, 1 monitoring target. No N systemd units, N nginx upstreams, N signing key files on disk.
- **Provisioning:** API call vs. write YAML + generate key + create DB + run migrations + configure proxy + start process.
- **Ready for Rust:** When Synapse Pro / Rust workers land, our tenant isolation (schema routing, key management, context propagation) transfers directly. The 500x CPU improvement applies to all tenants at once.

---

## Future considerations

- **Federation (phases 8-9):** Per-tenant `FederationSender` will multiply outbound connection count. Need connection pooling per remote server per tenant — or batching across tenants to the same remote.
- **Workers (phase 11):** Replication streams need tenant awareness. Worker assignment (which worker handles which tenant) is a new scheduling problem.
- **Postgres sharding:** At 500+ tenants, a single Postgres instance may not suffice. Horizontal sharding by tenant (each shard holds N tenant schemas) is the natural split.
- **S3 media (phase 5 follow-up):** Eliminates per-tenant disk I/O contention. Single bucket with `server_name` key prefix.
