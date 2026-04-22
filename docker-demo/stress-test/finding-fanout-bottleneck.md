# Finding — per-tenant fan-out bottleneck

**Date:** 2026-04-21 → 2026-04-22
**Branch:** `feature/multi-tenant`
**Scope:** Consolidated writeup of the per-tenant background-process fan-out bottleneck — every measurement, every source reference, every command that produced a number. Nothing extrapolated is presented as measured.

Companion files:
- `docker-demo/stress-test/result-2026-04-21.md` — original post-fix 4-tenant stress run
- `docker-demo/stress-test/density-2026-04-21.md` — 10 + 50 tenant density runs
- `docker-demo/stress-test/finding-density-versions-p95.md` — the investigation that first named fan-out as the mechanism

## TL;DR (measured)

At 50 tenants on a 200-VU k6 profile, `http_req_duration` p95 is **17.6 ms** (versions-endpoint p95 **18.81 ms**). At 100 tenants on the same profile with the per-tenant fan-out disabled by an env flag, p95 is **5.25 ms** (versions p95 **3.14 ms**). No other code change between the runs. The only architectural pressure that moves with tenant count and matches the latency shape is the fan-out pattern in `synapse/tenant_background.py::run_as_background_process_per_tenant`.

## The code path (verified from source, 2026-04-22)

### The helper

`synapse/tenant_background.py::run_as_background_process_per_tenant` (lines 61-137). On every invocation:

```python
tenants = registry.get_all_tenants()           # list copy, O(N)
...
for tenant in tenants:                         # synchronous loop on reactor
    async def _runner(_t=tenant):
        with tenant_context(_t):
            return await func(*args, **kwargs)
    deferreds.append(
        run_as_background_process(desc, tenant.server_name, _runner)
    )
return deferreds
```

Each `run_as_background_process` call before the actual coroutine begins:
1. Builds a `BackgroundProcessLoggingContext` (LogContext alloc + `__enter__`),
2. Registers / increments the `synapse_background_process_start_count_total{name=…, server_name=…}` counter,
3. Schedules the coroutine on the reactor via `defer.ensureDeferred`.

This runs on the reactor thread. Verified by reading the function body end-to-end; no `callInThread`, no queue, no batching.

### The call sites

`grep -rn "run_as_background_process_per_tenant" synapse/ --include='*.py'` (excluding the helper's own file) returns 14 unique callsites (2026-04-22):

| File:line | Caller | Mechanism |
|---|---|---|
| `synapse/handlers/account_validity.py:81` | expiry sweep | `clock.looping_call` |
| `synapse/handlers/auth.py:235` | `expire_old_sessions` | `clock.looping_call(..., Duration(minutes=5))` |
| `synapse/handlers/deactivate_account.py:278` | deactivation worker | event-driven |
| `synapse/handlers/device.py:193` | `delete_stale_devices` | `clock.looping_call` |
| `synapse/handlers/device.py:1438` | `_handle_new_device_update_async` | `clock.looping_call` |
| `synapse/handlers/message.py` | dummy-event fill-in | `clock.looping_call` |
| `synapse/handlers/pagination.py:180` | purge-history | `clock.looping_call` |
| `synapse/handlers/presence.py:869` → `_dispatch_handle_timeouts` | presence timeouts | `clock.looping_call(..., Duration(seconds=5))` |
| `synapse/handlers/presence.py:942` (no-op branch) | timeouts-no-op | same loop, emits metric |
| `synapse/handlers/presence.py:948` | timeouts-real | same loop, dispatches work |
| `synapse/handlers/presence.py:1011` | persist_presence_changes | `clock.looping_call` |
| `synapse/handlers/stats.py:110` | `stats.notify_new_event` | notifier-driven |
| `synapse/handlers/user_directory.py:217` | `user_directory.notify_new_event` | notifier-driven |

The presence dispatcher at `synapse/handlers/presence.py:865-871` is the shortest-interval looper:

```python
self.clock.call_later(
    Duration(seconds=30),
    self.clock.looping_call,
    self._dispatch_handle_timeouts,
    Duration(seconds=5),         # ← every 5 s
)
```

### What hostname-lookup does NOT explain

Read of `synapse/tenant_registry.py:158-180` (`get_tenant`) and `synapse/http/site.py:1030-1050` (`SynapseSite.get_tenant_for_host`):

- `self._tenants: dict[str, TenantConfig]` → `.get()` is O(1).
- `self._hostname_aliases: dict[str, str]` → `.get()` is O(1).
- No iteration of `self._tenants` exists on the request hot path. A repo-wide grep for `for … in self._tenants` returns two hits (`tenant_registry.py:219` in `get_all_server_names`, `rest/admin/tenants.py:84` in the list endpoint) — neither is reached per-request.

Per-request tenant resolution is O(1). This was the first hypothesis when density latency was noticed; it was wrong. Reading the code disproved it before any measurement was needed.

## Measurements

All four runs use the same k6 profile (`stress-test-noroom.js`: warm-up 20 → 50 → 100 → 200 VU sustained → cool-down, total ~5m10s) against the same host and `docker-demo/` stack. Only tenant count and the fan-out flag vary.

### Top-line latencies

| Metric | 4 tenants | 10 tenants | **50 tenants** | 100 tenants |
|---|---|---|---|---|
| fan-out | on | on | **on** | **OFF** |
| Total requests | 52,666 | 52,714 | 52,423 | 52,908 |
| Total iterations | 16,940 | 16,968 | 16,877 | 17,031 |
| Throughput (req/s) | 168 | 168 | 168 | 168 |
| `http_req_duration` avg | 3.14 ms | 4.40 ms | 7.37 ms | 3.35 ms |
| `http_req_duration` p50 | 1.85 ms | 2.37 ms | 2.13 ms | 1.98 ms |
| `http_req_duration` p95 | **4.89 ms** | **8.83 ms** | **17.60 ms** | **5.25 ms** |
| `http_req_duration` max | 250 ms | 424 ms | 1,040 ms | 252 ms |
| checks pass rate | 100% | 100% | 100% | 100% |
| `http_req_failed` | 3.05% | 3.05% | 3.03% | 3.05% |
| isolation_check_passed | 100% | 100% | 100% | 100% |
| public-schema leaks | 0 | 0 | 0 | 0 |
| strict-mode assertion fires | 0 | 0 | 0 | 0 |

The `http_req_failed` rate is the cross-tenant-isolation probe receiving expected 401s. It is scored as a passing check.

### Per-endpoint p95 (ms)

| Endpoint | 4t | 10t | **50t** | 100t (fan-out off) |
|---|---|---|---|---|
| `versions` (static, unauth) | 3.07 | 3.72 | **18.81** | 3.14 |
| `whoami` | 2.93 | 5.38 | 8.91 | 3.11 |
| `sync?timeout=0` | 6.48 | 11.37 | 21.01 | 6.9 |
| `login` | 238 | 296 | 403 | 238 |

The `versions` endpoint does not touch the database and does no authentication. It's the cleanest per-tenant-count signal.

### Per-endpoint median (ms) at 50 tenants

| Endpoint | avg | p50 | p95 | max |
|---|---|---|---|---|
| `versions` | 8.17 | **1.82** | 18.81 | 825 |
| `whoami` | 3.66 | 1.44 | 8.91 | 780 |
| `sync` | 6.78 | 3.15 | 21.01 | 865 |

`versions` p50 at 50 tenants (1.82 ms) equals `versions` p50 at 10 tenants (1.82 ms). Median is flat; only the tail moves.

### Memory (RSS of `synapse-demo-server`)

All samples taken via `docker stats --no-stream` + `cat /proc/1/status | grep VmRSS` on the Synapse container. Host has 15 GB RAM.

| Phase | 4 tenants | 10 tenants | 50 tenants | 100 tenants (fan-out off) |
|---|---|---|---|---|
| T0 — cold boot, 0 tenants | ~146 MB | 146 MB | 147 MB | 145 MB |
| T1 — N tenants + N×10 users, idle | ~148 MB | 150 MB | 158 MB | 151 MB |
| Peak RSS during 200-VU run | ~171 MB | 171 MB | 186 MB | 172 MB |

### Fairness — per-tenant `whoami` mean latency (InfluxDB)

Measured by `SELECT MEAN("value"), COUNT("value"), MAX("value") FROM "whoami_duration" WHERE time > now() - 10m GROUP BY "tenant"`.

| | 10 tenants (fan-out on) | 50 tenants (fan-out on) | 100 tenants (fan-out OFF) |
|---|---|---|---|
| Mean of per-tenant means | 2.34 ms | 3.67 ms | 1.64 ms |
| Min / max tenant mean | 2.28 / 2.43 ms | 2.63 / 9.50 ms | 1.49 / 1.92 ms |
| Spread (max / min) | **1.07×** | **3.62×** | **1.29×** |
| Iterations per tenant | 1,549 – 1,786 | 245 – 429 | 76 – 265 |

At 50 tenants the slowest tenant by mean was `stress-050.localhost` (9.50 ms), followed by `stress-010`, `stress-012`, `stress-015`, `stress-034`. Fastest were `stress-005`, `stress-021`, `stress-039`, `stress-024`, `stress-023`. At 100 tenants (fan-out off) the top-5 slowest were `stress-076`, `stress-090`, `stress-055`, `stress-022`, `stress-099` — no consistent ordinal bias.

## Direct evidence that fan-out is active

During the 50-tenant run, `curl http://localhost:9000/_synapse/metrics | grep synapse_background_process_start_count_total | awk -F'name="' '{split($2, a, "\""); print a[1]}' | sort | uniq -c | sort -rn | head` produced the following (name → distinct `server_name` labels present):

| Background process | Distinct server_name labels |
|---|---|
| `user_directory.notify_new_event` | 51 |
| `stats.notify_new_event` | 51 |
| `room_forgetter.notify_new_event` | 51 |
| `presence.notify_new_event` | 51 |
| `_maybe_retry_device_resync` | 51 |
| `handle_presence_timeouts` | 51 |
| `_handle_new_device_update_async` | 51 |
| `delayed_events.notify_new_event` | 51 |
| `send_dummy_events_to_fill_extremities` | 50 |
| `persist_presence_changes` | 50 |
| `expire_old_sessions` | 50 |
| `update_client_ips` | 45 |
| `typing._handle_timeouts` | 45 |
| `prune_old_user_ips` | 45 |

51 = 50 configured tenants + the `localhost` default tenant. Each row is one background-process *name* that spawned with that many distinct tenant labels during the 5-minute run. Per-tenant total invocations for any one row vary (1–19 per tenant per run depending on looper interval); they are visible in the full metric dump.

### The 100-tenant fan-out-off PoC

The diagnostic flag added to `synapse/tenant_background.py::run_as_background_process_per_tenant`:

```python
if _FANOUT_DISABLED:            # env SYNAPSE_MT_DISABLE_FANOUT=1
    if not _FANOUT_DISABLED_LOGGED:
        logger.warning("… skipping ALL per-tenant background fan-out …")
        _FANOUT_DISABLED_LOGGED = True
    return []
```

When the 100-tenant run was executed with this flag set (via `docker-compose.override.yml`), `curl http://localhost:9000/_synapse/metrics` enumerated only **35 distinct `synapse_background_process_start_count_total{name=…}` rows** — all non-fan-out maintenance processes (`background_updates`, `_censor_redactions`, `clean_scheduled_tasks`, `_cleanup_locks`, etc.). None of the 14 per-tenant callers in the table above appeared. The diagnostic warning was emitted exactly once (module-level `_FANOUT_DISABLED_LOGGED` flag).

Under this configuration, with 2.5× more tenants than the 50-tenant baseline, `versions` p95 dropped from 18.81 ms to 3.14 ms — matching the 4-tenant post-fix baseline (3.07 ms) within 0.07 ms. No code change other than the env gate.

## What the measurements support

1. **The fan-out pattern is active at the scale claimed.** The Prometheus row count proves 14 process names × up-to-51 tenant labels each were created over a 5-minute window at N=50.

2. **Disabling the fan-out collapses the density latency to the 4-tenant baseline, at 2.5× the tenant count.** The 100-tenant-fan-out-off run is the controlled experiment.

3. **Tenant-registry O(1) resolution is not the cause.** Source reading plus the flat median at 50 tenants rule out per-request linear cost.

4. **Per-tenant schema isolation is not affected by the fan-out.** `public.*` leaks were zero in every run, as were strict-mode assertion fires. The fan-out is a latency / fairness issue, not an isolation-boundary issue.

5. **Memory scales sub-linearly in tenant count.** From 10 to 50 tenants the per-tenant idle cost was ~0.2 MB; from 50 to 100 tenants (with fan-out off) the per-tenant idle cost was ~−0.07 MB (100-tenant T1 was lower than 50-tenant T1). Fan-out costs memory too: 50 tenants with fan-out on = 186 MB peak, 100 tenants with fan-out off = 172 MB peak.

## What we correlated but did not prove

These statements are consistent with the data but were not causally demonstrated by a controlled test. Flagged so future investigators can design the test if they need certainty.

- **Iteration order explains `stress-050` being the slowest at 50 tenants.** `self._tenants` is a dict populated in DB-insertion order, and `registry.get_all_tenants()` iterates that order. The slowest-tenant-is-last pattern is consistent with late-dispatch-position on each burst. To prove it: provision the 50 tenants in reverse order and re-run. If `stress-001` is now slowest, confirmed.

- **The 825 ms `versions` max at 50 tenants is a single overlap between a request arrival and a fan-out burst.** We did not correlate the request timestamp to synapse-log events at the same instant. To prove it: during a repro, log `time.monotonic()` at fan-out start/end and at request entry/exit, then align tails.

- **Each fan-out spawn costs ~0.2–0.5 ms of reactor time.** Derived from the p95 delta divided by N, not measured directly. To prove it: `py-spy record -p $(pidof synapse)` during a tick, and measure the `run_as_background_process` frame span.

## Observed environment settings

- Synapse container image: rebuilt from `feature/multi-tenant` for each run; `docker images synapse:multi-tenant` confirmed the current SHA matched HEAD.
- `cp_max: 50` (`docker-demo/config/homeserver.yaml` line ~46). Raised from upstream default 10 earlier in the density-harness work.
- `pool_mode: session`, `default_pool_size: 50` (`docker-demo/config/pgbouncer.ini`).
- `SYNAPSE_MT_STRICT_STATE_GROUPS=1` was set for every run in this session via `docker-compose.override.yml`. Zero assertions fired in any run.
- Observability stack (InfluxDB + Grafana) from `docker-demo/stress-test/docker-compose.yml` running on `docker-demo_synapse-demo-net`.

## Reproduce steps

```bash
cd /home/monta/Documents/workspace/synapse-multitenant/docker-demo
# clean-slate both stacks
docker compose -f stress-test/docker-compose.yml down
docker compose down -v
docker compose up -d && sleep 30
docker compose -f stress-test/docker-compose.yml up -d

# provision N tenants + register 10 users each (idempotent; ~2–6 min)
cd stress-test
python3 setup_noroom.py --tenants 50        # or 4, 10, 100

# k6 with InfluxDB output
docker run --rm \
  --network docker-demo_synapse-demo-net \
  -v "$(pwd):/scripts" -w /scripts \
  grafana/k6 run \
  --out "influxdb=http://stress-influxdb:8086/k6" \
  --env BASE_URL="https://synapse-demo-traefik" \
  --summary-export=/scripts/k6-summary.json \
  stress-test-noroom.js
```

To reproduce the fan-out-off PoC, temporarily add `SYNAPSE_MT_DISABLE_FANOUT: "1"` to the `synapse` service in `docker-compose.override.yml` and rebuild the Synapse image with the diagnostic gate (the gate itself was not committed — see "Current state of the code" below).

## Current state of the code

As of commit `6eac684c9` on `feature/multi-tenant`:

- The helper `run_as_background_process_per_tenant` is unchanged in production — no fix has been merged.
- The diagnostic `SYNAPSE_MT_DISABLE_FANOUT` gate used for the 100-tenant PoC is **not committed**. It lives only in the local working tree of whoever ran the experiment. Re-applying requires the small change described in "The 100-tenant fan-out-off PoC" above.
- The measured impact appears in the Scene-E explainer in the multi-tenancy-workflow animation (`multi-tenancy-workflow/app.js`, registered as tab 5 / shortcut `5`), which renders the 14 callsites, the helper, and the burst animation next to the measured numbers.

## Mitigation options discussed (not evaluated, not implemented)

These were enumerated in the in-session discussion and are recorded here for future reference. None has been built or measured.

1. **Stagger within the tick window** — spawn via `clock.call_later(i × Δ, …)` instead of a synchronous loop.
2. **Skip zero-work tenants before fan-out** — precompute a work-present predicate; only dispatch for tenants with real work.
3. **Amortize to one process per looper** — single background process iterates tenants inside a `with tenant_context(_t):` block.
4. **Semaphore-bounded fan-out** — wrap the spawn loop in a K-wide `DeferredSemaphore`.
5. **Jitter `looping_call` intervals** — add a random fraction to each interval so the 8+ loopers don't coincide.
6. **Consolidate sibling loopers** — merge multiple `*.notify_new_event` per-tenant processes into one "tenant tick" process.
7. **Event-driven dispatch from the wheel / notifier** — replace polling-and-filtering with direct per-tenant callbacks.
8. **Per-tenant workers** — separate worker processes per tenant; removes the reactor-thread fan-out from the main process.

Each has observable-behavior tradeoffs (metric cardinality, failure isolation, per-tenant freshness, per-tenant fairness of background work). See in-session notes for details if this moves to a fix PR.

## What is not yet measured

- 100-tenant run with fan-out ON (we only measured 100 with fan-out off).
- 200+ tenants at any fan-out setting.
- Long-haul (hours) at any tenant count.
- Tenant asymmetry (one big + many small).
- Federation path at density.
- Profile of actual reactor-ms-per-spawn.
- Reverse-order provisioning to confirm iteration-order fairness.

## References

- Source file (helper): `synapse/tenant_background.py`
- Scene-E explainer: `multi-tenancy-workflow/app.js` (SceneE class), `multi-tenancy-workflow/data.js` (`FANOUT_CALLSITES`, `FANOUT_MEASUREMENTS`)
- Prior docs: `docker-demo/stress-test/result-2026-04-21.md`, `docker-demo/stress-test/density-2026-04-21.md`, `docker-demo/stress-test/finding-density-versions-p95.md`
- Commits: `8e6022abd` (initial post-fix results), `a70c41737` (density 10+50), `ee497bd73` (versions-p95 investigation), `6eac684c9` (Scene-E explainer)
