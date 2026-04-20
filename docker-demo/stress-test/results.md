# Stress-test results — 2026-04-20

## Environment

- Stack: `docker-demo/` fresh `docker compose up -d` after `down -v`
- Branch: `feature/multi-tenant` @ `4486a497c`
- Tenants: `stress-a.localhost`, `stress-b.localhost`, `stress-c.localhost`, `stress-d.localhost` (4 total) — provisioned via the control plane after stack came up, with `registration_enabled=true`
- Users: 10 per tenant (40 total, passwords `stresstest`)
- Rooms: **none** (room creation fails on this build — see "Blocker" below)
- k6 runner: `grafana/k6` image via Docker, on `docker-demo_synapse-demo-net`, target `https://synapse-demo-traefik` with TLS verify disabled

## Ramp profile

| Stage      | VUs     | Duration |
|------------|---------|----------|
| warm-up    | → 20    | 20s      |
| moderate   | → 50    | 30s      |
| over cp_max| → 100   | 30s      |
| sustained  | 100     | 2m       |
| overload   | → 200   | 30s      |
| sustained  | 200     | 1m       |
| cool-down  | → 0     | 20s      |
| **total**  | peak 200| **~5m10s** |

## Per-iteration workload (stress-test-noroom.js)

Each VU iteration did:

1. `POST /_matrix/client/v3/login` (cached per VU after first call)
2. `GET  /_matrix/client/versions`
3. `GET  /_matrix/client/v3/account/whoami`
4. `GET  /_matrix/client/v3/sync?timeout=0`
5. Every 10th iteration: cross-tenant isolation probe — present the VU's token under a sibling tenant's `Host` header; expect 401/403/404.

## Top-line numbers

| Metric                              | Value                                |
|-------------------------------------|--------------------------------------|
| Total HTTP requests                 | **52,372** (168 RPS)                 |
| Total iterations                    | **16,940** (54/s)                    |
| Total checks                        | 69,190                               |
| `http_req_duration` avg / p50 / p95 / max | 5.90 / 2.68 / **12.47** / 762.61 ms  |
| `http_req_failed` rate              | 18.28% (9,576 / 52,372)              |
| Check pass rate                     | 78.58% (54,370 / 69,190)             |
| Data received / sent                | 87 MB / 3.0 MB                       |

### Per-endpoint latencies (ms)

| Endpoint           | avg    | p50    | p95    | max     |
|--------------------|--------|--------|--------|---------|
| `/login`           | 237.59 | 248.79 | 314.96 | 762.61  |
| `/versions`        | 4.43   | 2.44   | 9.94   | 508.47  |
| `/account/whoami`  | 3.28   | 2.13   | 8.27   | 549.13  |
| `/sync?timeout=0`  | 5.83   | 3.78   | 15.67  | 553.07  |

## Thresholds — pass / fail

| Threshold                           | Target   | Actual  | Result |
|-------------------------------------|----------|---------|--------|
| `http_req_duration p(95) < 5000ms`  | <5000ms  | 12.47ms | PASS   |
| `http_req_failed rate < 15%`        | <15%     | 18.28%  | FAIL   |
| `checks rate > 90%`                 | >90%     | 78.58%  | FAIL   |

Latency is excellent end-to-end (p95 under 13ms even at 200 VUs), so the stack is not compute- or connection-pool-bound in the tested workload. The threshold failures are driven by two distinct problems: a flaky login race and a cross-tenant isolation defect (next two sections).

## Per-check breakdown

| Check                                | Pass rate                      |
|--------------------------------------|--------------------------------|
| `login 200`                          | 62%  (200/322 login attempts)  |
| `versions 200`                       | **100%** (all iterations)      |
| `whoami 200`                         | 73%  (12,306 / 16,818)         |
| `whoami user matches tenant`         | 73%  (12,306 / 16,818)         |
| `sync 200`                           | 73%  (12,308 / 16,818)         |
| `cross-tenant token rejected`        | **27%** (432 / 1,596)          |

Observations:

- `versions` is a static, unauth'd endpoint — 100% green under 200 VUs.
- `whoami` / `sync` both fail at an identical 27% rate — same root cause: the VU's cached login token becomes invalid partway through the run (the VU sees one successful login, then all subsequent auth'd calls 401 for the rest of its life). This is consistent with the `Login raced against device deletion` / device-cleanup race seen during bootstrap.
- Login itself has 38% failure once concurrency reaches the 100–200 VU plateau, for the same reason.

## Finding #1 — Cross-tenant token leak (confirmed, reproducible at rest)

**27% reject rate on cross-tenant isolation checks.** A token minted for tenant X is accepted by tenants Y/Z/W the majority of the time.

Manual reproduction after the run finished (idle load, single request):

```console
$ TOK=$(curl … Host: stress-a.localhost … /_matrix/client/v3/login)
$ curl -k -H "Host: stress-b.localhost" -H "Authorization: Bearer $TOK" \
       https://localhost/_matrix/client/v3/account/whoami
HTTP 200
{"user_id":"@user0:stress-a.localhost","is_guest":false,"device_id":"FDHJNGRVDS"}
```

Result: whoami against `stress-b.localhost` with a `stress-a.localhost` token returns **200** and hands back `@user0:stress-a.localhost`. Same for `stress-c.localhost` and `stress-d.localhost`.

This violates the design invariant stated in `CLAUDE.md`: *"tenant isolation is a security boundary… never leak data across `TenantConfig` boundaries."* The 27% pass rate in the k6 run is partly because some of those checks saw a 401 from *unrelated* reasons (invalidated token, timeout) — the steady-state behaviour is that cross-tenant token reuse succeeds.

Likely location: `access_tokens` lookups aren't constrained to the current tenant's schema, or the schema-selection happens after token validation. Worth checking the auth path in this fork.

## Finding #2 — `createRoom` is broken on this build (stress-test blocker)

The original `stress-test.js` sends messages into a per-tenant room. Bootstrapping those rooms via `POST /_matrix/client/v3/createRoom` fails reliably with HTTP 500. Example from a fresh-stack `setup.py` run:

```
Synapse log:
  Exception: Trying to persist state with unpersisted prev_group: 20
```

and in other attempts:

```
  psycopg2.errors.UniqueViolation: state_group_edges_state_group_prev_state_group_idx
  psycopg2.errors.UniqueViolation: state_groups_persisting_pkey …(19, master) already exists
  psycopg2.errors.UniqueViolation: sliding_sync_joined_rooms_event_stream_ordering_idx …(18) already exists
```

DB inspection on a freshly-booted stack, after one failed `createRoom`:

```
public.state_groups             -> 17 rows (ids 1..17)    <-- LEAKED
tenant_stress_a_localhost.state_groups -> 0 rows
public.state_group_id_seq       -> last_value = 17
tenant_stress_a_localhost.state_group_id_seq -> last_value = 20
```

State groups that should have been written to the tenant schema landed in `public`, while the tenant's sequence advanced as if they had been in-schema. The result is that Synapse sees state_group 20 "exists" (per the sequence) but can't find it in any schema it expects, and persistence aborts.

This is what forced the stress-test to run against a room-free workload. Fixing this is the prerequisite for running the original room-messaging load test.

## Finding #3 — Tenant registry is not hydrated on startup

A fresh `docker compose up -d` starts Synapse with:

```
synapse.tenant_registry - Tenant registry initialized with 0 tenants: []
```

Traffic to tenant hosts falls through to the default `server_name=localhost`: users get `@user0:localhost`, rooms get created in `public` schema, etc. To get multi-tenant routing working I had to explicitly:

```bash
curl -k -X POST \
     -H "Host: stress-a.localhost" \
     -H "Authorization: Bearer demo-reload-secret" \
     https://localhost/_synapse/admin/v1/tenants/reload
```

The control-plane container does not push tenants into Synapse on its own startup; it's a reactive API only. In a dev / stress-test flow that restarts Synapse, the tenant registry is effectively empty until something triggers a reload.

## Changes made to the test harness

- `setup.py`: point at the provisioned hosts (`stress-{a..d}.localhost`), use the correct `registration_shared_secret` (`demo_shared_secret_change_in_production`), switch to `https://` + port 443, disable TLS verify, add retries on the register/login races.
- `run.sh`: default to HTTPS:443, `-k` on curl health probe, `--summary-export=k6-summary.json`, Traefik-based BASE_URL when running via Docker k6.
- `stress-test.js`: add `insecureSkipTLSVerify: true`, default BASE_URL to `https://localhost`.
- New: `setup_noroom.py` — user-only bootstrap for the room-free variant.
- New: `stress-test-noroom.js` — login + versions + whoami + sync + isolation check; the variant actually executed in this run.

Raw k6 output: `k6-summary.json` (same directory).

## Recommended next steps

1. Fix the `createRoom` state-group-schema leak so the full room-messaging stress test can run. Likely in the state persistence path around `_mark_state_groups_as_persisting_txn` / state group ID generation — tenant context is not being applied consistently.
2. Investigate the cross-tenant token acceptance in the auth / token lookup path. This is the higher-severity of the two issues (it's a security boundary, not a liveness one).
3. Make the tenant registry self-hydrate on startup (or have the control plane push after boot) so a cold stack is immediately usable without an out-of-band `/_synapse/admin/v1/tenants/reload` call.
4. Once (1) is fixed, re-run `stress-test.js` (the original, room-based variant) — the stack at 200 VUs is latency-healthy; the real budget is probably several hundred VUs higher before cp_max=10 becomes the bottleneck.
