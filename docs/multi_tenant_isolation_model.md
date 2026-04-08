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

## Mechanism B load-bearing set (sequences)

Every database-wide sequence must exist *per tenant schema* with the
tenant's column defaults pointing at the per-schema sequence.
Otherwise `SELECT nextval('public.events_stream_seq')` is shared
across tenants and `events.stream_ordering` interleaves, breaking
sync-token semantics and per-tenant backup/restore.

- `account_data_sequence`
- `application_services_txn_id_seq`
- `cache_invalidation_stream_seq`
- `device_inbox_sequence`
- `device_lists_sequence`
- `e2e_cross_signing_keys_sequence`
- `event_auth_chain_id`
- `events_backfill_stream_seq`
- `events_stream_seq`
- `instance_map_instance_id_seq`
- `presence_stream_sequence`
- `push_rules_stream_sequence`
- `pushers_sequence`
- `receipts_sequence`
- `thread_subscriptions_sequence`
- `un_partial_stated_event_stream_sequence`
- `un_partial_stated_room_stream_sequence`
- `user_id_seq`

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
