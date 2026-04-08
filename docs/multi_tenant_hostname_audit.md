# `hs.hostname` / `self.server_name` audit

Enumerates every read of `self.hs.hostname` or `self.server_name` in
`synapse/` (production code only — `tests/` excluded). Each hit has a
disposition:

- **R** — Request-path string construction. Leaks the global hostname
  to user-facing responses. **Must fix in this task** by routing
  through `HomeServer.effective_server_name()` (defined in
  `synapse/server.py:723`).
- **S** — Storage / constructor capture. Storage is schema-isolated
  per tenant, and `self.server_name = hs.hostname` is used as a
  `@cached`-key prefix, audit column, or process identifier where the
  primary hostname is correct. Covered by Mechanism B (schema
  isolation, see `docs/multi_tenant_isolation_model.md`).
- **B** — Background process. Hits live inside loops that have not yet
  been converted to `run_as_background_process_per_tenant`. **Deferred
  to phase 2 close** — these will be revisited as part of the pusher /
  notifier / federation-sender fan-out.
- **M** — Metric label (`SERVER_NAME_LABEL`) or `Measure(...,
  server_name=...)` context name. The label is intentionally the
  process-level hostname so that metric series stay stable across
  tenants. Most M hits sit inside S/B code anyway.
- **T** — Test code. Out of scope for production audit.

Raw grep dump: `/tmp/hostname-audit-raw.txt` (522 hits across 23
top-level subdirs). Counts by subsystem at the bottom of this doc.

Generated on **2026-04-08** as part of phase 1 close.

## Request-path hits (R) — fixed in this commit

| File:Line | Before | After | Notes |
|---|---|---|---|
| `synapse/rest/client/register.py:827` | `"home_server": self.hs.hostname` | `self.hs.effective_server_name()` | Register response body — leaked global hostname to every newly-registered user. Caught by Phase 2 visual inspection; the Task 8 `registration_response` probe verifies `user_id` qualification but not the `home_server` field. |
| `synapse/rest/client/register.py:885` | same | same | Guest-registration response body. |
| `synapse/rest/consent/consent_resource.py:132` | `UserID(username, self.hs.hostname).to_string()` | `UserID(username, self.hs.effective_server_name()).to_string()` | Consent flow GET — qualified user_id was being built with the wrong hostname when consent was reached under a tenant Host header. |
| `synapse/rest/consent/consent_resource.py:165` | same | same | Consent flow POST. |
| `synapse/rest/client/openid.py:79,102` | init-time `self.server_name = hs.config.server.server_name`, response `"matrix_server_name": self.server_name` | dropped capture, `self.hs.effective_server_name()` per-request | OpenID `request_token` response. The `matrix_server_name` field is what relying parties use to verify the token against the federation `/openid/userinfo` endpoint — wrong value here lands the verifier on the wrong tenant's federation surface, which is a real cross-tenant identity bridge bug. |
| `synapse/rest/media/upload_resource.py:50,150` | init-time capture, request-path check `if server_name != self.server_name` | dropped capture, `self.hs.effective_server_name()` per-request | `AsyncUploadServlet.on_PUT` validates that the URL's `server_name` segment matches the local server. Under multi-tenant, requests under `acme.localhost` should be allowed to upload media under that server_name and rejected for `corp.localhost`. The pre-fix check rejected ALL non-primary tenants. |

## Storage / constructor capture hits (S) — covered by schema isolation

These all set `self.server_name = hs.hostname` (or read it directly)
in storage data sources, controllers, or process-scoped helpers.
Storage is schema-isolated per tenant via `_set_tenant_schema`
(`synapse/storage/database.py:683`); the captured `server_name` field
is used for `@cached` key prefixes, the `SERVER_NAME_LABEL` metric
label, audit columns, or process identifiers — all of which are
correct at the *primary* hostname level. Mechanism B (schema
isolation) covers data correctness.

| File | Hit count | Use site shape |
|---|---|---|
| `synapse/storage/_base.py` | 1 | `@cached`-key prefix on `SQLBaseStore` |
| `synapse/storage/database.py` | ~10 | `DatabasePool.server_name`, used for metric labels and as a process-scoped identifier; tenant isolation happens via `_set_tenant_schema` per transaction |
| `synapse/storage/util/id_generators.py` | 3 | id-generator process identifier |
| `synapse/storage/background_updates.py` | 1 | constructor capture; background updates are deliberately global per roadmap |
| `synapse/storage/controllers/state.py` | 2 | `Measure(..., server_name=...)` |
| `synapse/storage/controllers/persist_events.py` | ~15 | `SERVER_NAME_LABEL` metric labels, `Measure` context names — labels are intentionally process-level |
| `synapse/storage/controllers/purge_events.py` | 1 | constructor capture |

**Disposition:** leave as-is. Schema isolation makes the *data* correct;
the metric labels and `@cached` keys remaining at the primary hostname
is the desired behavior for cross-tenant aggregate metric queries.

## Background-process hits (B) — deferred to phase 2 close

These are inside background loops or push/federation/notifier code
paths that have not been converted to per-tenant fan-out yet. Phase 2
close will revisit these as it walks each subsystem.

| Subsystem | Files | Hit count |
|---|---|---|
| Push / pusherpool | `synapse/push/emailpusher.py`, `synapse/push/httppusher.py`, `synapse/push/pusherpool.py`, `synapse/push/bulk_push_rule_evaluator.py`, `synapse/push/mailer.py` (constructor only — request-path uses already done by Task 7b) | ~19 |
| Federation sender | `synapse/federation/sender/__init__.py`, `synapse/federation/sender/per_destination_queue.py`, `synapse/federation/sender/transaction_manager.py`, `synapse/federation/send_queue.py`, `synapse/federation/federation_client.py`, `synapse/federation/federation_server.py` | ~58 |
| Replication | `synapse/replication/http/_base.py`, `synapse/replication/http/send_events.py`, `synapse/replication/http/federation.py` | ~46 |
| Notifier | `synapse/notifier.py` | 6 |
| Server notices | `synapse/server_notices/server_notices_manager.py` | 5 |
| Module API callbacks | `synapse/module_api/callbacks/spamchecker_callbacks.py`, `synapse/module_api/callbacks/media_repository_callbacks.py`, `synapse/module_api/callbacks/ratelimit_callbacks.py` | ~22 |
| Federation client well-known resolver | `synapse/http/federation/well_known_resolver.py` | 2 |
| Federation HTTP client signing | `synapse/http/matrixfederationclient.py` | 11 — note: signs outbound federation requests with `self.server_name`. **Cross-tenant federation is a known phase 2 hazard.** Documented under `multi_tenant_roadmap.md` Phase 2. |
| Logging context | `synapse/logging/context.py` | 2 — sentinel context defaults |

**Disposition:** deferred. Phase 2 close plan must enumerate which of
these need per-tenant fan-out vs which run only against the primary
server.

## Metric-label / Measure-name hits (M)

The bulk of the remaining hits — handlers (138), storage controllers,
push, federation — are `**{SERVER_NAME_LABEL: self.server_name}` or
`Measure(..., server_name=self.server_name)`. The label is
intentionally process-scoped so Prometheus series stay stable
regardless of which tenant's request is currently in flight. These
also overlap heavily with B classification.

**Disposition:** leave as-is. If a future plan wants per-tenant
metric series, it will need a deliberate label-cardinality review
first.

## Specific deliberate non-fixes

| File:Line | Why not fixed |
|---|---|
| `synapse/rest/admin/media.py:64` | Captured but never used in `MediaInfo` class — dead code, not a leak. Cleanup out of scope. |
| `synapse/rest/admin/media.py:373,394` | `PurgeMediaCacheRestServlet` legacy endpoint check. The code's own comment says: "This check is useless, we keep it for the legacy endpoint only." Admin-only endpoint, no end-user data exposure. Defer. |
| `synapse/rest/client/register.py:340` | `our_server_name=self.server_name` passed into `FederationRateLimiter`. The rate limiter uses this as an internal accounting key, not a user-facing string. M classification. |
| `synapse/rest/client/register.py:86,186,337` | Constructor captures used only for `SERVER_NAME_LABEL` metric labels — M. |
| `synapse/rest/client/account.py:81,333,420` | All three are constructor captures used only for `SERVER_NAME_LABEL` — M. |
| `synapse/rest/client/room.py:859,1320` | Constructor captures used for `Measure` context names — M. |
| `synapse/rest/client/sync.py:115` | Constructor capture used for `Measure` — M. |

## Subsystem hit counts (raw)

```
138 handlers     B / M (mostly)
104 storage      S / M
 58 federation   B (phase 2)
 46 replication  B (phase 2)
 28 rest         R fixed (5 sites), rest are M / dead
 22 module_api   B (callbacks)
 22 http         S / B (federation client signing)
 19 push         B (phase 2)
 16 util         S
 11 api          S
  9 state        S
  9 appservice   B / M
  8 metrics      S (label-source)
  8 media        R fixed (1 site), rest are S
  6 notifier.py  B
  5 server_notices B
  4 config       S (config bootstrap, fine)
  2 server.py    The effective_server_name() helper itself + is_mine
  2 logging      S (sentinel context)
  2 crypto       S (Keyring init)
  1 tenant_registry.py  S
  1 _scripts     T-equivalent (port_db script)
  1 events       S
```

## Phase 2 follow-up punch list

The B-classified hits above are the input list for the Phase 2 close
plan's "background process tenant fan-out" work. Specifically:

1. **Federation sender** — needs per-tenant `_destinations`,
   per-tenant signing key selection (already partially handled by
   `MultiTenantKeyring`), per-tenant outbound queue.
2. **Pusher pool** — needs per-tenant pusher loops; currently the
   pusher pool sees a single global view.
3. **User directory** — already known-failing (Task 6 baseline);
   `notify_new_event` background loop is not tenant-aware.
4. **Server notices** — `server_notices_manager` uses
   `self.server_notices_mxid` from primary hostname; needs per-tenant
   MXID resolution.
5. **`MatrixFederationHttpClient` outbound signing** — every outbound
   federation request signs with the primary server name. Federation
   under multi-tenant is fundamentally Phase 2.
