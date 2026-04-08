---
name: sync-roadmap
description: Reconcile roadmap-progess.md and the multi-tenancy-workflow animation against the current state of the branch. Use when the user says "/sync-roadmap", asks to "update the roadmap progress", or has just finished a phase / loop conversion / leak fix and wants the trackers brought up to date.
---

# sync-roadmap

Bring the multi-tenant roadmap trackers in sync with what has actually
landed on the branch. The trackers are the **only** thing management
reads — they must not drift from the code.

## What this skill maintains

Three files are kept consistent with each other and with the code:

1. **`docs/multi_tenant_roadmap.md`** — the canonical roadmap. Source of
   truth for phase numbering, names, and "what counts as done".
2. **`roadmap-progess.md`** (note the spelling — do not rename) — the
   detailed progress audit cross-referenced against the roadmap.
   Structured by phase number with verification output and known gaps.
3. **`multi-tenancy-workflow/data.js`** — the animated explainer's
   stage data. The "background processes" stage in particular lists
   the loops that have been converted; it must reflect reality.

When this skill runs, all three should agree.

## Procedure

### Step 1 — Establish "what changed since last sync"

- Read the current `roadmap-progess.md` and note the date in its header
  (e.g. `**Date:** 2026-04-08`).
- `git log --since=<that date> --name-status` to see what landed since.
- `git status` and `git diff` for uncommitted work that should also be
  reflected (the trackers cover *current branch state*, not just
  committed state).
- Read `docs/multi_tenant_roadmap.md` so phase numbers and names match.

### Step 2 — Classify each change against the roadmap phases

For each changed file, decide which roadmap phase (if any) it belongs
to. The mapping that has held so far:

| Path pattern | Likely phase |
|---|---|
| `synapse/tenant_*.py`, `synapse/config/tenants.py` | Foundation (already done) |
| `synapse/http/site.py`, log config, leak probes | Phase 1 (audit & instrumentation) |
| `synapse/handlers/*.py` background loop conversions | Phase 2 (tenant-aware bg processes) |
| `synapse/handlers/presence.py`, `synapse/push/*` | Phase 2 (deferred subset) |
| `synapse/config/*sso*.py`, oidc/saml/cas, email, push config | Phase 3 |
| Rate limiting, app services | Phase 4 |
| `synapse/media/*`, storage providers | Phase 5 |
| Tenant hot reload, backup/restore CLI | Phase 6 |
| `synapse/federation/sender/*` | Phase 7 |
| `synapse/federation/transport/server/*`, `.well-known/matrix/server` | Phase 8 |
| `synapse/handlers/e2e_*`, key backup, cross-signing | Phase 9 |
| `synapse/app/*worker*` | Phase 10 |
| `docker-multitenant/`, `docker-demo/`, `multi-tenancy-workflow/`, `scripts/` | Infrastructure (not a phase — goes in the "Infrastructure additions" section) |

If a change doesn't fit any phase, it likely belongs in
"Cross-cutting items surfaced during this work". Flag it explicitly
rather than silently dropping it.

### Step 3 — Update `roadmap-progess.md`

- Bump the `**Date:**` header to today.
- Update the **executive-summary table** at the top: a phase's status
  changes when work lands in it. Status vocabulary:
  `⏸ Not started` → `🟡 In progress` → `🟢 Majority complete` →
  `✅ Substantially done` → `✅ Complete`.
- Under the phase's section: append landed work to the appropriate
  list/table. For phase 2 (and any future phase that converts a list
  of call sites), keep the "Loops converted" / "call sites converted"
  table sorted by file path. Do not delete prior entries.
- Update the "Gaps still owed" subsection by removing items that are
  now done.
- Add new findings to **Cross-cutting items** if relevant — that
  section is the early-warning channel for management.
- Re-check the **Suggested next steps** ordering: if a phase is now
  done, it drops off the list; if a new blocker emerged, it gets added.

### Step 4 — Update `multi-tenancy-workflow/data.js`

- Open the file and find the stage(s) that correspond to the work
  that landed.
- For background-process work, the relevant stage's `logLines`
  array should mention each converted loop. Add new entries; do not
  remove old ones.
- For phase 1 / leak-fix work, update the "audit" or "instrumentation"
  stage if one exists; if not, ask the user before inventing a new
  stage (the animation has a fixed visual layout).
- Keep prose terse — these strings get rendered in a small bubble.

### Step 5 — Verify the trackers agree

Quick sanity checks before declaring done:

- Every loop listed in `roadmap-progess.md` phase 2 table is also
  mentioned in `multi-tenancy-workflow/data.js`.
- Every phase status in the executive-summary table is consistent
  with the prose in that phase's section.
- `git diff roadmap-progess.md multi-tenancy-workflow/data.js` shows
  changes in both files (or, if nothing landed since the last sync,
  cleanly report that and exit without writing).

### Step 6 — Report to the user

A short summary, in this shape:

    Synced. Changes since 2026-04-08:
      - phase 2: converted X, Y, Z (status now: ✅)
      - phase 1: closed wellknown leak (status unchanged)
      - cross-cutting: added note about <thing>
    Trackers updated: roadmap-progess.md, multi-tenancy-workflow/data.js

Do **not** commit. The user commits the trackers themselves so the
commit message can describe the work, not the bookkeeping.

## Things to avoid

- Do not rename `roadmap-progess.md` (the typo is load-bearing —
  changing it breaks any external links).
- Do not rewrite the roadmap document itself (`docs/multi_tenant_roadmap.md`).
  The roadmap is the spec; the progress file is the audit. If the
  spec needs to change, that is a separate conversation with the user.
- Do not invent verification output. If a probe wasn't actually run,
  do not paste fabricated `[PASS]` lines into the progress file.
  Either run the probe or omit the verification block for that item.
- Do not delete prior landed-work entries when updating, even if you
  think they are redundant. The progress file is append-only history.
- Do not touch animation visual layout (positions, colours, stage
  count) — only the data strings inside existing stages.
