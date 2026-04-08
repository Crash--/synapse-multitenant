# Phase 2 Close — Tenant-Aware State Reads, Bootstrap Seed Rows, and Background Loops

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task.

> **Date:** 2026-04-08
> **Branch:** `feature/multi-tenant`
> **Companion to:** `docs/multi_tenant_roadmap.md` (spec), `roadmap-progess.md` (audit), `docs/superpowers/plans/2026-04-08-phase-1-close-real-tenant-isolation.md` (template), `docs/multi_tenant_hostname_audit.md` (B-hit punch list).

---

## Context

**What just happened.** Phase 1 close (commits `5cde4e2a6` → `261df8d62`, twelve commits across ten tasks) turned schema-per-tenant isolation from fiction into reality: `clone_schema_with_sequences` clones tables AND creates per-tenant sequences with repointed defaults; `_set_tenant_schema` no longer falls through to `, public`; a startup-time assertion now refuses to bring the listener up against an under-built tenant schema; `effective_server_name()` was swept through the request-path readers (mailer, auth, login, well-known, openid, register, consent, media upload).

**What that surfaced.** Closing phase 1 properly exposed three bugs that the old `, public` fall-through had been masking:

1. **`createRoom` returns 403 on non-primary tenants** because cross-tenant data leaks through the state_groups read path. The error message body literally names a foreign-tenant event/room ID inside the auth check (`found event $A:acme.localhost in the state which is in room !S:acme.localhost` while authing a corp room). Writes are isolated; reads are not. Per-tenant sequences are correctly minting state_group IDs in each schema, but every cache between `runInteraction` and the request is keyed on the bare `state_group: int`, so acme's state_group `42` and corp's state_group `42` collide on the cache hot path.
2. **Six background loops crash on every non-primary tenant** because the cloned schemas are missing **singleton seed rows** that upstream Synapse's `prepare_database` writes into `public` on first boot. These loops correctly fan out per tenant (the helper from phase 2 is binding context fine — `[server_name=corp.localhost]` shows in the logs), but they call `simple_select_one` on tables like `stats_incremental_position` and `user_directory_stream_pos` and get `StoreError: 404 No row found` because `LIKE INCLUDING ALL` copies structure but not data.
3. **Several B-classified hits from `multi_tenant_hostname_audit.md`** that were deferred from phase 1 close still need attention — but the audit's classification of `notifier.py` as B was wrong (all 6 hits are metric labels, no work needed) and the classification of `server_notices_manager.py` was right but understated (5 reads of `self.server_notices_mxid` baked from global config produce **stored-row corruption** — wrong-tenant MXID written into event `sender` fields and used as the room creator).

**Why this needs a plan, not a one-shot fix.** Phase 2 close is too large for a monolithic commit set. Decomposing it into independently-shippable, independently-verifiable sub-phases makes each fix reviewable and lets the rig stay green between landings. It also surfaces a process improvement from phase 1 close: the state_groups leak was a day-9 surprise instead of a day-1 probe, because nobody had written the red probe at the start. Phase 2 close fixes that by making "Task 0 = write the red probe" a standing rule.

---

## Decomposition rationale

**Axis chosen: severity → subsystem (Option A from brainstorming).** `2a` is severity-driven (state_groups is the highest-pain bug — it makes the rig functionally single-tenant); `2b` is the seed-row finding that surfaced during reproduction (a second emergency, but distinct from `2a` so it ships on its own); `2c` is subsystem-grouped (the residual bg-loop and audit work).

**Axes considered and rejected:**

- *Pure subsystem.* Loses the "highest-pain first" property. Hides that state_groups is uniquely load-bearing.
- *Severity then dependency (Option B from brainstorming).* Presupposes a "shared bg-loop foundation" sub-phase that bundles bootstrap and conversions together. Rejected because the bootstrap fix and the loop fan-out are mechanically independent: the bootstrap fix lives in `scripts/create_tenant_schema.py`, the loop fan-out lives in handler files. Bundling them just makes the commit graph harder to bisect.
- *Pure subsystem grouping under a single "phase 2 close" commit set.* Rejected because phase 2 close has at least two unrelated bugs (state_groups read leak, missing seed rows) plus a punch list. Bundling means landing or reverting them together, which violates "each commit describes one thing."

**State_groups root-cause hypotheses surfaced (recorded for the bisect):**

| H | Hypothesis | Evidence for | Evidence against |
|---|---|---|---|
| H0 | Shared `state_group_id_seq` across tenants | — | Ruled out: per-tenant sequences confirmed via `pg_class` query (acme/corp/startup all have their own `state_group_id_seq`) |
| H1 | **`StateGroupDataStore` `DictionaryCache` (`_state_group_cache`, `_state_group_members_cache`) keyed on bare `state_group: int`** — leading suspect | `store.py:121,128` confirmed; the error message shape ("found event in state which is in room :acme.localhost" while authing corp) matches a cache hit returning the wrong tenant's state | None — this is the smoking gun |
| H2 | `_get_state_group_for_event` (`@cached` keyed on `event_id: str`) returning the wrong group | `main/state.py:604` confirmed tenant-blind; feeds bad `state_group` into H1 | Event_ids are SHA-hashed so cross-tenant collision is astronomically unlikely; this is a *propagation path* for H1, not an independent root cause |
| H3 | Separate state DB pool not applying tenant `search_path` | — | Moot: docker rig uses single combined DB (`config/homeserver.yaml` declares one `database:` block); flagged for future probe in multi-DB rigs |
| H4 | `MultiWriterIdGenerator` in-process counter overlap | Holds a process-wide `_current_positions: dict[str, int]` (`id_generators.py:240`) populated at construction | Not used for `state_group_id_seq` (which uses stateless `PostgresSequenceGenerator`); used for `events_stream_seq` so it's an *adjacent* hazard but not the source of the 403 |
| H5 | `event_to_state_groups` mapping cached without tenant key | `_get_state_group_for_event` confirmed; same family as H2 | Same as H2 — propagation, not root |
| H6 | `StateHandler._state_cache` keyed on `frozenset[int]` of state_group IDs | `state/__init__.py:645` confirmed tenant-blind | Hit during state resolution which is downstream of H1 cache reads — likely fixed by H1 fix unless state resolution is reached on a clean cache miss |

**Bisect strategy:** start from H1, add tenant-keying to the two `DictionaryCache` outer keys, re-run the probe. If green, narrow to H2/H5/H6 only as defence-in-depth. If still red, walk the read path with a logger that prints `(state_group, returned_room_id, expected_room_id)` to find which cache returned cross-tenant data.

**Helper decision:** if the bisect surfaces 3+ caches needing the same fix, write a `tenant_keyed_cache_key` helper in this sub-phase. If it surfaces 1–2, fix in place. Pre-committing to a helper before the bisect violates probes-first.

---

## Dependency graph

```
                      ┌─────────────────────────────────────┐
                      │ 2a — state_groups read leak (bisect)│
                      └─────────────────────────────────────┘
                                       │
                                       ▼
                      ┌─────────────────────────────────────┐
                      │ 2b — bootstrap singleton seed rows  │
                      └─────────────────────────────────────┘
                                       │
                                       ▼
                      ┌─────────────────────────────────────┐
                      │ 2c — presence loops + server_notices│
                      └─────────────────────────────────────┘
```

**Strict serial.** `2a → 2b` because: until state_groups reads are correct, you can't tell whether a bg-loop failure is "missing seed row" or "wrong-tenant data leaking through state cache." `2b → 2c` because: presence and server_notices may themselves fail with the same `StoreError: 404 No row found` shape that motivated `2b`, and you don't want to rediscover the same fix twice.

No cross-sub-phase parallelism. *Within* `2c` the presence work and the server_notices work touch disjoint files (`handlers/presence.py` vs `server_notices/server_notices_manager.py`) and can be dispatched as parallel subagents in one message.

---

## Standing rules (baked into every sub-phase)

These are the lessons-learned standing rules from phase 1 close. They are not one-off task notes — they apply to every task in every sub-phase below:

1. **Probes-first, always.** Task 0 of every sub-phase is "write the red probe that proves this is broken." If the probe can't be written, the bug isn't understood yet.
2. **Probe-after-every-task.** Inner loop is `cd docker-multitenant && docker compose restart synapse && docker compose run --rm test`. Don't batch regressions to a checkpoint task at the end of a sub-phase.
3. **Persistent audit dump file** under `docs/` or `/tmp/`, generated once per sub-phase, referenced by every subagent dispatched within that sub-phase. Phase 1 re-grepped the 522-hit hostname catalogue several times because no agent had the prior raw output. Don't repeat that.
4. **Parallel subagent dispatch** for independent work — fire in one message, not in series.
5. **Sonnet 4.6 as default subagent model.** Opus 4.6 only for tasks whose description contains "trace", "bisect", "diagnose", or "audit/classify". Mechanical pattern work doesn't need Opus and Opus 4.6 has been hitting 529 overload regularly.
6. **Commit after every green task,** separately from tracker updates. Each commit describes one thing.
7. **Smaller, scoped subagent prompts.** Reserve the heavy implementer + spec-review + code-review three-agent flow for tasks with real design judgment. Mechanical refactors get one agent with a tight prompt.

---

## Sub-phase 2a — state_groups / event-auth cross-tenant cache leak

### Scope

Fix the read-side leak that makes `POST /createRoom` return 403 on non-primary tenants. In scope: every in-process cache on the `StateGroupDataStore` / `StateHandler` / `StateStorageController` read path that is keyed on a bare `state_group: int`, `event_id: str`, or `frozenset[int]` of state_groups without a tenant component. Out of scope: `MultiWriterIdGenerator` for `events_stream_seq` (adjacent but not the cause), the systemic `@cached` decorator key builder change (defence-in-depth, deferred unless the bisect demands it), the federation-side `state_groups` leak hazard (federation phase).

### Red probes (Task 0)

Two probes, both expected red against current branch:

- **`test_create_room_per_tenant`** — `POST /createRoom` against each of `acme.localhost`, `corp.localhost`, `startup.localhost` in sequence; assert all three return 200 with a room_id whose suffix matches the tenant. The existing probe rig already exercises this and fails on corp/startup; this task lifts it from "skip on failure" to a hard `[FAIL]` and adds startup explicitly. Lives in `docker-multitenant/scripts/test_tenants.py` under `PHASE 2A PROBES`.
- **`test_state_group_cache_isolation`** — direct cache-isolation probe. Uses two requests under different Host headers that both touch the same low-numbered `state_group` ID; asserts the second request returns its own tenant's state, not the cached first tenant's state. May need to live in a Trial unit test (`tests/tenant/test_state_group_cache_isolation.py`) rather than the docker rig because the assertion is on returned `StateMap` contents, not HTTP response shape.

If either probe cannot be written, stop and escalate.

### Files touched (best-effort, will grow)

- `synapse/storage/databases/state/store.py` — primary suspect: `_state_group_cache` (line 121), `_state_group_members_cache` (line 128), `get_state_group_delta` cache (line 150), `_insert_into_cache` (line 439), the `txn.call_after` prefill in `insert_full_state_txn` (lines 703–708)
- `synapse/storage/databases/main/state.py` — `_get_state_group_for_event` / `_get_state_group_for_events` (line 604, 619), `get_partial_current_state_ids` (line 507)
- `synapse/state/__init__.py` — `StateHandler._state_cache` (line 645), `resolve_state_groups` (lines 702–725)
- `synapse/storage/controllers/state.py` — `_get_joined_hosts` (line 817)
- `synapse/util/caches/descriptors.py` — only if the bisect surfaces 3+ tenant-keyed caches and a helper becomes worth it (lines 147, 622–692)
- `docker-multitenant/scripts/test_tenants.py` — new `PHASE 2A PROBES` block
- Possibly `tests/tenant/test_state_group_cache_isolation.py` — new unit test
- `docs/multi_tenant_state_cache_keying.md` — new design doc capturing what was changed and why (parallel to phase 1 close's `multi_tenant_isolation_model.md`)

### Task list

- **Task 0:** Write red probes (above). Confirm both fail. Commit.
- **Task 1:** Audit dump — re-run the cache inventory from the planning agent and persist as `/tmp/state_group_cache_inventory.md` so subsequent tasks reference it without re-grepping. Not committed; lives for the duration of the sub-phase. Standing rule #3.
- **Task 2:** Bisect H1 — add tenant-keying to `_state_group_cache` and `_state_group_members_cache` outer keys (or replace `DictionaryCache[int, ...]` with `DictionaryCache[tuple[str, int], ...]` keyed on `(effective_server_name, state_group)`). Re-run probes. **Subagent model: Opus** (description contains "bisect").
- **Task 3:** If Task 2 turned the probes green, narrow to H2/H5/H6 as defence-in-depth (one cache at a time; commit each separately). If Task 2 was insufficient, walk the read path with a logger that prints `(state_group, returned_room_id, expected_room_id)` and identify the next cache. **Subagent model: Opus.**
- **Task 4:** If 3+ caches needed the same fix in Tasks 2–3, extract a `tenant_keyed_cache_key(state_group)` helper in `synapse/util/caches/descriptors.py` and refactor the call sites onto it. Otherwise skip. **Subagent model: Sonnet** (mechanical refactor).
- **Task 5:** Add a regression unit test (`tests/tenant/test_state_group_cache_isolation.py`) that exercises the cache directly with two simulated tenants. **Subagent model: Sonnet.**
- **Task 6:** Write `docs/multi_tenant_state_cache_keying.md` capturing what changed, which caches were tenant-keyed, and the bisect order — so future-you doesn't have to rederive it. **Subagent model: Sonnet.**
- **Task 7:** Re-run the full probe suite (`PHASE 1 LEAK PROBES`, `PHASE 2 PROBES`, `PHASE 1B ISOLATION PROBES`, new `PHASE 2A PROBES`) and confirm no regressions outside the known-failing bg loops covered by `2b`. Commit nothing if green.

**Estimated commits:** 6–10.

### Definition of done

- `test_create_room_per_tenant` green for all three tenants.
- `test_state_group_cache_isolation` green.
- No regressions in `PHASE 1 LEAK PROBES`, `PHASE 2 PROBES` (the bg-loop probes that were already failing because of `2b` stay failing — they're tracked under `2b`), or `PHASE 1B ISOLATION PROBES`.
- `docs/multi_tenant_state_cache_keying.md` lands.
- `tests/tenant/test_state_group_cache_isolation.py` lands and passes.
- `roadmap-progess.md` cross-cutting item 7 flips to `[RESOLVED 2026-04-DD in phase 2a close]`. **Run `sync-roadmap` skill after this sub-phase ships** (do not run during this session).

### Risks & deferrals

- **Risk:** the bisect surfaces a cache that lives in upstream Synapse but is shared across `events_stream_seq` and `state_group_id_seq` (e.g., a cross-cutting cache in `caches/dictionary_cache.py`). If so, `2a` may need to also touch the `events_stream_seq` `MultiWriterIdGenerator` adjacent hazard. Decision point: if the bisect lands here, escalate before scope-creeping.
- **Risk:** state resolution v2 (`synapse/state/v2.py`) has its own caches that may need keying; the planning audit didn't enumerate them because they're rarely on the createRoom path. Defer unless probes catch them.
- **Deferred:** the systemic `@cached` decorator change that injects tenant into every cache key. Defence-in-depth, but it's a forty-call-site sweep that's not justified by the failing probe alone.
- **Deferred:** federation-side state_groups isolation. Phase 7.

---

## Sub-phase 2b — Bootstrap singleton seed-row completeness

### Scope

Extend `scripts/create_tenant_schema.py::clone_schema_with_sequences` to copy the eight singleton seed rows from `public.<table>` into each `tenant_<x>.<table>` during bootstrap. In scope: catalogue, generator extension, back-apply to `docker-multitenant/scripts/init_schemas.py`, unit tests for the catalogue, integration probe. Out of scope: any change to the failing handlers themselves (they're correct in expecting a row) and any change to upstream Synapse's `prepare_database` (we ride alongside it, we don't fork it).

### Red probes (Task 0)

- **`test_bg_loops_quiet_on_non_primary_tenant`** — runs the full probe suite and asserts the synapse log under `data/logs/synapse.log` contains zero `StoreError: 404 No row found` lines under `[server_name=corp.localhost]` or `[server_name=startup.localhost]` over a 30 s window after rig startup. Currently red (six bg loops fire on a fresh rig).
- **`test_singleton_seed_rows_present_per_tenant`** — host-side psql probe that asserts each of the eight catalogued tables has the expected row count in every tenant schema. Lives in `docker-multitenant/scripts/run_isolation_probes_host.sh` (the host-side probe pattern phase 1 close already established for the stream-sequence probe).

### Files touched

- `scripts/create_tenant_schema.py` — extend `build_clone_schema_sql` to emit `INSERT INTO {target_schema}.{table} SELECT * FROM {source_schema}.{table}` for each catalogued table; extend `discover_schema_surface` to return the seed-row catalogue (or accept it as a constant)
- `tests/tenant/test_schema_bootstrap.py` — extend the existing pure-function test to cover the seed-row INSERT generation
- `docker-multitenant/scripts/init_schemas.py` — re-runs the (now extended) `clone_schema_with_sequences`; no code change expected
- `docker-multitenant/scripts/test_tenants.py` — new `PHASE 2B SEED-ROW PROBES` block
- `docker-multitenant/scripts/run_isolation_probes_host.sh` — new host-side probe
- `docs/multi_tenant_isolation_model.md` — append a "Singleton seed rows" section to the existing isolation-model doc

### Catalogue (from the planning audit, locked)

Eight tables, all sourced from `synapse/storage/schema/main/full_schemas/72/full.sql.postgres` (lines 1327–1333) and post-72 deltas. All use the `Lock CHAR(1) DEFAULT 'X' UNIQUE CHECK (Lock='X')` idiom except `federation_stream_position` (2 rows keyed on `(type, instance_name)`):

| Table | Source | Row count |
|---|---|---|
| `appservice_stream_position` | `full_schemas/72/full.sql.postgres:1327` | 1 |
| `event_push_summary_last_receipt_stream_id` | `full_schemas/72/full.sql.postgres:1328` | 1 |
| `event_push_summary_stream_ordering` | `full_schemas/72/full.sql.postgres:1329` | 1 |
| `federation_stream_position` | `full_schemas/72/full.sql.postgres:1330–1331` | 2 |
| `stats_incremental_position` | `full_schemas/72/full.sql.postgres:1332` | 1 |
| `user_directory_stream_pos` | `full_schemas/72/full.sql.postgres:1333` | 1 |
| `room_forgetter_stream_pos` | `delta/76/04_add_room_forgetter.sql:41–43` | 1 |
| `delayed_events_stream_pos` | `delta/88/01_add_delayed_events.sql:41–43` | 1 |
| `device_lists_changes_converted_stream_position` | `delta/73/12refactor_device_list_outbound_pokes.sql:59–66` | 1 |

The catalogue lives as a top-level `SINGLETON_SEED_TABLES` constant in `scripts/create_tenant_schema.py`. The bootstrap copies via `INSERT INTO {target}.{table} SELECT * FROM public.{table}` — column-positional, which is robust to nullable columns added in later deltas (e.g., `device_lists_changes_converted_stream_position.instance_name`).

### Task list

- **Task 0:** Write `test_bg_loops_quiet_on_non_primary_tenant` and `test_singleton_seed_rows_present_per_tenant`. Confirm both fail. Commit.
- **Task 1:** Extend `tests/tenant/test_schema_bootstrap.py` with a new test asserting `build_clone_schema_sql` emits the right `INSERT INTO ... SELECT *` statements for the catalogue. Confirm fails.
- **Task 2:** Extend `build_clone_schema_sql` and `clone_schema_with_sequences` to emit and execute the seed-row INSERTs. Confirm Task 1's test passes.
- **Task 3:** Drop and re-create the docker rig's tenant schemas (using the fast-iteration path from phase 1 close: `docker compose exec postgres psql -c "DROP SCHEMA tenant_acme CASCADE;"` then `python -m scripts.create_tenant_schema --all-tenants`). Confirm Task 0's probes turn green.
- **Task 4:** Cold-start the rig (`docker compose down -v && docker compose up -d`). Confirm probes still green from a clean postgres volume. This is the back-apply verification.
- **Task 5:** Append "Singleton seed rows" section to `docs/multi_tenant_isolation_model.md` documenting the catalogue and why each row exists.

**Subagent model:** Sonnet for all tasks. None require trace/bisect/diagnose. Mechanical work.

**Estimated commits:** 4–6.

### Definition of done

- Both Task 0 probes green.
- `tests/tenant/test_schema_bootstrap.py` extended and passes.
- All previously-failing bg loops (`stats.notify_new_event`, `user_directory.notify_new_event`, `delayed_events.notify_new_event`, `room_forgetter.notify_new_event`, `_handle_new_device_update_async`, `rotate_notifs`) stop logging `StoreError: 404` / `min() iterable argument is empty` on a fresh rig.
- `docs/multi_tenant_isolation_model.md` updated.
- **Run `sync-roadmap` after this sub-phase ships.**

### Risks & deferrals

- **Risk:** the catalogue is incomplete because a future Synapse delta adds a ninth seed row. Mitigation: the assertion in `synapse/app/homeserver.py::assert_tenant_schema_isolated` already enumerates expected tables; consider extending it to also assert seed-row presence. Out of scope for `2b` but flag for `2c` or a follow-up.
- **Risk:** `INSERT INTO ... SELECT *` collides with an existing row when re-running `init_schemas.py` against an already-initialized schema. Mitigation: wrap in `ON CONFLICT DO NOTHING` (postgres) — the `Lock='X'` UNIQUE constraint will reject duplicates cleanly.
- **Deferred:** auto-discovery of seed rows by introspecting `public.<table>` row counts at clone time (rather than a static catalogue). Tempting but produces a different bug (operator runs `init_schemas` against a partially-populated `public` and accidentally clones user data into the tenant schema). Static catalogue is the safer shape.

---

## Sub-phase 2c — Presence loop fan-out + server_notices per-tenant MXID

### Scope

Two disjoint pieces of work in this sub-phase, dispatched as parallel subagents:

**(2c-A) Presence loops** — convert the five background loops in `synapse/handlers/presence.py` to fan out per tenant via `run_as_background_process_per_tenant`. Per-tenant `Measure` labels via `effective_server_name()`.

**(2c-B) Server-notices per-tenant MXID** — fix the stored-row corruption bug in `synapse/server_notices/server_notices_manager.py`. The class loads `server_notices_mxid` from `self._config.servernotices.server_notices_mxid` at `__init__` (line 51) and then writes that single MXID into event `sender` fields (`:89`) and uses it as the room creator (`:155`). Under multi-tenant, this means notices sent on `corp.localhost` are stored as if sent by `@notices:acme.localhost`. The fix: resolve the MXID per-request via `get_current_tenant()` and the tenant's own `server_notices_mxid` config — which means the field also has to land on `TenantConfig` (similar to the phase 1 close `identity_server` / `public_baseurl` work).

**Notifier dropped from this sub-phase:** the audit's classification of `synapse/notifier.py` as B was wrong. All 6 hits at lines 290, 301, 308, 384, 611 are metric labels (M); the `__init__` assignment at line 246 is the source. No work needed. Update `docs/multi_tenant_hostname_audit.md` to re-classify them and move on.

### Red probes (Task 0)

- **`test_presence_loop_per_tenant`** — assert `background_process_start_count{name='handle_presence_timeouts'}` is present for all three tenants in Prometheus output. Currently red (loop runs once per process, labelled with primary server_name only).
- **`test_server_notice_sender_per_tenant`** — POST `/_synapse/admin/v1/send_server_notice` with `txn_id` against `corp.localhost`; query the corp tenant's events table for the resulting message; assert the event's `sender` is `@notices:corp.localhost`, NOT `@notices:acme.localhost`. Currently red — `sender` is the primary tenant's MXID.

### Files touched

**(2c-A) Presence:**
- `synapse/handlers/presence.py` — `_handle_timeouts` (line 866 registration, 1054 decorator), `_persist_unpersisted_changes` (line 874, 924), `notify_new_event` / `_unsafe_process` (line 1513–1528), `WorkerPresenceHandler.send_stop_syncing` (line 532), `PresenceEventSource._clear_queue` (line 2474)
- The five loops share state on `self` (`wheel_timer`, `user_to_current_state`, `unpersisted_users_changes`, `_event_pos`, `_event_processing`) — these need to become per-tenant dicts/sets keyed on `effective_server_name`, following the pattern from `synapse/handlers/user_directory.py` and `synapse/handlers/stats.py`
- `Measure(..., server_name=self.server_name)` calls inside loop bodies (line 1535) become `server_name=get_current_tenant().server_name` or equivalent

**(2c-B) Server notices:**
- `synapse/config/tenants.py` — add `server_notices_mxid: str | None = None` field with `effective_server_notices_mxid` accessor (parallel to `identity_server` from phase 1 close)
- `synapse/server_notices/server_notices_manager.py` — replace the `__init__`-time `self.server_notices_mxid` capture with a `_get_server_notices_mxid()` method that resolves per-request via `get_current_tenant().effective_server_notices_mxid`. Update lines 81, 89, 127, 155, 260, 283, 311, 317, 331 to call the resolver instead of reading the captured field.
- `tests/tenant/test_config.py` — extend with a `server_notices_mxid` round-trip test (parallel to the `identity_server` test from phase 1 close)
- `docs/multi_tenant_hostname_audit.md` — re-classify notifier hits from B to M; move server_notices from B to "fixed in 2c"

### Task list

**Wave 1 (sequential setup):**
- **Task 0:** Write both red probes. Confirm both fail. Commit.
- **Task 1:** Audit dump for `2c-A` — list every read of `self` state on the presence handler that needs to become per-tenant. Persist as `/tmp/presence_state_audit.md`. **Subagent model: Opus** (description contains "audit/classify"). Not committed.

**Wave 2 (parallel — single message, two subagents):**
- **Task 2 (2c-A):** Convert all five presence loops to per-tenant fan-out. Per-tenant `_user_to_current_state`, `_event_pos`, `_event_processing` dicts. Per-tenant `Measure` labels. **Subagent model: Sonnet.** Mechanical, follows `user_directory.py` / `stats.py` precedent.
- **Task 3 (2c-B):** Add `server_notices_mxid` to `TenantConfig`, refactor `server_notices_manager.py` to per-request resolution, extend `tests/tenant/test_config.py`. **Subagent model: Sonnet.**

**Wave 3 (sequential cleanup):**
- **Task 4:** Re-run probes. Both must be green. If `2c-A`'s probe is green but `2c-B`'s is red (or vice versa), commit the green half and iterate on the red half.
- **Task 5:** Update `docs/multi_tenant_hostname_audit.md` — re-classify notifier B → M, mark server_notices as fixed, mark presence as fixed. Commit.
- **Task 6:** Re-run the *full* probe suite (phases 1, 1B, 2, 2A, 2B, 2C). Confirm green-everywhere. This is the phase 2 close gate.

**Estimated commits:** 5–7.

### Definition of done

- `test_presence_loop_per_tenant` green for all three tenants.
- `test_server_notice_sender_per_tenant` green for all three tenants.
- Full probe suite (phases 1, 1B, 2, 2A, 2B, 2C) green end-to-end on the docker rig.
- `tests/tenant/test_config.py` extended and passes.
- `docs/multi_tenant_hostname_audit.md` updated with the re-classifications and fixes.
- **Run `sync-roadmap` after this sub-phase ships.** This is also the sub-phase that flips the executive-summary row in `roadmap-progess.md` from 🟢 back to ✅ for phase 2.

### Risks & deferrals

- **Risk:** server_notices is bigger than the brainstorm anticipated — it's a *real* stored-row corruption bug, not a label leak. If `2c-B` turns out to need a deeper rework (e.g., the singleton manager pattern itself doesn't survive multi-tenancy and needs to become per-tenant in `HomeServer`), escalate and split into its own sub-phase `2d`. The current task budget assumes a config + per-request resolution fix.
- **Risk:** presence has a `WorkerPresenceHandler` variant (line 532) that runs in worker mode. Workers are phase 10. The base `PresenceEventHandler` is the main-process variant and is what the docker rig exercises. Decision: convert both, but the worker variant is untested and stays on the "phase 10 will re-verify" honour system.
- **Deferred:** any presence work that requires federation context (federation user-sync etc.) — flagged but not converted. Phase 7.
- **Deferred:** `notifier.py` and `replication/http/*` — re-classified as M and phase 10 respectively. Out of scope permanently for phase 2.

---

## Phase 2 close — overall definition of done

When all of the following are true:

1. **Probes:**
   - `PHASE 2A PROBES` block green: `test_create_room_per_tenant`, `test_state_group_cache_isolation`
   - `PHASE 2B SEED-ROW PROBES` block green: `test_bg_loops_quiet_on_non_primary_tenant`, `test_singleton_seed_rows_present_per_tenant`
   - `PHASE 2C PROBES` block green: `test_presence_loop_per_tenant`, `test_server_notice_sender_per_tenant`
   - All previously-green probes (phase 1, 1B, 2 originals) stay green
2. **Trackers:**
   - `roadmap-progess.md` executive-summary row for phase 2 flips from 🟢 to ✅
   - `roadmap-progess.md` cross-cutting item 7 (state_groups leak) flipped to `[RESOLVED 2026-04-DD in phase 2a close]`
   - `multi-tenancy-workflow/data.js` background-processes stage shows presence as converted
3. **Docs:**
   - `docs/multi_tenant_state_cache_keying.md` lands (`2a`)
   - `docs/multi_tenant_isolation_model.md` updated with seed-row catalogue (`2b`)
   - `docs/multi_tenant_hostname_audit.md` updated with re-classifications (`2c`)
4. **Tests:**
   - `tests/tenant/test_state_group_cache_isolation.py` passes
   - `tests/tenant/test_schema_bootstrap.py` extended and passes
   - `tests/tenant/test_config.py` extended and passes
   - Full `trial tests.tenant` suite green
5. **`sync-roadmap` skill** has been run after each of `2a`, `2b`, `2c` (three separate runs, one per sub-phase, separate commits from the code changes per standing rule #6).

---

## Out of scope / deferred to phase N

Recorded so future-you doesn't re-litigate any of this:

- **Pushers (`pusherpool.py`, `emailpusher.py`, `httppusher.py`)** — deferred to **phase 3** (per-tenant SSO / email / push / identity). Reason: pushers belong to a user, the user pins the tenant, so the fan-out shape may differ from `notify_new_event` style; and pushers without per-tenant push config is half a fix.
- **`user_directory.py:614 kick_off_remote_profile_refresh_process`** — deferred to **phase 7** (federation outbound). The inner `kick_off_remote_profile_refresh_process_for_remote_server` takes a *remote* server_name argument, so per-tenant fan-out semantics need re-thinking.
- **`MatrixFederationHttpClient` outbound signing (~11 hits)** — deferred to **phase 7**. Federation is the next major chunk and signs outbound traffic with the primary key.
- **`federation_sender`, `federation_server`, `well_known_resolver`, `federation_client` (~58 hits)** — deferred to **phase 7**.
- **`replication/http/*` (~46 hits)** — deferred to **phase 10** (workers). Replication only exists when workers exist; no work needed in a single-process deployment.
- **Storage background updaters (`background_updates.py`, `_base.py` storage hits)** — **deliberately permanently global per the roadmap.** They operate on schema structure, not per-row data; the schema bootstrap step already runs them against each tenant schema.
- **`module_api/callbacks/*` hits (~22)** — deferred to **phase 4** (per-tenant rate limiting + app services), where the module API surface is naturally re-audited for tenant scoping.
- **Workers** — **phase 10**. No tenant-aware worker work in phase 2.
- **Hot add/remove of tenants** — **phase 6**.
- **The systemic `@cached`-decorator key-builder change** that injects tenant context into every cache key — **deferred unless `2a` bisect demands it.** Defence-in-depth, forty-call-site sweep, not justified by the failing probe alone.
- **`MultiWriterIdGenerator` for `events_stream_seq` in-process counter overlap** — known adjacent hazard surfaced by the `2a` audit; **flagged for a future probe** but not in scope for `2a` because it's not the source of the createRoom 403.
- **Multi-DB (separate state DB pool) tenant routing** — moot for the docker rig (single combined DB); flagged for future probe in any multi-DB rig.

---

## Verification (the canonical reviewer command set)

```bash
# Unit tests
trial tests.tenant

# docker-multitenant probe rig (the canonical CI surface)
cd docker-multitenant
docker compose up -d
docker compose run --rm test     # runs PHASE 1, 1B, 2, 2A, 2B, 2C probes
bash scripts/run_isolation_probes_host.sh   # host-side probes (sequence + seed-row)
docker compose down -v
```

Expected end state on a fresh `up -d`: every `[PASS]` line, zero `[FAIL]`, zero `StoreError: 404` log lines under any tenant's `server_name=` context, `createRoom` returns 200 on all three tenants.
