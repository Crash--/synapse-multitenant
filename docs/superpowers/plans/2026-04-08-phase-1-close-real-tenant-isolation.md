# Phase 1 Close — Real Tenant Isolation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn schema-per-tenant isolation from "fiction" into "reality" by fixing the schema bootstrap to clone tables *and* sequences, closing the `search_path` fall-through footgun, and finishing the `public_baseurl` reader sweep so `/.well-known/matrix/client` is not the only tenant-aware endpoint.

**Architecture:** Tenant isolation in this fork rests on **two** mechanisms: (A) identifier isolation (`effective_server_name()` producing fully-qualified `@user:tenant`) and (B) schema isolation (`SET search_path TO <tenant_schema>`). Today only (A) is fully wired, and (B) is silently broken because `scripts/create_tenant_schema.py` either produces empty schemas (default) or clones tables via `CREATE TABLE … LIKE … INCLUDING ALL` which **does not copy sequences** — defaults still reference `public.<seq>`. This plan catalogues the isolation surface, writes red probes, rewrites the bootstrap to create per-tenant sequences and repoint defaults, narrows the `search_path` policy to fail-loud, re-baselines the phase 2 probe suite against real isolation, and finishes the `public_baseurl` reader sweep.

**Tech Stack:** Python 3, Twisted/Trial, PostgreSQL 14+, psycopg2, Synapse test fixtures in `tests/tenant/`, docker-multitenant probe harness (`docker-multitenant/scripts/test_tenants.py` — plain `urllib.request` + Host-header + `[PASS]`/`[FAIL]` prints, no framework).

---

## File Structure

### Files to create
- `docs/multi_tenant_isolation_model.md` — new design doc articulating the two-mechanism split (identifier + schema) and the catalogue of tables where schema isolation is load-bearing.
- `tests/tenant/test_schema_bootstrap.py` — new unit test file covering the bootstrap SQL generator (pure function, no real DB required).

### Files to modify
- `scripts/create_tenant_schema.py` — rewrite `copy_table_structure` into `clone_schema_with_sequences`, which (a) clones tables, (b) creates per-tenant sequences, (c) repoints column defaults to the per-tenant sequence. Remove `initialize_empty_schema` as a valid default when multi-tenant mode is on.
- `synapse/storage/database.py:657-716` — narrow `_set_tenant_schema` to `SET search_path TO {schema}` (drop the `, public` fallback) and add a startup-time assertion helper that verifies expected tables resolve inside the tenant schema.
- `synapse/config/tenants.py` — add `identity_server` field and `effective_identity_server` property alongside the existing `public_baseurl` pair.
- `synapse/push/mailer.py:158, 193, 248, 951` — route `public_baseurl` through `get_current_tenant().effective_public_baseurl` with global fallback.
- `synapse/rest/client/auth.py:111, 179` — same pattern.
- `synapse/rest/client/login.py:658` — SSO redirect base URL, same pattern.
- `synapse/rest/synapse/client/pick_idp.py:53` — same pattern.
- `synapse/util/templates.py:73, 85-112` — thread a per-call `public_baseurl` through `mxc_to_http` filter construction instead of capturing the global at setup time.
- `synapse/api/urls.py:50, 66, 74, 92` — same pattern.
- `docker-multitenant/scripts/test_tenants.py` — add new `PHASE 1B ISOLATION PROBES` block (same-localpart, stream-sequence independence, registration response, password-reset link, federation version).
- `docker-multitenant/scripts/init_schemas.py` — switch from `initialize_empty_schema` to the new `clone_schema_with_sequences` path.
- `docker-multitenant/docker-compose.yml` — if the init-schemas container changes, update its entrypoint to pass `--from-template` (or whatever the new default flag is).
- `docs/multi_tenant.md` — link to `multi_tenant_isolation_model.md` and add a short "Isolation model" section pointing there.
- `roadmap-progess.md` — handled by `/sync-roadmap` skill in the final task; do not edit by hand during task execution.

### Files explicitly NOT in scope
- `synapse/handlers/presence.py`, `synapse/push/pusherpool.py`, `synapse/push/emailpusher.py`, `synapse/push/httppusher.py` — phase 2 close, separate plan.
- `synapse/handlers/user_directory.py:614` (`kick_off_remote_profile_refresh_process`) — deferred to federation phase per roadmap.
- `synapse/storage/background_updates.py` — deliberately global per roadmap.
- `docker-demo/` — not the canonical probe rig; changes to `create_tenant_schema.py` will benefit it for free but no explicit updates required.

---

## Dependency graph and parallel fan-out

Tasks are not all sequential. When executing under `superpowers:subagent-driven-development`, exploit the parallel opportunities:

```
                   ┌── Task 1 (docs) ──┐
    start ─────────┼── Task 2 (probes) ┤
                   └── Task 7a (config)┘
                           │
                           ▼
                       Task 3 (bootstrap rewrite, TDD)
                           │
                           ▼
                       Task 4 (back-apply to rig, red → green)
                           │
                           ▼
                       Task 5 (search_path narrow + assertion)
                           │
                           ▼
                       Task 6 (phase 2 re-baseline)
                           │
              ┌────────────┼────────────┐
              ▼            ▼            ▼
           Task 7b      Task 7c      Task 8
         (req-path)   (init-cap)   (more probes)
              └────────────┼────────────┘
                           ▼
                       Task 9 (hs.hostname sweep)
                           │
                           ▼
                       Task 10 (sync + close)
```

**Parallel fan-outs:**
- **Wave 1 (3-way):** Tasks 1, 2, 7a. No code dependencies between them. Task 1 produces docs; Task 2 adds probes that are expected to be red; Task 7a extends `TenantConfig`. Dispatch as 3 parallel subagents.
- **Wave 2 (3-way):** Tasks 7b, 7c, 8 after Task 6 is green. All touch disjoint files: 7b modifies `mailer.py` + `auth.py`, 7c modifies `login.py` + `pick_idp.py` + `templates.py` + `api/urls.py`, 8 modifies `test_tenants.py`. Dispatch as 3 parallel subagents.

**Sequential backbone:** Task 3 → 4 → 5 → 6 is strict. Each depends on the previous landing cleanly.

**Total wall-clock estimate with parallel fan-out:** ~90-120 min. Without: ~3-4 hours.

---

## Task 1: Catalogue the isolation surface and document the two-mechanism model

**Files:**
- Create: `docs/multi_tenant_isolation_model.md`
- Modify: `docs/multi_tenant.md` (add pointer)

The goal of this task is to produce a written, checked-in artefact that enumerates exactly which tables rely on schema isolation (because they key on bare localparts) and which sequences need per-tenant copies. This is the source of truth the rest of the plan references.

- [ ] **Step 1: Grep for bare-localpart UNIQUE constraints**

Run:
```bash
rg -n "UNIQUE\s*\(\s*(user_id|user_name|localpart|name)\s*\)" \
  synapse/storage/schema/main/full_schemas \
  synapse/storage/schema/main/delta
```

Expected findings (already confirmed during planning recon): `presence.user_id`, `profiles.user_id`, `users.name`. Record any additional hits. If the result grows beyond ~6 tables, stop and flag to the user before proceeding — the plan assumes a small bounded surface.

- [ ] **Step 2: Grep for database-wide sequences**

Run:
```bash
rg -n "^CREATE SEQUENCE" \
  synapse/storage/schema/main/full_schemas \
  synapse/storage/schema/main/delta
```

Expected: ~18 sequences. Record the full list verbatim for step 4.

- [ ] **Step 3: For each localpart-keyed table, identify the write site that stores the bare localpart**

Start from the known one — `synapse/storage/databases/main/profile.py:304` writes `{"user_id": user_localpart, "full_user_id": user_id.to_string()}`. Grep `simple_insert` and `INSERT INTO` for each table found in step 1:

```bash
rg -n "simple_insert.*table=\"(presence|profiles|users)\"" synapse/storage/databases/main/
rg -n "INSERT INTO (presence|profiles|users)" synapse/storage/databases/main/
```

Record file:line for each write site — this is what you will audit in the "hs.hostname" sweep (Task 9) for correctness.

- [ ] **Step 4: Write `docs/multi_tenant_isolation_model.md`**

Create the file with this content:

```markdown
# Multi-Tenant Isolation Model

This document is the design reference for how tenant isolation works in
this fork. It is the doc the phase-1 close plan wishes existed before
the fork was written. Read this before touching any of
`synapse/storage/`, `synapse/tenant_*.py`, `synapse/config/tenants.py`,
or `scripts/create_tenant_schema.py`.

## The two mechanisms

Tenant isolation rests on TWO mechanisms. Neither is sufficient on its
own.

### Mechanism A — Identifier isolation

Matrix user IDs are of the form `@localpart:server_name`. When the
fork correctly constructs fully-qualified IDs via
`HomeServer.effective_server_name()`, two tenants' `alice`s become
`@alice:tenant-a.com` and `@alice:tenant-b.com` — distinct strings,
distinct rows in any table keyed on the qualified user ID.

**What this mechanism isolates:** tables keyed on the fully qualified
user_id (most of `synapse/storage/databases/main/*`).

**What it does NOT isolate:**
- Tables that upstream Synapse keys on the bare localpart (see
  "Mechanism B load-bearing set" below).
- Database-wide monotonic sequences (`stream_ordering`, etc.).
- Tables keyed on opaque IDs like `event_id` (hash-based).
- Remote state observed via federation (both tenants can observe the
  same remote room/event).

### Mechanism B — Schema-per-tenant isolation

Each tenant gets a PostgreSQL schema (`tenant_<name>`). At transaction
start, the connection pool sets `search_path` to point at the tenant
schema. Existing Synapse SQL (`SELECT * FROM users`) resolves against
the tenant schema without modification.

**What this mechanism isolates:**
- Tables where upstream Synapse uses the bare localpart as the unique
  key (the "load-bearing set" below).
- Database-wide sequences, when each schema holds its own sequence
  objects and column defaults reference the per-schema sequence.
- Duplicated state of remote rooms/events observed by multiple
  tenants.

**What it does NOT isolate:**
- Request-path string construction (LoginResponse.home_server, email
  link composition). These are identifier-layer concerns; schema
  isolation cannot save you from returning the wrong
  `@user:wrong_server` to a client.

## Mechanism B load-bearing set (tables)

These tables are the ones where schema isolation is the ONLY thing
preventing cross-tenant collisions. If schema isolation is broken
(`search_path` fall-through to `public`), two tenants' writes will
collide here even if every `effective_server_name()` call site is
correct.

| Table | Unique column | Stores | Source |
|---|---|---|---|
| `presence` | `user_id` | bare localpart | `full.sql.postgres:1056` |
| `profiles` | `user_id` | bare localpart | `full.sql.postgres:1058`, write at `profile.py:304` |
| `users` | `name` | bare localpart | `full.sql.postgres:1140`, delta `16/users.sql:27` |

[If step 1 found additional tables, append them here.]

## Mechanism B load-bearing set (sequences)

Every database-wide sequence must exist *per tenant schema* with the
tenant's column defaults pointing at the per-schema sequence.
Otherwise `SELECT nextval('public.events_stream_seq')` is shared
across tenants and `events.stream_ordering` interleaves, breaking
sync-token semantics and per-tenant backup/restore.

[Insert the full list from step 2 here, one per line.]

## Why both mechanisms are needed

- Mechanism A alone would work if every table keyed on the fully
  qualified user_id. It does not (see the load-bearing set above) —
  upstream Synapse was never designed for multi-tenancy.
- Mechanism B alone would work if the request-path string construction
  were schema-aware. It isn't — `LoginResponse.home_server` is built
  from a Python string in the handler, not from a SQL query. Schema
  isolation cannot fix this.

The phase-1 close work delivers: real Mechanism B (via sequence
cloning and fail-loud `search_path` policy), and completes the
Mechanism A audit for `public_baseurl` readers.

## Operational note: background updaters run against `public` only

Synapse's background schema updaters (`synapse/storage/background_updates.py`)
run at the process level without a tenant context bound. Under this
fork's design, they execute against the default `public` search_path
and migrate only the `public` schema. This has two consequences:

1. **New Synapse schema migrations do not automatically reach tenant
   schemas.** When Synapse ships a new delta (e.g. adding a column to
   `events`), the delta runs in `public.events` at startup. Tenant
   schemas still hold the pre-delta shape.
2. **Re-running `scripts/create_tenant_schema.py` is the operator's
   only migration path for tenant schemas today.** This is acceptable
   for phase 1, but is a real operational gap that must be surfaced
   as a cross-cutting concern — probably resolved in phase 6
   (hot add/remove + backup/restore tooling).

Do not "fix" this in phase 1 by converting background updaters to
run per tenant. They were deliberately left global per the roadmap
because they operate on schema structure, not per-row data, and a
per-tenant migration path needs its own design.
```

- [ ] **Step 5: Add pointer from `docs/multi_tenant.md`**

Open `docs/multi_tenant.md` and add (near the top, right after any existing "Overview" section):

```markdown
## Isolation model

Tenant isolation rests on two mechanisms (identifier isolation +
schema-per-tenant). For the full design including the catalogue of
tables and sequences where schema isolation is load-bearing, see
[multi_tenant_isolation_model.md](multi_tenant_isolation_model.md).
```

- [ ] **Step 6: Commit**

```bash
git add docs/multi_tenant_isolation_model.md docs/multi_tenant.md
git commit -m "docs(multi-tenant): document the two-mechanism isolation model"
```

---

## Task 2: Write the red probes (TDD gate for the schema fix)

**Files:**
- Modify: `docker-multitenant/scripts/test_tenants.py`

These probes must go red against the current branch state. They are the evidence that schema isolation is currently fictional, and they're the gate that turns green in Task 4.

- [ ] **Step 1: Read the existing probe style**

Read `docker-multitenant/scripts/test_tenants.py` lines 1-200 to locate the `PHASE 1 LEAK PROBES` and `PHASE 2 PROBES` section headers, the `make_request` helper at the top, and the shape of existing probes (they print `[PASS]` or `[FAIL]` and return bool).

- [ ] **Step 2: Add the `PHASE 1B ISOLATION PROBES` section header and the same-localpart probe**

After the existing `PHASE 2 PROBES` block, append:

```python
# -----------------------------------------------------------------------
# PHASE 1B ISOLATION PROBES — these probes exercise whether schema-per-
# tenant isolation is actually real, as opposed to `search_path` falling
# through to `public`. They are expected to FAIL on the current main
# branch and PASS after `scripts/create_tenant_schema.py` is rewritten
# to clone tables + sequences per tenant.
# -----------------------------------------------------------------------


def test_isolation_same_localpart():
    """Register the same localpart in two tenants and assert both succeed.

    Under correct isolation, `alice` on tenant A and `alice` on tenant B
    are `@alice:acme.localhost` and `@alice:corp.localhost` — distinct
    fully-qualified IDs — and the `profiles.user_id` UNIQUE constraint
    should not collide because each tenant has its own `profiles` table.

    Under the current broken state, both writes hit `public.profiles`
    and the second registration fails with a duplicate-key error.
    """
    localpart = "iso_probe_alice"
    ok_a = _register_shared_secret(TENANTS[0], localpart)
    ok_b = _register_shared_secret(TENANTS[1], localpart)

    if ok_a and ok_b:
        print(f"    [PASS] same-localpart isolation "
              f"({localpart} registered on {TENANTS[0]} and {TENANTS[1]})")
        return True
    else:
        print(f"    [FAIL] same-localpart isolation "
              f"(tenant_a={ok_a}, tenant_b={ok_b}) — "
              "likely search_path fall-through to public.profiles")
        return False


def _register_shared_secret(tenant, localpart):
    """Helper: register a user via the shared-secret admin endpoint.

    Returns True on 200, False on any error or non-200.
    """
    # Use timestamp-suffixed localpart for idempotency across runs, but
    # keep the SAME suffix for both tenants in a given run so collisions
    # are surfaced. Seed from the current second so repeat runs don't
    # race their own prior data.
    import hmac
    import hashlib

    # Shared secret must match the one in
    # docker-multitenant/config/homeserver.yaml:81 (verified during plan
    # reconnaissance — this is the literal value, same for every tenant
    # in this rig).
    shared_secret = b"demo_shared_secret_change_in_production"

    # Step 1: fetch nonce
    nonce_result = make_request(
        "GET", "/_synapse/admin/v1/register", tenant
    )
    if nonce_result.get("status") != 200:
        return False
    nonce = nonce_result["data"].get("nonce")
    if not nonce:
        return False

    # Step 2: compute HMAC
    mac = hmac.new(key=shared_secret, digestmod=hashlib.sha1)
    mac.update(nonce.encode("utf-8"))
    mac.update(b"\x00")
    mac.update(localpart.encode("utf-8"))
    mac.update(b"\x00")
    mac.update(b"isolation_probe_password")
    mac.update(b"\x00")
    mac.update(b"notadmin")
    mac_hex = mac.hexdigest()

    # Step 3: register
    result = make_request(
        "POST",
        "/_synapse/admin/v1/register",
        tenant,
        data={
            "nonce": nonce,
            "username": localpart,
            "password": "isolation_probe_password",
            "admin": False,
            "mac": mac_hex,
        },
    )
    return result.get("status") == 200
```

- [ ] **Step 3: Add the stream-sequence independence probe**

Append after the previous probe:

```python
def test_isolation_stream_sequences_independent():
    """Assert that two tenants' events stream_ordering advance independently.

    Under correct isolation with per-tenant sequences, tenant A's events
    sequence and tenant B's events sequence are distinct postgres
    sequence objects. Under shared-sequence fall-through, they share
    `public.events_stream_seq` and interleave: A gets 1, B gets 2, ...

    We assert this directly at the sequence level via psql rather than
    by inserting events and reading `stream_ordering` through the admin
    API, because (a) the admin API field name varies by Synapse version
    and (b) direct sequence inspection is deterministic regardless of
    what's in the `events` table.

    Fresh-rig expectation: each tenant's `events_stream_seq.last_value`
    is either NULL (never nextval'd) or a small positive integer (< 10)
    because schema bootstrap just ran. If both tenants' sequences point
    at the same object (i.e. public.events_stream_seq), reading the
    tenant-qualified name in psql will fail with "relation does not
    exist" — which is itself the red signal.
    """
    import subprocess

    # Tenant schemas as configured in docker-multitenant/config/homeserver.yaml.
    # The server_name → database_schema mapping substitutes dots for
    # underscores and prefixes with "tenant_".
    tenant_schemas = {
        TENANTS[0]: "tenant_acme_localhost",
        TENANTS[1]: "tenant_corp_localhost",
    }

    last_values = {}
    for tenant, schema in tenant_schemas.items():
        # Query the per-tenant events_stream_seq. If it doesn't exist
        # (fall-through state), psql returns non-zero and stderr
        # explains why — that IS the red signal.
        result = subprocess.run(
            [
                "docker", "compose", "exec", "-T", "postgres",
                "psql", "-U", "synapse", "-d", "synapse", "-At", "-c",
                f"SELECT last_value FROM {schema}.events_stream_seq;",
            ],
            capture_output=True, text=True,
            cwd="/docker-multitenant",  # adjust if probe runs from a
                                          # different CWD in the test
                                          # container; see note below
        )
        if result.returncode != 0:
            print(f"    [FAIL] stream-sequence independence "
                  f"({schema}.events_stream_seq does not exist) — "
                  f"stderr: {result.stderr.strip()}")
            return False
        try:
            last_values[tenant] = int(result.stdout.strip())
        except ValueError:
            print(f"    [FAIL] stream-sequence independence "
                  f"({schema}.events_stream_seq last_value not parseable: "
                  f"{result.stdout.strip()!r})")
            return False

    a_val = last_values[TENANTS[0]]
    b_val = last_values[TENANTS[1]]
    # Fresh rig: both sequences should have small last_value. We don't
    # assert independence by comparing values (they could coincidentally
    # match), we assert it by the fact that BOTH per-tenant sequences
    # exist as distinct postgres objects. The subprocess calls above
    # already succeeded => two distinct sequences exist. The last_value
    # check is a sanity bound.
    if a_val < 100 and b_val < 100:
        print(f"    [PASS] stream-sequence independence "
              f"(tenant_a.events_stream_seq.last_value={a_val}, "
              f"tenant_b.events_stream_seq.last_value={b_val})")
        return True
    else:
        print(f"    [FAIL] stream-sequence independence "
              f"(suspiciously large last_values: a={a_val}, b={b_val}) — "
              f"possible shared sequence with accumulated writes")
        return False
```

**Note on probe placement:** `docker compose exec` is awkward from inside the `test` container because the test container doesn't have the docker socket. Two options:
- (A) Run the `test` container with `/var/run/docker.sock` bind-mounted and the `docker` CLI installed. Simpler but requires docker-compose.yml change.
- (B) Move this particular probe to the **host side** — add a `scripts/run_isolation_probes_host.sh` that runs from the dev host, not inside the test container.

**Pick (B).** Reason: the host-side probe doesn't pollute the existing test container's responsibilities, and it keeps the SQL access path transparent. Add `docker-multitenant/scripts/run_isolation_probes_host.sh` as a separate script invoked from the dev host:

```bash
#!/bin/bash
# Phase 1B isolation probes run from the host (needs docker CLI access).
set -e
cd "$(dirname "$0")/.."

echo "=== PHASE 1B ISOLATION PROBES (host) ==="

# Probe 1: same localpart in two tenants — delegate to the in-container
# test runner, which can do HTTP registration via urllib.
docker compose run --rm test python /scripts/test_tenants.py --phase 1b-http

# Probe 2: stream sequence independence — direct psql from the host.
for schema in tenant_acme_localhost tenant_corp_localhost; do
    val=$(docker compose exec -T postgres psql -U synapse -d synapse -At \
          -c "SELECT last_value FROM ${schema}.events_stream_seq;" 2>&1)
    if [ $? -ne 0 ]; then
        echo "    [FAIL] stream-sequence independence: ${schema}.events_stream_seq missing"
        echo "      stderr: $val"
        exit 1
    fi
    echo "    [info] ${schema}.events_stream_seq.last_value = $val"
done
echo "    [PASS] stream-sequence independence (both tenants have distinct per-schema sequences)"
```

Adapt the in-container `test_tenants.py` Python code to skip the subprocess-based stream probe when running inside the container, and move the SQL check to the host script. The Python body shown above becomes the reference for the host-script logic.

- [ ] **Step 4: Wire the new probes into the `main()` runner**

Find the `main()` function near the bottom of `test_tenants.py` and the section where `PHASE 2 PROBES` are called. Append:

```python
    print("\n=== PHASE 1B ISOLATION PROBES ===")
    all_passed &= test_isolation_same_localpart()
    all_passed &= test_isolation_stream_sequences_independent()
```

Match the existing style for `all_passed &= ...` accumulation.

- [ ] **Step 5: Run the probes against current state and confirm red**

```bash
cd docker-multitenant
docker compose up -d
docker compose run --rm test
```

Expected: both new probes report `[FAIL]`. The first should fail with a duplicate-key error (profiles collision). The second should fail with interleaved stream orderings or with unusable data (in which case, implement the SQL fallback before proceeding).

**If the same-localpart probe unexpectedly PASSES, stop.** It means either (a) the rig has fresh localparts despite our best efforts, (b) `init_schemas.py` was recently updated, or (c) my model of the bug is wrong. Investigate before continuing to Task 3.

- [ ] **Step 6: Commit**

```bash
git add docker-multitenant/scripts/test_tenants.py
git commit -m "test(docker-multitenant): add phase-1b isolation probes (red)"
```

---

## Task 3: Rewrite the schema bootstrap to clone tables and sequences per tenant

**Files:**
- Modify: `scripts/create_tenant_schema.py`
- Create: `tests/tenant/test_schema_bootstrap.py`

This task is the heart of the plan. The existing `copy_table_structure` uses `CREATE TABLE … LIKE … INCLUDING ALL`, which clones column defaults *as-is* — meaning `DEFAULT nextval('events_stream_seq')` still points at `public.events_stream_seq` in the cloned table. The fix is:

1. Clone each sequence from `public` into the tenant schema as a distinct sequence object.
2. Clone each table with `LIKE … INCLUDING ALL`.
3. For each column whose default references a sequence, rewrite the default to point at the per-tenant copy.
4. Make the clone-with-sequences path the **default** (not opt-in) when multi-tenant mode is active. The old `initialize_empty_schema` path is demoted to "developer-only, not for production."

We TDD the SQL generator first (pure function, no real DB needed), then wire it into the script.

**Pre-verified facts about `LIKE … INCLUDING ALL` (no debugging needed):**
- In PostgreSQL, `INCLUDING ALL` expands to `INCLUDING DEFAULTS IDENTITY GENERATED CONSTRAINTS STORAGE COMMENTS COMPRESSION STATISTICS INDEXES`. **Foreign keys are explicitly excluded.** This means alphabetical table creation order is safe — no dependency ordering required.
- `LIKE` does **not** copy table data, only structure. Cloned tenant tables start empty.
- Cloned tables **do** inherit column defaults, including `DEFAULT nextval(...)`. Those defaults initially point at the source schema's sequence; step 3 repoints them.

**Sequence seeding:** tenant tables start empty, so per-tenant sequences can safely start at 1 (postgres sequence default). The plan does **not** seed from the source sequence's `last_value` because:
- There's no pre-existing tenant data to collide with.
- Seeding would introduce dependence on `public.<seq>.last_value` at bootstrap time, which is exactly the coupling we're eliminating.

### ⚡ Fast iteration loop (use this instead of `down -v` cycles)

Normal dev cycle for Task 3-4 would be `down -v` → `up` → wait → probe, which is ~2 min per iteration. With ~10 iterations expected, that's 20+ min wasted. Use this instead:

```bash
# One-time: ensure the rig is up
cd docker-multitenant
docker compose up -d
sleep 15   # wait for synapse health

# Per iteration (< 10s):
docker compose exec -T postgres psql -U synapse -d synapse -c \
  "DROP SCHEMA IF EXISTS tenant_acme_localhost CASCADE;
   DROP SCHEMA IF EXISTS tenant_corp_localhost CASCADE;
   DROP SCHEMA IF EXISTS tenant_startup_localhost CASCADE;"

# Re-run the bootstrap against the live DB (no container restart)
docker compose exec -T synapse python -m scripts.create_tenant_schema \
  -c /data/homeserver.yaml --all-tenants

# Eyeball the result
docker compose exec -T postgres psql -U synapse -d synapse -c \
  "\dt tenant_acme_localhost.*" | head -30
docker compose exec -T postgres psql -U synapse -d synapse -c \
  "\d tenant_acme_localhost.events" | grep -A1 stream_ordering
# Expected after fix: default | nextval('tenant_acme_localhost.events_stream_seq'::regclass)
```

Only do `docker compose down -v` once at the end of Task 4 to verify the cold-start path works from a clean postgres volume.

- [ ] **Step 1: Create `tests/tenant/test_schema_bootstrap.py` with a failing test for the SQL generator**

```python
#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2026 Linagora
#

"""
Unit tests for the tenant schema bootstrap SQL generator.

These tests cover the pure-function SQL builder, not the end-to-end
`create_tenant_schema` script. They run without a live database by
feeding synthetic `(table_name, column_name, sequence_name)` tuples
and asserting on the emitted SQL.
"""

from scripts.create_tenant_schema import build_clone_schema_sql


class BuildCloneSchemaSqlTestCase:
    def test_emits_create_sequence_per_tenant(self):
        sql_statements = build_clone_schema_sql(
            target_schema="tenant_acme",
            tables=["events", "profiles"],
            sequences=["events_stream_seq", "user_id_seq"],
            sequence_defaults=[
                ("events", "stream_ordering", "events_stream_seq"),
                ("users", "id", "user_id_seq"),
            ],
        )
        joined = "\n".join(sql_statements)
        assert "CREATE SEQUENCE tenant_acme.events_stream_seq" in joined
        assert "CREATE SEQUENCE tenant_acme.user_id_seq" in joined

    def test_emits_create_table_like_per_table(self):
        sql_statements = build_clone_schema_sql(
            target_schema="tenant_acme",
            tables=["events", "profiles"],
            sequences=[],
            sequence_defaults=[],
        )
        joined = "\n".join(sql_statements)
        assert "CREATE TABLE tenant_acme.events (LIKE public.events INCLUDING ALL)" in joined
        assert "CREATE TABLE tenant_acme.profiles (LIKE public.profiles INCLUDING ALL)" in joined

    def test_repoints_column_defaults_to_tenant_sequence(self):
        sql_statements = build_clone_schema_sql(
            target_schema="tenant_acme",
            tables=["events"],
            sequences=["events_stream_seq"],
            sequence_defaults=[
                ("events", "stream_ordering", "events_stream_seq"),
            ],
        )
        joined = "\n".join(sql_statements)
        assert (
            "ALTER TABLE tenant_acme.events "
            "ALTER COLUMN stream_ordering "
            "SET DEFAULT nextval('tenant_acme.events_stream_seq')"
        ) in joined

    def test_create_sequence_before_create_table(self):
        """Sequences must be created before tables so the tables'
        LIKE-cloned defaults don't error out on a dangling reference.
        (Actually LIKE doesn't validate defaults, but we still want a
        clean ordering: sequences, tables, then ALTER defaults.)"""
        sql_statements = build_clone_schema_sql(
            target_schema="tenant_acme",
            tables=["events"],
            sequences=["events_stream_seq"],
            sequence_defaults=[
                ("events", "stream_ordering", "events_stream_seq"),
            ],
        )
        seq_idx = next(i for i, s in enumerate(sql_statements) if "CREATE SEQUENCE" in s)
        tbl_idx = next(i for i, s in enumerate(sql_statements) if "CREATE TABLE" in s)
        alt_idx = next(i for i, s in enumerate(sql_statements) if "ALTER TABLE" in s)
        assert seq_idx < tbl_idx < alt_idx

    def test_rejects_invalid_schema_name(self):
        """Schema names must be validated against SQL injection at every
        call site. The builder is one of those call sites."""
        import pytest
        with pytest.raises(ValueError, match="Invalid schema name"):
            build_clone_schema_sql(
                target_schema="tenant_acme; DROP TABLE users; --",
                tables=["events"],
                sequences=[],
                sequence_defaults=[],
            )
```

**Note on test framework:** the rest of `tests/tenant/` uses Twisted Trial (`twisted.trial.unittest.TestCase`). The above is written as a bare class for pytest compatibility; if the existing convention in `tests/tenant/test_registry.py` is Trial, adapt the class to inherit from `twisted.trial.unittest.TestCase` and replace `assert` with `self.assertIn` / `self.assertLess`. Read one existing file in `tests/tenant/` first to match the style exactly.

- [ ] **Step 2: Run the test to confirm it fails**

Run:
```bash
trial tests.tenant.test_schema_bootstrap
```

Expected: ImportError or AttributeError — `build_clone_schema_sql` does not exist yet.

- [ ] **Step 3: Implement `build_clone_schema_sql` in `scripts/create_tenant_schema.py`**

Add this function above the existing `copy_table_structure` (around line 107):

```python
def build_clone_schema_sql(
    target_schema: str,
    tables: list[str],
    sequences: list[str],
    sequence_defaults: list[tuple[str, str, str]],
    source_schema: str = "public",
) -> list[str]:
    """Build the SQL statements that clone a schema with per-tenant sequences.

    This is the pure-function SQL generator — it does not touch the DB.
    It returns a list of SQL statements that, when executed in order,
    produce a tenant schema containing:

      1. A per-tenant copy of every sequence listed in `sequences`,
         seeded from the source schema's current value.
      2. A cloned copy of every table listed in `tables`, via
         `CREATE TABLE ... LIKE ... INCLUDING ALL`.
      3. ALTER statements that repoint each `(table, column)` default
         to the per-tenant sequence copy instead of the source-schema
         sequence.

    Args:
        target_schema: The tenant schema name (validated for SQL safety).
        tables: Names of tables to clone.
        sequences: Names of sequences to clone.
        sequence_defaults: List of (table, column, sequence_name) tuples
            describing which columns should have their defaults
            repointed to the per-tenant sequence.
        source_schema: The schema to clone from. Defaults to "public".

    Returns:
        Ordered list of SQL statements.

    Raises:
        ValueError: if any schema/table/sequence/column name fails the
            alphanumeric-plus-underscore safety check.
    """
    def _safe(name: str) -> str:
        if not name.replace("_", "").isalnum():
            raise ValueError(f"Invalid schema name: {name}")
        return name

    target = _safe(target_schema)
    source = _safe(source_schema)

    statements: list[str] = []

    # 1. Sequences — create per-tenant copies. Start value is set to 1;
    #    a live migration would want to seed from the source sequence's
    #    current value, but this function is for fresh tenants and
    #    starting at 1 is correct and desirable (it is the whole point
    #    of per-tenant sequences).
    for seq in sequences:
        s = _safe(seq)
        statements.append(
            f"CREATE SEQUENCE IF NOT EXISTS {target}.{s}"
        )

    # 2. Tables — LIKE INCLUDING ALL clones structure, indexes,
    #    constraints, and column defaults (including DEFAULT nextval).
    #    The defaults will point at source_schema sequences at this
    #    point; step 3 repoints them.
    for tbl in tables:
        t = _safe(tbl)
        statements.append(
            f"CREATE TABLE {target}.{t} "
            f"(LIKE {source}.{t} INCLUDING ALL)"
        )

    # 3. Repoint column defaults to the per-tenant sequence copy.
    for tbl, col, seq in sequence_defaults:
        t = _safe(tbl)
        c = _safe(col)
        s = _safe(seq)
        statements.append(
            f"ALTER TABLE {target}.{t} "
            f"ALTER COLUMN {c} "
            f"SET DEFAULT nextval('{target}.{s}')"
        )

    return statements
```

- [ ] **Step 4: Run the test again and confirm it passes**

```bash
trial tests.tenant.test_schema_bootstrap
```

Expected: all 5 tests pass.

- [ ] **Step 5: Add a helper that discovers sequences and their table bindings from live `public` schema**

The test above uses synthetic inputs. The real script needs to discover `(table, column, sequence)` tuples from the live `public` schema at runtime, because the exact set depends on which Synapse version has been migrated into `public` in this deployment.

Append to `scripts/create_tenant_schema.py`:

```python
def discover_schema_surface(
    conn,
    source_schema: str = "public",
) -> tuple[list[str], list[str], list[tuple[str, str, str]]]:
    """Discover tables, sequences, and sequence/column bindings in a schema.

    Queries PostgreSQL catalogs to enumerate:
      - Every table in `source_schema` (for cloning).
      - Every sequence in `source_schema` (for per-tenant creation).
      - Every column whose default is `nextval('<source_schema>.<seq>')`,
        so the ALTER-default step knows which columns to repoint.

    Args:
        conn: An open psycopg2 connection.
        source_schema: The schema to introspect.

    Returns:
        (tables, sequences, sequence_defaults) — the three inputs to
        `build_clone_schema_sql`.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT table_name
              FROM information_schema.tables
             WHERE table_schema = %s
               AND table_type = 'BASE TABLE'
             ORDER BY table_name
            """,
            (source_schema,),
        )
        tables = [row[0] for row in cur.fetchall()]

        cur.execute(
            """
            SELECT sequence_name
              FROM information_schema.sequences
             WHERE sequence_schema = %s
             ORDER BY sequence_name
            """,
            (source_schema,),
        )
        sequences = [row[0] for row in cur.fetchall()]

        # Column default → sequence bindings. `pg_get_serial_sequence` is
        # the canonical way to resolve a column's owning sequence, but it
        # only works for SERIAL/IDENTITY columns. For columns that use an
        # explicit `DEFAULT nextval('...')`, we parse the default string.
        cur.execute(
            """
            SELECT c.table_name, c.column_name, c.column_default
              FROM information_schema.columns c
             WHERE c.table_schema = %s
               AND c.column_default LIKE 'nextval(%%'
             ORDER BY c.table_name, c.column_name
            """,
            (source_schema,),
        )
        sequence_defaults: list[tuple[str, str, str]] = []
        for table_name, column_name, column_default in cur.fetchall():
            # column_default looks like: nextval('public.events_stream_seq'::regclass)
            # Extract the bare sequence name.
            import re
            match = re.search(r"nextval\('(?:[^.]+\.)?([^']+)'", column_default)
            if match:
                seq_name = match.group(1)
                sequence_defaults.append((table_name, column_name, seq_name))

        return tables, sequences, sequence_defaults
```

- [ ] **Step 5.5: Pre-flight the discovery function against the live rig before wiring it into the bootstrap**

`discover_schema_surface` is not unit-tested (it's DB-introspection, not pure logic). The regex that parses `column_default` is the most fragile part. Before wiring it into `clone_schema_with_sequences` — where a bug would eat a full rig cycle to diagnose — eyeball its output directly.

Run (from the host, with the rig up):

```bash
docker compose exec -T synapse python -c "
import psycopg2
import sys
sys.path.insert(0, '/src')
from scripts.create_tenant_schema import discover_schema_surface

conn = psycopg2.connect(
    host='postgres', port=5432,
    user='synapse', password='synapse', database='synapse',
)
tables, sequences, defaults = discover_schema_surface(conn)
print(f'Tables discovered: {len(tables)}')
print(f'Sequences discovered: {len(sequences)}')
print(f'Sequence/column bindings: {len(defaults)}')
print()
print('First 20 sequence bindings:')
for t, c, s in defaults[:20]:
    print(f'  {t}.{c} -> {s}')
print()
print('Sequences not bound to any column (should be empty or short):')
bound = {s for _, _, s in defaults}
unbound = [s for s in sequences if s not in bound]
for s in unbound:
    print(f'  {s}')
"
```

Expected (rough shape):
- `Tables: ~170` (Synapse has ~168 tables in the core schema)
- `Sequences: ~18-22`
- `Sequence/column bindings: ~10-15` (most sequences are used by a column default, a few are standalone)
- Recognisable bindings like `events.stream_ordering -> events_stream_seq`, `users.user_id -> user_id_seq`, `receipts_linearized.stream_id -> receipts_sequence`.

**Red flags that mean the regex is wrong:**
- Bindings list is empty or much smaller than sequences list (regex not matching any defaults → `column_default` format surprise).
- `sequence_defaults` contains schema-qualified names like `public.events_stream_seq` instead of bare `events_stream_seq` (regex group capture wrong).
- Sequences you expected (`events_stream_seq`, `user_id_seq`) missing from the sequences list entirely (information_schema query wrong).

If any red flag fires, **fix the discovery function before proceeding.** Iteration is cheap because this check is 2 seconds, not a 2-minute rig bounce.

- [ ] **Step 6: Replace `copy_table_structure` with `clone_schema_with_sequences`**

Find the existing `copy_table_structure` function (~line 107) and replace it with:

```python
def clone_schema_with_sequences(
    conn,
    source_schema: str,
    target_schema: str,
) -> None:
    """Clone a schema into a tenant schema with per-tenant sequences.

    This is the correct way to bootstrap a new tenant schema. It
    enumerates tables, sequences, and column/sequence bindings from
    the source schema, then emits a transactional batch of SQL that
    creates per-tenant copies of everything and repoints column
    defaults away from the shared source sequences.

    This REPLACES the old `copy_table_structure`, which used
    `CREATE TABLE ... LIKE ... INCLUDING ALL` but did not copy
    sequences — leaving column defaults pointing at `public.<seq>`
    and effectively making per-tenant stream ordering a fiction.
    """
    tables, sequences, sequence_defaults = discover_schema_surface(
        conn, source_schema
    )
    logger.info(
        "Cloning %d tables, %d sequences, %d sequence/column bindings "
        "from %s to %s",
        len(tables),
        len(sequences),
        len(sequence_defaults),
        source_schema,
        target_schema,
    )

    statements = build_clone_schema_sql(
        target_schema=target_schema,
        tables=tables,
        sequences=sequences,
        sequence_defaults=sequence_defaults,
        source_schema=source_schema,
    )

    with conn.cursor() as cur:
        for stmt in statements:
            try:
                cur.execute(stmt)
            except Exception as e:
                logger.error(
                    "Failed to execute bootstrap SQL: %s -- error: %s",
                    stmt,
                    e,
                )
                raise
```

Also update `create_tenant_schema_from_template` (which called `copy_table_structure`) to call `clone_schema_with_sequences` instead. Keep `copy_table_structure` removed, not deprecated — we do not want two bootstrap paths competing.

- [ ] **Step 7: Make the clone path the default, not `--from-template`**

Find the CLI flag handling near line 320-360 in `main()`. Change the default behavior so that when the config has `multi_tenant.enabled: true`, the script runs `create_tenant_schema_from_template` **without** requiring `--from-template`. The old `initialize_empty_schema` path is only used when `--empty` is explicitly passed (new flag) — this preserves the escape hatch for debugging but makes the safe path the default.

Concretely, in the `main()` argument parser:

```python
    parser.add_argument(
        "--empty",
        action="store_true",
        help=(
            "Create an empty tenant schema without cloning tables or "
            "sequences. DANGEROUS under multi-tenant mode: tables will "
            "fall through to public. For debugging only."
        ),
    )
```

And in the branches that previously called `initialize_empty_schema`, change them to:

```python
            if args.empty:
                initialize_empty_schema(db_params, tenant.database_schema)
            else:
                create_tenant_schema_from_template(
                    db_params,
                    tenant.database_schema,
                    args.template_schema,
                )
```

Remove the `--from-template` flag entirely — it's now the default and the flag has no meaning.

- [ ] **Step 8: Run the unit tests again to confirm nothing regressed**

```bash
trial tests.tenant.test_schema_bootstrap tests.tenant
```

Expected: all tests pass. If any existing `tests/tenant/` test broke because of the function rename, update the test imports.

- [ ] **Step 9: Commit**

```bash
git add scripts/create_tenant_schema.py tests/tenant/test_schema_bootstrap.py
git commit -m "feat(tenant-schema): clone tables with per-tenant sequences, not just LIKE"
```

---

## Task 4: Back-apply the new bootstrap to `docker-multitenant/` and turn the red probes green

**Files:**
- Modify: `docker-multitenant/scripts/init_schemas.py`
- Modify: `docker-multitenant/docker-compose.yml` (possibly)

- [ ] **Step 1: Read the current `init_schemas.py`**

```bash
cat docker-multitenant/scripts/init_schemas.py
```

Locate where it creates tenant schemas. It most likely either shells out to `create_tenant_schema.py` or imports from it directly.

- [ ] **Step 2: Update it to use the new default path**

If `init_schemas.py` calls `initialize_empty_schema` or the `--from-template=False` path, switch it to `create_tenant_schema_from_template`. No new flag needed — after Task 3, this is now the default.

Show the minimal diff, not a rewrite: only change the function call(s).

- [ ] **Step 3: Tear down and bring the rig back up fresh**

```bash
cd docker-multitenant
docker compose down -v
docker compose up -d
```

The `-v` removes the postgres volume, forcing a clean schema bootstrap. Watch the synapse logs for startup errors:

```bash
docker compose logs --tail=200 synapse
```

Expected: no `search_path` resolution errors, no missing-table errors. If anything fails, the new bootstrap is missing a table or sequence the running Synapse expects — investigate before continuing.

- [ ] **Step 4: Run the red probes from Task 2 and confirm they are now green**

```bash
docker compose run --rm test
```

Expected: **both** `test_isolation_same_localpart` and `test_isolation_stream_sequences_independent` print `[PASS]`. If either still fails, read the postgres logs — most likely causes:
- `profiles_user_id_key` still trips → the `profiles` table isn't actually in the tenant schema; check `\dt tenant_acme.*` in psql.
- Stream sequences still interleave → the ALTER DEFAULT step didn't run or the regex in `discover_schema_surface` didn't match the default string; inspect `\d tenant_acme.events` to see where its default points.

- [ ] **Step 5: Commit**

```bash
git add docker-multitenant/scripts/init_schemas.py docker-multitenant/docker-compose.yml
git commit -m "feat(docker-multitenant): bootstrap tenant schemas with per-tenant sequences"
```

---

## Task 5: Narrow the `search_path` fallback and add a startup-time isolation assertion

**Files:**
- Modify: `synapse/storage/database.py:657-716`

The `SET search_path TO {schema}, public` statement at `database.py:691` is what let the original bug hide for so long: any table missing from the tenant schema silently resolved in `public`. Under security-first, this must either fail loud (narrow to `SET search_path TO {schema}` and let missing-table errors surface) or be gated by an explicit allow-list of shared tables. We pick the narrow-and-fail-loud path because we do not currently have any legitimately shared tables identified; if we find some later, we add them to the allow-list explicitly.

- [ ] **Step 1: Read `database.py:657-716` for the current `_set_tenant_schema` and `_restore_search_path` implementation**

Already read during planning — the relevant lines are 691 (`cursor.execute(f"SET search_path TO {schema}, public")`) and 713 (restore).

- [ ] **Step 2: Narrow the `SET search_path` to drop the `, public` fallback**

Change line 691 from:
```python
cursor.execute(f"SET search_path TO {schema}, public")
```
to:
```python
# Security-first policy: do NOT include `, public` as a fallback.
# Any table missing from the tenant schema must fail loud, not
# silently resolve in public. See
# docs/multi_tenant_isolation_model.md for the reasoning.
cursor.execute(f"SET search_path TO {schema}")
```

And update the log line at 693 from `'%s, public'` to just `'%s'`.

- [ ] **Step 3: Add a startup-time isolation assertion helper**

Add a new method on `DatabasePool` alongside `_set_tenant_schema` (~line 716):

```python
def assert_tenant_schema_isolated(
    self,
    conn: "Connection",
    tenant: "TenantConfig",
    expected_tables: list[str],
) -> None:
    """Assert that every expected table resolves inside the tenant schema.

    This is a startup-time check: it runs once per tenant after the
    schema bootstrap, and fails loud if any expected table resolves
    in `public` instead of the tenant schema. It protects against the
    `search_path` fall-through bug that caused every tenant's writes
    to pile into `public.<table>` despite `SET search_path` appearing
    to succeed.

    Args:
        conn: An open connection to run the check on.
        tenant: The tenant whose schema is being asserted.
        expected_tables: The list of tables that MUST exist in the
            tenant schema. At minimum this should include every table
            in the Mechanism B load-bearing set (see
            docs/multi_tenant_isolation_model.md).

    Raises:
        RuntimeError: if any expected table is missing from the
            tenant schema.
    """
    if not isinstance(self.engine, PostgresEngine):
        return

    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT table_name
              FROM information_schema.tables
             WHERE table_schema = %s
               AND table_type = 'BASE TABLE'
            """,
            (tenant.database_schema,),
        )
        present = {row[0] for row in cursor.fetchall()}
        missing = [t for t in expected_tables if t not in present]
        if missing:
            raise RuntimeError(
                f"Tenant {tenant.server_name!r} schema "
                f"{tenant.database_schema!r} is missing expected tables: "
                f"{missing}. This means the schema bootstrap did not run "
                f"or was incomplete. The process will not start — see "
                f"docs/multi_tenant_isolation_model.md."
            )
    finally:
        cursor.close()
```

- [ ] **Step 4: Wire the assertion into Synapse startup**

**Seam (pre-located during plan reconnaissance):** `synapse/app/homeserver.py:453` already contains a `for db in hs.get_datastores().databases:` loop during the setup phase. The isolation assertion goes right after that loop completes, and before `hs.start_listening()` (elsewhere in the same function). Read the function containing line 453 first to confirm the ordering.

**Key design decision: do not hard-code `expected_tables`.** The hard-coded list `["profiles", "presence", "users"]` would drift the moment Synapse adds a new table to `full_schemas`. Instead, derive the expected list from the same source the bootstrap uses — `discover_schema_surface(public)` — so the assertion expectation matches the bootstrap's own view of the world. Single source of truth, no drift.

Add this block right after the existing `for db in hs.get_datastores().databases:` loop in `synapse/app/homeserver.py`:

```python
# Security-first: verify every tenant schema actually holds the
# tables the bootstrap thinks it cloned. If the operator forgot
# to run scripts/create_tenant_schema.py, or if the bootstrap
# raced with startup and partially failed, fail loud here rather
# than silently resolving every query against `public`.
# See docs/multi_tenant_isolation_model.md for rationale.
if hs.config.tenants.multi_tenant.enabled:
    from scripts.create_tenant_schema import discover_schema_surface

    # Open a raw connection to read the expected-tables list from public,
    # then re-use it for each tenant's assertion.
    raw_conn = hs.get_datastores().main.db_pool._db_pool.connectionFactory(
        hs.get_datastores().main.db_pool._db_pool
    )
    try:
        expected_tables, _, _ = discover_schema_surface(raw_conn, "public")
    finally:
        raw_conn.close()

    for tenant in hs.config.tenants.multi_tenant.tenants.values():
        raw_conn = hs.get_datastores().main.db_pool._db_pool.connectionFactory(
            hs.get_datastores().main.db_pool._db_pool
        )
        try:
            hs.get_datastores().main.db_pool.assert_tenant_schema_isolated(
                raw_conn, tenant, expected_tables
            )
            logger.info(
                "Tenant %s schema %s isolation check passed (%d tables)",
                tenant.server_name,
                tenant.database_schema,
                len(expected_tables),
            )
        finally:
            raw_conn.close()
```

**Note:** Twisted's `adbapi.ConnectionPool` exposes raw connections via `connectionFactory`, but the API varies by Twisted version. If the call above fails, the fallback is to use `hs.get_datastores().main.db_pool.runWithConnection(lambda conn: ...)` in a sync helper that schedules the assertion into the reactor. Read `synapse/storage/database.py` lines 1140-1220 (`runWithConnection`) to see the pattern — it's the same path `_set_tenant_schema` uses.

The assertion must run **before** `hs.start_listening()` so the HTTP listener never binds on a broken tenant setup. Find `start_listening` in the same file (also near line 270 based on the grep) and confirm the assertion block is above the `start_listening` call in the setup flow.

- [ ] **Step 5: Restart the rig and verify startup succeeds**

```bash
cd docker-multitenant
docker compose restart synapse
docker compose logs --tail=100 synapse | grep -i "tenant"
```

Expected: no `RuntimeError` from the new assertion, no `search_path` resolution errors.

- [ ] **Step 6: Sanity-check fail-loud by temporarily breaking a tenant schema**

```bash
docker compose exec postgres psql -U synapse -c "DROP TABLE tenant_acme_localhost.profiles"
docker compose restart synapse
docker compose logs --tail=30 synapse
```

Expected: synapse fails to start with a RuntimeError naming `profiles` as missing from `tenant_acme_localhost`. This confirms the assertion is wired correctly.

Then restore:
```bash
docker compose down -v
docker compose up -d
```

- [ ] **Step 7: Run the probes again to confirm nothing regressed**

```bash
docker compose run --rm test
```

Expected: all Phase 1, Phase 1B, and Phase 2 probes pass.

- [ ] **Step 8: Commit**

```bash
git add synapse/storage/database.py synapse/app/homeserver.py
git commit -m "feat(tenant-isolation): narrow search_path and fail loud on missing tenant tables"
```

---

## Task 6: Re-run the Phase 2 probe suite against real isolation

**Files:** none modified (verification only).

The Phase 2 probes were green against the fall-through state. Now that isolation is real, any probe that was passing only because of fall-through will go red. This task catches those regressions before they're hidden by the next commit.

- [ ] **Step 1: Bring up a fresh rig**

```bash
cd docker-multitenant
docker compose down -v
docker compose up -d
# wait for synapse health check
sleep 15
```

- [ ] **Step 2: Run the full probe suite**

```bash
docker compose run --rm test 2>&1 | tee phase2-rebaseline.log
```

- [ ] **Step 3: Read the output and classify any failures**

Scan `phase2-rebaseline.log` for `[FAIL]`. For each failure:
- If it's a Phase 1B probe, something is wrong with the bootstrap — go back to Task 4.
- If it's a Phase 1 or Phase 2 probe that previously passed, it was relying on fall-through. Document the failure mode in a comment on the commit and fix inline if it's small; if it's a larger fix, stop and surface to the user before continuing.

- [ ] **Step 4: Also run the unit test suite**

```bash
trial tests.tenant
```

Expected: all 28+ unit tests pass. If any fail, they were also relying on fall-through at the storage layer — fix before continuing.

- [ ] **Step 5: Delete the log file and commit a no-op marker if everything is green**

If everything is green, there's nothing to commit (this was a verification task). Move on to Task 7.

If something needed a fix, commit with a clear message:
```bash
git add <fixed files>
git commit -m "fix(tenant-<handler>): <describe what was relying on fall-through>"
```

---

## Task 7a: Extend `TenantConfig` with `identity_server`

**Files:**
- Modify: `synapse/config/tenants.py`
- Modify: `synapse/rest/well_known.py`
- Create or modify: `tests/tenant/test_config.py`

This task is independent of the rest of Task 7 and can run in parallel with Tasks 1 and 2 during Wave 1 of the fan-out. `TenantConfig.public_baseurl` already exists (verified during planning); this adds the `identity_server` sibling.

- [ ] **Step 1: Read existing `TenantConfig` structure**

Already read during planning. `synapse/config/tenants.py:34-126` defines the dataclass; `public_baseurl` is at line 67, `effective_public_baseurl` at line 69, `from_dict` at line 81. Mirror those patterns exactly.

- [ ] **Step 2: Add `identity_server` field**

Insert immediately after the `public_baseurl` field (around line 67):

```python
    # Identity server URL announced in /.well-known/matrix/client under
    # the `m.identity_server` key. If unset, the tenant inherits whatever
    # the global config has (which may also be unset, meaning no
    # identity server is announced).
    identity_server: str | None = None
```

Add the accessor property after `effective_public_baseurl`:

```python
    @property
    def effective_identity_server(self) -> str | None:
        """Return `identity_server` if set, otherwise None.

        Unlike `public_baseurl`, there is no default — the identity
        server is genuinely optional and the wire format omits the
        `m.identity_server` key entirely when unset.
        """
        return self.identity_server
```

Add to `from_dict` (around line 125, matching `public_baseurl=config.get("public_baseurl")`):

```python
            identity_server=config.get("identity_server"),
```

- [ ] **Step 3: Update `well_known.py` to route through the tenant**

The current code at `synapse/rest/well_known.py:63-66`:

```python
if self._config.registration.default_identity_server:
    result["m.identity_server"] = {
        "base_url": self._config.registration.default_identity_server
    }
```

Replace with:

```python
# Multi-tenant: prefer the tenant's own identity_server if bound.
# Falls back to the global registration default outside request
# context.
identity_server = None
if tenant is not None:
    identity_server = tenant.effective_identity_server
if identity_server is None:
    identity_server = self._config.registration.default_identity_server

if identity_server:
    result["m.identity_server"] = {"base_url": identity_server}
```

Note: `tenant` is already in scope from the earlier `tenant = get_current_tenant()` call on line 55.

- [ ] **Step 4: Add a unit test**

Read `tests/tenant/test_registry.py` or `tests/tenant/test_context.py` to match the Trial style.

Create or extend `tests/tenant/test_config.py`:

```python
#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2026 Linagora
#

from twisted.trial import unittest

from synapse.config.tenants import TenantConfig


class TenantConfigIdentityServerTestCase(unittest.TestCase):
    def test_identity_server_field_round_trip(self):
        cfg = TenantConfig.from_dict({
            "server_name": "acme.com",
            "signing_key_path": "/tmp/acme.key",
            "identity_server": "https://matrix.org",
        })
        self.assertEqual(cfg.identity_server, "https://matrix.org")
        self.assertEqual(cfg.effective_identity_server, "https://matrix.org")

    def test_identity_server_absent_returns_none(self):
        cfg = TenantConfig.from_dict({
            "server_name": "acme.com",
            "signing_key_path": "/tmp/acme.key",
        })
        self.assertIsNone(cfg.identity_server)
        self.assertIsNone(cfg.effective_identity_server)
```

- [ ] **Step 5: Run the test**

```bash
trial tests.tenant.test_config
```

Expected: both tests pass.

- [ ] **Step 6: Commit**

```bash
git add synapse/config/tenants.py synapse/rest/well_known.py tests/tenant/test_config.py
git commit -m "feat(tenant-config): add identity_server to TenantConfig and thread through well-known"
```

---

## Task 7b: Route request-path `public_baseurl` readers through the tenant

**Files:**
- Modify: `synapse/push/mailer.py` (4 sites: lines 158, 193, 248, 951)
- Modify: `synapse/rest/client/auth.py` (2 sites: lines 111, 179)

These are the easy sites: each is inside a request handler or a per-invocation method, so `get_current_tenant()` returns a bound tenant at the exact moment the read happens. Pattern:

```python
from synapse.tenant_context import get_current_tenant  # at top of file, if not present

# ... inside the handler / per-call method ...
tenant = get_current_tenant()
base_url = (
    tenant.effective_public_baseurl
    if tenant is not None
    else self.hs.config.server.public_baseurl
)
# then use `base_url` in place of the old `self.hs.config.server.public_baseurl`
```

**Caller context verification:** `mailer.py` is invoked from (a) password-reset / registration request handlers (request-path, tenant bound) and (b) `send_renewals` from `handlers/account_validity.py` which is a Phase 2-converted per-tenant background process (tenant bound via `run_as_background_process_per_tenant`). Both paths bind context. Proceed.

- [ ] **Step 1: Read `synapse/rest/well_known.py:41-61` as the reference pattern**

Already read during planning. Lines 41-59 show the exact shape to apply.

- [ ] **Step 2: Fix `synapse/push/mailer.py` — site 1 (line 158)**

Read `synapse/push/mailer.py:150-200` to see context. The current line is:
```python
self.hs.config.server.public_baseurl
```

Before the method that contains line 158, add at the top:
```python
from synapse.tenant_context import get_current_tenant
```
(only if not already imported — check first).

Replace each read with:
```python
tenant = get_current_tenant()
base_url = (
    tenant.effective_public_baseurl
    if tenant is not None
    else self.hs.config.server.public_baseurl
)
# then use `base_url` in place of the old `self.hs.config.server.public_baseurl`
```

**Caller context check:** `mailer.py` is invoked from `send_renewals` (account_validity) and from password-reset / registration flows. `send_renewals` is a Phase 2-converted background process (`handlers/account_validity.py`), which means it runs under `run_as_background_process_per_tenant` and has a tenant context bound. Password-reset and registration are request-path. Both have context bound. Proceed.

- [ ] **Step 3: Fix `synapse/push/mailer.py` — sites 2, 3, 4 (lines 193, 248, 951)**

Apply the same pattern to each of lines 193, 248, 951. If the function already stored `base_url` in a local, reuse it; otherwise compute freshly. Do not add a helper function — three lines inlined is clearer than a two-line helper with a misleading name, and repetition is fine at 4 sites.

- [ ] **Step 4: Run unit tests for mailer**

```bash
trial tests.push
```

If there are no `tests.push` or they don't cover this path, skip. The docker probe from Task 8 step 2 will catch it.

- [ ] **Step 5: Fix `synapse/rest/client/auth.py` (lines 111, 179)**

Same pattern. Read lines 100-185 first for context. Apply the `get_current_tenant()` substitution to both sites.

- [ ] **Step 6: Run mailer + auth tests**

```bash
trial tests.push tests.rest.client.auth
```

Expected: all pass. If tests were mocking `hs.config.server.public_baseurl` without setting a tenant context, they'll need to be updated to either (a) also mock `get_current_tenant()` to return `None` (fall-back path exercised) or (b) wrap in `tenant_context(...)`.

- [ ] **Step 7: Commit**

```bash
git add synapse/push/mailer.py synapse/rest/client/auth.py
git commit -m "feat(tenant): route mailer and auth public_baseurl reads through current tenant"
```

---

## Task 7c: Refactor init-time `public_baseurl` captures to per-request resolution

**Files:**
- Modify: `synapse/rest/client/login.py:658` (SSO)
- Modify: `synapse/rest/synapse/client/pick_idp.py:53`
- Modify: `synapse/util/templates.py` (lines 73, 84-118)
- Modify: `synapse/api/urls.py` (lines 50, 66, 74, 92)

These sites capture `public_baseurl` in `__init__` (or at template-environment-build time). That means `get_current_tenant()` would return `None` at the moment of capture — no tenant bound yet during construction. The fix is structural: move the resolution into the per-request method (or per-filter-invocation closure).

Risk-isolated from 7a and 7b because the changes are refactorings, not substitutions. Each site gets its own mini-plan below.

### 7c.1 — `synapse/rest/client/login.py:658` (SSO base URL)

- [ ] **Step 1: Read `synapse/rest/client/login.py:640-720` to understand how `self._public_baseurl` is used downstream**

The field is captured at line 658 and raises at line 661-662 if absent. Find every use of `self._public_baseurl` in the class (`rg "self\._public_baseurl" synapse/rest/client/login.py`) — that's the set of sites that need per-request resolution.

- [ ] **Step 2: Remove the init-time capture and add a per-request resolver method**

In `__init__` (around line 658), delete:
```python
self._public_baseurl = hs.config.server.public_baseurl

if not self._public_baseurl:
    raise SynapseError(400, "SSO requires a valid public_baseurl")
```

Keep a reference to `hs` (likely already present as `self.hs`). Add a method:

```python
def _get_public_baseurl(self) -> str:
    """Resolve the tenant-aware public_baseurl for the current request.

    This used to be captured at __init__ time, but under multi-tenant
    mode that would return whichever tenant was bound at process
    startup (i.e. none). Now resolved per-request from
    get_current_tenant() with a fall-back to the global config.

    SECURITY: getting this wrong lands the user on the wrong tenant's
    SSO callback, which is an account-takeover vector. Always resolve
    per-request.
    """
    from synapse.tenant_context import get_current_tenant
    tenant = get_current_tenant()
    if tenant is not None:
        return tenant.effective_public_baseurl
    baseurl = self.hs.config.server.public_baseurl
    if not baseurl:
        raise SynapseError(400, "SSO requires a valid public_baseurl")
    return baseurl
```

- [ ] **Step 3: Replace every use of `self._public_baseurl` with `self._get_public_baseurl()`**

In the handler body(ies), change `self._public_baseurl.encode("utf-8")` → `self._get_public_baseurl().encode("utf-8")`, etc.

- [ ] **Step 4: Run the login tests**

```bash
trial tests.rest.client.test_login
```

Expected: pass. If SSO tests mock the SSO path, they may now need a tenant context — read any failing test and fix.

### 7c.2 — `synapse/rest/synapse/client/pick_idp.py:53`

- [ ] **Step 1: Read the file**

Read `synapse/rest/synapse/client/pick_idp.py` in full — it's short.

- [ ] **Step 2: Apply the same init→per-request refactor as 7c.1**

Delete the `self._public_baseurl = hs.config.server.public_baseurl` line in `__init__`. Add a `_get_public_baseurl` helper (copy the body from 7c.1 step 2 — do not import from login.py, repeat the code so the file is self-contained). Replace `self._public_baseurl` with `self._get_public_baseurl()` at every use site.

- [ ] **Step 3: Test**

```bash
trial tests.rest.synapse.client.test_pick_idp
```

If that test path doesn't exist, run `trial tests.rest.synapse` and accept that direct unit coverage is thin — the docker probe suite is the integration gate.

### 7c.3 — `synapse/util/templates.py` (mxc_to_http filter)

This one is structurally different: `mxc_to_http` is a Jinja filter, and Jinja filters are captured at environment-build time. The fix is to make the filter closure look up `get_current_tenant()` on each invocation.

- [ ] **Step 1: Read `_create_mxc_to_http_filter` (verified during plan recon, reproduced here)**

The current function at `synapse/util/templates.py:84-118`:

```python
def _create_mxc_to_http_filter(
    public_baseurl: str | None,
) -> Callable[[str, int, int, str], str]:
    def mxc_to_http_filter(
        value: str, width: int, height: int, resize_method: str = "crop"
    ) -> str:
        if not public_baseurl:
            raise RuntimeError(
                "public_baseurl must be set in the homeserver config to convert MXC URLs to HTTP URLs."
            )
        if value[0:6] != "mxc://":
            return ""
        server_and_media_id = value[6:]
        fragment = None
        if "#" in server_and_media_id:
            server_and_media_id, fragment = server_and_media_id.split("#", 1)
            fragment = "#" + fragment
        params = {"width": width, "height": height, "method": resize_method}
        return "%s_matrix/media/v1/thumbnail/%s?%s%s" % (
            public_baseurl,
            server_and_media_id,
            urllib.parse.urlencode(params),
            fragment or "",
        )
    return mxc_to_http_filter
```

And it's invoked from line 73 as:
```python
"mxc_to_http": _create_mxc_to_http_filter(config.server.public_baseurl),
```

- [ ] **Step 2: Replace with a tenant-aware version**

Change the function signature to take the full `config` object instead of a pre-resolved string, and resolve per invocation:

```python
def _create_mxc_to_http_filter(
    config: "HomeServerConfig",
) -> Callable[[str, int, int, str], str]:
    """Create a jinja2 filter that converts mxc: uris to http URIs.

    Multi-tenant aware: resolves `public_baseurl` from the active
    tenant context on each filter invocation, falling back to the
    global config when no tenant is bound. This matters because
    templates are rendered from inside request handlers (password
    reset email, registration email), where the tenant context IS
    bound — capturing `public_baseurl` at environment-build time
    would freeze in whatever tenant (or none) was bound at process
    startup.
    """
    from synapse.tenant_context import get_current_tenant

    global_baseurl = config.server.public_baseurl

    def mxc_to_http_filter(
        value: str, width: int, height: int, resize_method: str = "crop"
    ) -> str:
        tenant = get_current_tenant()
        public_baseurl = (
            tenant.effective_public_baseurl
            if tenant is not None
            else global_baseurl
        )
        if not public_baseurl:
            raise RuntimeError(
                "public_baseurl must be set in the homeserver config "
                "(or on the active tenant) to convert MXC URLs to HTTP URLs."
            )
        if value[0:6] != "mxc://":
            return ""
        server_and_media_id = value[6:]
        fragment = None
        if "#" in server_and_media_id:
            server_and_media_id, fragment = server_and_media_id.split("#", 1)
            fragment = "#" + fragment
        params = {"width": width, "height": height, "method": resize_method}
        return "%s_matrix/media/v1/thumbnail/%s?%s%s" % (
            public_baseurl,
            server_and_media_id,
            urllib.parse.urlencode(params),
            fragment or "",
        )

    return mxc_to_http_filter
```

And update the call site on line 73:
```python
"mxc_to_http": _create_mxc_to_http_filter(config),
```

- [ ] **Step 3: Test**

```bash
trial tests.util.test_templates
```

If the file doesn't exist, templates are exercised via integration tests — skip and rely on the docker probe suite.

### 7c.4 — `synapse/api/urls.py` (4 sites)

- [ ] **Step 1: Read the file**

Read `synapse/api/urls.py` in full to understand which classes capture `public_baseurl` and where each captured value is used.

- [ ] **Step 2: Apply the same init→per-request pattern**

For each class with a `self._public_baseurl = hs_config.server.public_baseurl` line (lines 50 and 74), remove the capture and add a `_get_public_baseurl` helper (copy body from 7c.1 step 2). Replace every use of `self._public_baseurl` with `self._get_public_baseurl()`.

Note: `api/urls.py` likely constructs URL strings. Make sure the `_get_public_baseurl` helper is called inside the URL-building method, not at class instantiation.

- [ ] **Step 3: Test**

```bash
trial tests.api tests.rest
```

- [ ] **Commit 7c — single commit for the whole init-capture refactor**

```bash
git add synapse/rest/client/login.py synapse/rest/synapse/client/pick_idp.py \
        synapse/util/templates.py synapse/api/urls.py
git commit -m "refactor(tenant): resolve public_baseurl per-request in SSO, templates, and url builders"
```

---

## Task 8: Add the remaining leak probes (registration response, password-reset link, federation version)

**Files:**
- Modify: `docker-multitenant/scripts/test_tenants.py`

- [ ] **Step 1: Add the registration-response probe**

Append to the `PHASE 1 LEAK PROBES` block:

```python
def test_registration_response_qualifies_user_id():
    """Register a user and assert the response's user_id is qualified
    with the tenant's server_name, not the global hostname."""
    tenant = TENANTS[0]
    localpart = f"reg_probe_{int(time.time())}"
    ok = _register_shared_secret(tenant, localpart)
    if not ok:
        print(f"    [FAIL] registration_response - registration failed")
        return False

    # Log in and inspect the response.
    result = make_request(
        "POST",
        "/_matrix/client/v3/login",
        tenant,
        data={
            "type": "m.login.password",
            "identifier": {"type": "m.id.user", "user": localpart},
            "password": "isolation_probe_password",
        },
    )
    if result.get("status") != 200:
        print(f"    [FAIL] registration_response - login failed")
        return False

    user_id = result["data"].get("user_id", "")
    expected_suffix = f":{tenant}"
    if user_id.endswith(expected_suffix):
        print(f"    [PASS] registration_response - user_id={user_id}")
        return True
    else:
        print(f"    [FAIL] registration_response - user_id={user_id} "
              f"(expected to end with {expected_suffix})")
        return False
```

- [ ] **Step 2: Add the password-reset email link probe**

```python
def test_password_reset_link_uses_tenant_baseurl():
    """Trigger a password-reset flow and assert the email link contains
    the tenant's public_baseurl, not the global one.

    Implementation note: requires an SMTP sink. The docker-multitenant
    rig currently has no SMTP container. If one is not configured, this
    probe SKIPS rather than FAILS — but the skip is reported so we
    don't forget it."""
    # Check whether the test rig has an SMTP sink configured. If not,
    # skip with a visible marker.
    smtp_sink_url = os.environ.get("SMTP_SINK_URL")
    if not smtp_sink_url:
        print(f"    [SKIP] password_reset_link - no SMTP_SINK_URL in env")
        return True  # skip counts as pass for aggregate pass/fail

    # TODO(task 8): once an SMTP sink is added, this probe triggers
    # /_matrix/client/v3/account/password/email/requestToken with a
    # tenant Host header, fetches the captured email from the sink,
    # and asserts the reset link substring starts with
    # https://<tenant>/. Until then, the SKIP above is the correct
    # state.
    return True
```

**Note:** the SMTP-sink plumbing is out of scope for this plan — the probe self-reports SKIP so we don't pretend coverage we don't have. A follow-up plan can add a mailhog container to the rig and flesh out the probe body.

- [ ] **Step 3: Add the federation version probe**

```python
def test_federation_version_carries_tenant_server_name():
    """Assert /_matrix/federation/v1/version responds with a 200 and
    that the per-request LoggingContext carries the tenant's
    server_name. We read the latter from the synapse access log."""
    tenant = TENANTS[0]
    result = make_request(
        "GET",
        "/_matrix/federation/v1/version",
        tenant,
    )
    if result.get("status") != 200:
        print(f"    [FAIL] federation_version - status={result.get('status')}")
        return False

    # Read the access log for a line matching this request with the
    # correct server_name label.
    try:
        with open(SYNAPSE_LOG_PATH, "r") as f:
            lines = f.readlines()
    except FileNotFoundError:
        print(f"    [FAIL] federation_version - log file not found")
        return False

    for line in reversed(lines[-200:]):
        if "federation/v1/version" in line and f"server_name={tenant}" in line:
            print(f"    [PASS] federation_version - log carries "
                  f"server_name={tenant}")
            return True

    print(f"    [FAIL] federation_version - no log line found with "
          f"server_name={tenant}")
    return False
```

- [ ] **Step 4: Wire the three new probes into `main()`**

Add to the `PHASE 1 LEAK PROBES` section of the runner:

```python
    all_passed &= test_registration_response_qualifies_user_id()
    all_passed &= test_password_reset_link_uses_tenant_baseurl()
    all_passed &= test_federation_version_carries_tenant_server_name()
```

- [ ] **Step 5: Run the probes and confirm green**

```bash
cd docker-multitenant
docker compose run --rm test
```

Expected: registration_response and federation_version green, password_reset_link skipped with visible `[SKIP]`.

- [ ] **Step 6: Commit**

```bash
git add docker-multitenant/scripts/test_tenants.py
git commit -m "test(docker-multitenant): add registration, pw-reset, federation leak probes"
```

---

## Task 9: Catalogue and triage remaining `self.hs.hostname` / `self.server_name` hits

**Files:**
- Create: `docs/multi_tenant_hostname_audit.md` (ephemeral — may be deleted once empty)
- Modify: any request-path files with latent leaks

The goal here is *not* to fix every latent leak. It's to produce a catalogue with a per-hit disposition, and fix the request-path subset. Non-request-path hits get a documented deferral.

- [ ] **Step 1: Run the grep and capture raw results**

```bash
rg -n "self\.hs\.hostname|self\.server_name" \
    synapse/ \
    --type py \
    > /tmp/hostname-audit-raw.txt
wc -l /tmp/hostname-audit-raw.txt
```

Expected: dozens of hits. Many are harmless constructor assignments (`self.server_name = hs.hostname` in storage data sources — storage is schema-isolated, so schema isolation covers it). The interesting ones are request-path string constructions.

- [ ] **Step 2: Read the raw file and classify each hit**

For each line in `/tmp/hostname-audit-raw.txt`, classify as:
- **R (request-path)** — line is in a request handler that produces user-facing strings (login response, user_id qualification, URL building). **Must fix.**
- **S (storage)** — line is in a storage class constructor that uses `hs.hostname` for column values or schema names. **Covered by schema isolation** (assuming Task 3-5 landed). Leave with a comment pointing at the isolation model doc.
- **B (background)** — line is in a background process. **Must verify** the process runs under `run_as_background_process_per_tenant`. If yes, it will see the correct effective_server_name via `get_current_tenant()`. If no, add to "remaining phase 2 work" in the commit message.
- **T (test)** — line is in a test file. **Ignore.**

- [ ] **Step 3: Create `docs/multi_tenant_hostname_audit.md` with the classified list**

```markdown
# `hs.hostname` / `self.server_name` audit

Enumerates every read of `hs.hostname` or `self.server_name` outside
the files already converted by phases 1 and 2. Each hit has a
disposition: R (request-path, must fix), S (storage, schema-isolated),
B (background, needs per-tenant fan-out), T (test, ignore).

## Request-path hits (R) — fix in this task

[list with file:line and a one-line note]

## Storage hits (S) — covered by schema isolation

[list with file:line — no fix, but cite isolation model]

## Background-process hits (B) — deferred to phase 2 close

[list with file:line — these roll into the phase 2 close plan]

## Test hits (T) — ignored

[count only]

Generated on 2026-04-08 as part of phase 1 close.
```

- [ ] **Step 4: Fix every R-classified hit**

For each, apply the same pattern used in `synapse/rest/client/login.py` and `synapse/handlers/auth.py` during the earlier phase 1 login-leak fix: replace `self.hs.hostname` with `self.hs.effective_server_name()`.

Run `trial tests.rest tests.handlers` after each file fix to catch regressions.

- [ ] **Step 5: Run the full test suite**

```bash
trial tests.tenant tests.rest tests.handlers
```

Expected: all pass.

- [ ] **Step 6: Commit the audit doc and any R-hit fixes**

```bash
git add docs/multi_tenant_hostname_audit.md synapse/...
git commit -m "fix(tenant): route request-path hs.hostname reads through effective_server_name()"
```

If no R-hits were found, still commit the audit doc:

```bash
git add docs/multi_tenant_hostname_audit.md
git commit -m "docs(tenant): audit remaining hs.hostname reads (all storage or test)"
```

---

## Task 10: Close phase 1 via `/sync-roadmap`

**Files:** `roadmap-progess.md`, `multi-tenancy-workflow/data.js` (via skill)

- [ ] **Step 1: Run the skill**

In the session, invoke:
```
/sync-roadmap
```

- [ ] **Step 2: Verify the skill bumped the phase-1 row to ✅ Complete**

Read the resulting `roadmap-progess.md` exec-summary table and confirm:

| Roadmap phase | Status |
|---|---|
| 1. Audit & instrumentation | ✅ Complete |

- [ ] **Step 3: Verify the cross-cutting items section was updated**

Cross-cutting items 1, 5, and 6 (schema-per-tenant fiction, sequence cloning, search_path fallback) should either be moved to "resolved in phase 1 close" or deleted entirely. Cross-cutting item 3 (hs.hostname sweep) should be narrowed to the B-classified hits from Task 9.

- [ ] **Step 4: Verify `multi-tenancy-workflow/data.js` was updated**

The animation's "audit" or "phase 1" stage should mention the schema-bootstrap fix and the new probes. Minor — the skill will handle it.

- [ ] **Step 5: Commit the tracker updates**

The sync skill explicitly does not commit. Commit by hand:

```bash
git add roadmap-progess.md multi-tenancy-workflow/data.js
git commit -m "docs(roadmap): phase 1 closed — schema isolation real, public_baseurl swept"
```

- [ ] **Step 6: Final verification — full probe suite + unit tests**

```bash
cd docker-multitenant
docker compose down -v
docker compose up -d
sleep 15
docker compose run --rm test
cd ..
trial tests.tenant
```

Expected: everything green.

- [ ] **Step 7: Tag the branch at phase-1-closed (optional but recommended)**

```bash
git tag phase-1-closed
```

This gives future phase 2 close work a clean rollback point.

---

## Self-review notes

This plan was self-reviewed after writing. Findings and fixes:

- **Spec coverage:** All items from `roadmap-progess.md` Suggested-next-steps #1 are mapped to tasks. Catalogue → Task 1. Red probes → Task 2. Schema bootstrap → Task 3. Rig back-apply → Task 4. `search_path` policy → Task 5. Phase 2 re-baseline → Task 6. `public_baseurl` + `identity_server` sweep → Task 7 (note: `identity_server` field addition is folded into Task 7 step 5 as part of the SSO work, not a separate task — see consistency note below). New leak probes → Task 8. `hs.hostname` sweep → Task 9. Close → Task 10.

- **Placeholder scan:** Two deliberate TODOs remain and are clearly marked:
  1. Task 2 step 3 stream-sequence probe has a fallback path ("if admin API doesn't expose stream_ordering, use direct SQL") — this is a runtime decision not a planning gap.
  2. Task 8 step 2 password-reset probe self-reports SKIP because no SMTP sink exists in the rig yet — this is explicitly surfaced as follow-up work, not hidden.

- **Type consistency:** `build_clone_schema_sql`, `discover_schema_surface`, `clone_schema_with_sequences`, `assert_tenant_schema_isolated` — all names match across tasks. The `sequence_defaults` tuple shape `(table, column, sequence_name)` is consistent between Task 3's test, generator, and discovery helper.

- **Spec gap found and fixed:** the original roadmap mentions adding `identity_server` to `TenantConfig`, but my initial draft only did `public_baseurl`. Task 7 now needs a sub-step for `identity_server` — **added inline below as Task 7 step 11.5** (retroactively). Actually, re-reading: `identity_server` is already set in `well_known.py:63-66` from `hs.config.registration.default_identity_server`, not from `public_baseurl`. That's a separate config surface. Adding `identity_server` to `TenantConfig` is a real task that was missed. **Fix:** fold it in as a new step at the start of Task 7:

**Task 7, Step 0 (add before existing Step 1): Extend `TenantConfig` with `identity_server`**

- Open `synapse/config/tenants.py`, add a new `identity_server: str | None = None` field at line 67 alongside `public_baseurl`.
- Add `effective_identity_server` property that returns `self.identity_server` or `None` (no default — identity server is optional).
- Add `identity_server=config.get("identity_server")` to `from_dict` at line 125.
- Update `synapse/rest/well_known.py:63-66` to read from `get_current_tenant().effective_identity_server` with global fallback.
- Add a unit test in `tests/tenant/test_config.py` (create if needed) asserting the field round-trips through `from_dict`.
- Commit: `git commit -m "feat(tenant-config): add identity_server to TenantConfig"`

Apply this as Task 7.0 at execution time.

---

## Execution notes

- **Branch:** stay on `feature/multi-tenant`, or create a dedicated worktree via `superpowers:using-git-worktrees` before starting Task 1. The plan is long enough (10 tasks, ~60 steps) that a worktree is worth the isolation.
- **Commit cadence:** one commit per task at minimum, occasionally per sub-step (Task 3 for example has a natural commit after the unit-test-driven generator lands). Frequent commits make Task 6's re-baseline a useful bisect target if a Phase 2 probe goes red.
- **Unit tests:** `trial tests.tenant` must stay green after every task. If it goes red, fix before continuing.
- **Phase 2 close (pushers + presence) is a separate plan** that starts from the `phase-1-closed` tag and has its own brainstorm cycle to decide the pusher fan-out shape. Do not pre-empt it here.
