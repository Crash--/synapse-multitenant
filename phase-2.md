# Phase 2 close — kickoff prompt

Paste everything below this line into a fresh Claude Code session.

---

I'm starting **phase 2 close** of the multi-tenant Synapse fork. Phase 1 close just landed (commits up through `cb163162c`, plus tracker updates in `roadmap-progess.md` and `multi-tenancy-workflow/data.js`). Read these first, in order:

1. `CLAUDE.md` — fork orientation
2. `docs/multi_tenant_roadmap.md` — phase spec
3. `roadmap-progess.md` — current state, especially **cross-cutting item 7** (state_groups leak) and the "Suggested next steps" section
4. `docs/superpowers/plans/2026-04-08-phase-1-close-real-tenant-isolation.md` — the phase 1 close plan, as a structural template for what I want for phase 2
5. `docs/multi_tenant_hostname_audit.md` — phase 1 close audit, specifically the "Background-process hits (B)" table and the "Phase 2 follow-up punch list"

**Goal of this session:** produce a phase 2 close plan document at `docs/superpowers/plans/2026-04-DD-phase-2-close-tenant-aware-bg-processes.md` (use today's date). Do **not** start implementing — just plan.

## Decompose phase 2 into sub-phases

Phase 2 is too large to ship as one monolithic close. Decompose it into lettered sub-phases (`2a`, `2b`, `2c`, …) where **each sub-phase is independently shippable, independently verifiable, and has its own definition of done**. Use as many letters as the work actually needs — do not artificially inflate or compress.

Suggested decomposition axes (the plan should justify whichever it picks):

- **By blast radius / severity** — start with the highest-severity bug (state_groups leak) as `2a`, since it's the only thing currently making the rig functionally single-tenant. Lower-severity bg-loop conversions follow.
- **By subsystem** — group all presence work into one sub-phase, all pusher work into another, all user_directory work into another. This makes parallel execution possible if the team grows.
- **By dependency** — if the state_groups bisect surfaces a shared root cause (e.g. an event-auth helper that other bg loops also use), the fix for that root cause becomes its own sub-phase that other sub-phases depend on.

The plan should include a **dependency graph** between sub-phases. `2a` blocking `2b` is fine. `2c` and `2d` running in parallel is fine. Implicit ordering is not fine.

Each sub-phase needs:

- **Scope** — one paragraph, what's in and what's out
- **Red probes** — the failing tests that gate the sub-phase. If you can't write the probe, you don't understand the bug yet
- **Files touched** — best-effort list, will grow during implementation
- **Definition of done** — which probes go green, which trackers update
- **Risks & deferrals** — what could surface and bump scope, what stays out and why
- **Estimated task count** — number of discrete commits expected (rough)

## Known phase 2 work to fold into the sub-phases

In priority order:

1. **state_groups / event-auth cross-tenant leak** (highest severity). `POST /createRoom` returns 403 `M_FORBIDDEN` on `corp.localhost` and `startup.localhost` but succeeds on `acme.localhost`. Surfaced by phase 1 close once `, public` search_path fall-through was removed. Trace path starts at `synapse/handlers/room.py::create_room` → `EventAuthHandler.compute_auth_events` → state_groups storage. Likely a call site reading global hostname or primary schema instead of request-tenant. **This belongs in `2a`.**
2. **Remaining background loop conversions:** presence, pushers, user_directory `notify_new_event`. The remote-profile-refresh sub-loop stays deferred to the federation phase by design.
3. **Anything else surfaced by the phase 1 close hostname audit** at `docs/multi_tenant_hostname_audit.md` — see the "Background-process hits (B)" table and the "Phase 2 follow-up punch list" at the bottom.

## Process improvements I want baked into this plan

Lessons from phase 1 close — bake these into the plan as standing rules, not one-off task notes:

- **Probes-first, always.** Task 0 of every sub-phase is "write the red probe that proves this is broken." If the probe can't be written, the bug isn't understood yet. The state_groups leak should have been a probe on day 1 of phase 1, not a day 9 surprise.
- **Probe-after-every-task** discipline. `cd docker-multitenant && docker compose restart synapse && docker compose run --rm test` is the inner loop. Don't batch regressions to a checkpoint task at the end of a sub-phase.
- **Persistent audit dump file** under `docs/` or `/tmp/`, generated once per sub-phase, referenced by every subagent. Phase 1 re-grepped the 522-hit hostname catalogue several times because no agent had the prior raw output.
- **Parallel subagent dispatch** for independent work. Presence, pushers, and user_directory loops are disjoint files — they should fire in one message, not in series. Sub-phases without dependencies on each other should also run in parallel where the team allows.
- **Sonnet 4.6 as default subagent model**, Opus 4.6 only for tasks whose description contains "trace", "bisect", "diagnose", or "audit/classify". Mechanical pattern work doesn't need Opus and Opus 4.6 has been hitting 529 overload regularly.
- **Commit after every green task**, separately from tracker updates. Compaction during phase 1 close caused a few moments of ambiguous working-tree state.
- **Smaller, scoped subagent prompts.** Reserve the heavy implementer + spec-review + code-review three-agent flow for tasks with real design judgment. Mechanical refactors get one agent with a tight prompt.

## Skills to use in this session

- **`brainstorming`** — invoke this *first*, before drafting any plan structure. The state_groups leak in particular has multiple possible root causes (shared `state_groups_id` sequence vs. an event_id qualifier reading global hostname vs. an event-auth helper bypassing tenant context), and brainstorming forces those alternatives onto the table instead of locking in the first hypothesis. The plan should reference which alternatives were considered and why the chosen ordering won. Brainstorming should also weigh the sub-phase decomposition axes against each other before committing to one.
- **EnterPlanMode** — once brainstorming converges, draft the plan in plan mode so I can approve before any file is written.
- **`sync-roadmap`** — *do not run this in this session*. It's the post-implementation skill that updates `roadmap-progess.md` and `multi-tenancy-workflow/data.js` after work lands. Mention it in each sub-phase's "definition of done" so future tasks remember to run it after that sub-phase ships.

Do **not** invoke `using-superpowers` explicitly — the session-start hook already loads it. Do invoke any other skill that the using-superpowers flow tells you applies (even at 1% relevance).

## Deliverable shape

Mirror the phase 1 close plan, but with sub-phases as the top-level structure:

- Header with date, overall goal, success criteria
- Sub-phase decomposition rationale (1–2 paragraphs explaining which axis you chose and why)
- Dependency graph between sub-phases (ASCII or bullet list)
- For each sub-phase `2a`, `2b`, …:
  - Scope, red probes, files touched, task list, definition of done, risks
  - Each task in the sub-phase: scope, files, red probe gate, verification command
- Overall "definition of done" for phase 2 close: which probes must be green, which trackers must be updated, when the executive-summary row in `roadmap-progess.md` flips back to ✅
- "Out of scope / deferred to phase N" section so future-you doesn't have to re-litigate (federation sender stays in phase 7, remote profile refresh stays in phase 7, etc.)

## First concrete actions for this session

1. Read the five docs above
2. Run `git log --oneline -20` to see what landed in phase 1 close
3. Reproduce the state_groups leak: `cd docker-multitenant && docker compose up -d && docker compose run --rm test`, capture the failing probe output (just enough to confirm the symptom — do not start diagnosing)
4. Skim `synapse/handlers/room.py` and `synapse/handlers/event_auth.py` to get a feel for the call shape — *just enough* to write a credible plan, not a full diagnosis
5. Invoke the `brainstorming` skill to weigh sub-phase decomposition options and state_groups root-cause hypotheses
6. EnterPlanMode and draft the plan document
7. Show me the plan before writing the file

Do **not** start writing code. Do **not** start subagent dispatches for implementation. The only deliverable from this session is the plan document and my approval of it.
