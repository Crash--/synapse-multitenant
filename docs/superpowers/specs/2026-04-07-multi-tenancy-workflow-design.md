# Multi-Tenancy Workflow — Animated Explainer

**Date:** 2026-04-07
**Status:** Approved
**Audience:** Developers onboarding to the multi-tenant Synapse fork

## Goal

Build a self-contained, no-build, animated HTML page that explains how multi-tenancy is currently implemented in this Synapse fork. Target audience: a new engineer who needs to understand how a request flows through tenant routing → context → schema switching → keyring → media path resolution, with pointers to the real source files.

The page should be openable directly from the filesystem (`file://`) with no dependencies, no build step, no network access.

## Deliverable

A new top-level folder `multi-tenancy-workflow/` containing:

```
multi-tenancy-workflow/
├── index.html      # entry point, all three scenes in one page
├── styles.css      # layout, colors, transitions, animation keyframes
├── app.js          # timeline orchestrator + scene controllers
├── data.js         # the "script": stages, code snippets, log lines, file refs
└── README.md       # how to view it
```

Plain HTML/CSS/JS only. SVG (inline, scriptable) for diagrams. All content embedded; works fully offline.

## Architecture

### Single page, three scenes, one shared timeline

Three scenes are three different lenses on the *same* request lifecycle. Putting them in one page with a single shared timeline at the bottom lets the reader switch lenses mid-playback and compare.

- **Top chrome:** title, scene tabs (A / B / C), parallel-tenant toggle.
- **Scene area:** whichever scene is active.
- **Bottom transport bar:** ▶/⏸, ↻ restart, scrubber with stage ticks, current-stage label, stage `n/10` counter. Stages on the scrubber are clickable → jump (and pause).

### Animation engine

A single `requestAnimationFrame` loop in `app.js` drives a `currentTimeMs` counter. Stages are defined in `data.js` with `startMs`/`endMs`. Each frame: compute current stage index, fire `stageChange` if it changed, let each mounted scene update its DOM/SVG. Pure data → view; no per-scene timers.

### Scene contract

Each scene is a class with:

```js
class Scene {
  mount(rootElement)      // build DOM
  onStageChange(stage, progress)  // stage = data object, progress = 0..1 within stage
  unmount()               // tear down
}
```

Tab switching calls unmount/mount; the timeline keeps running underneath, so a newly-mounted scene jumps straight to the current stage.

### Why SVG (not canvas)

- Components are stylable via CSS classes (`.active`, `.dimmed`)
- Degrades gracefully — static diagram visible even if JS fails
- DOM-inspectable for debugging

## The shared timeline (the "script")

The request being followed: `GET /_matrix/client/versions` with `Host: acme.localhost`.

| # | Stage | Duration | What lights up | Side panel |
|---|---|---|---|---|
| 0 | Client request | 1.5s | Browser icon emits packet with `Host: acme.localhost` highlighted | Raw HTTP request |
| 1 | Reverse proxy | 1.5s | Nginx box; packet enters; `Host` preserved via `proxy_set_header` | `docker-multitenant/nginx/nginx.conf` snippet |
| 2 | Synapse entry / TenantRouter | 2s | Synapse outer box → TenantRouter component | `synapse/tenant_registry.py` lookup |
| 3 | TenantConfig resolved | 1.5s | TenantConfig dataclass card pops up: `server_name`, `database_schema`, `signing_key_path`, `media_store_path` | `synapse/config/tenants.py` |
| 4 | Context binding | 1.5s | `contextvars.ContextVar` slot fills; "ctx: acme" badge follows packet | `synapse/tenant_context.py — set_current_tenant()` |
| 5 | DB schema switch | 2s | Three Postgres schemas drawn; only `tenant_acme` lights up; `SET search_path TO tenant_acme, public` types in | Storage note |
| 6 | Keyring lookup | 1.5s | MultiTenantKeyring; key ring rotates to `acme` key | `synapse/crypto/multitenant_keyring.py` |
| 7 | Media path resolution | 1.5s | FS tree highlights `/media/acme/...`, others dim | `synapse/media/multitenant_filepath.py` |
| 8 | Response | 1.5s | Packet travels back, tagged `[acme.localhost]` | JSON response preview |
| 9 | Isolation recap | 2s | All three tenants pulse simultaneously: "this happens in parallel, isolated, in one process" | One-line takeaway |

Total ~16s.

### Parallel-tenant mode

A toggle in the top chrome restarts the timeline emitting *two* sets of stage events (acme + corp) offset by ~200ms. Scenes render two packets / two log streams in their respective tenant colors. Strongest visual proof of isolation.

## The three scenes

### Scene A — Animated request flow diagram

Wide horizontal SVG, components left-to-right:

```
[Client] → [Nginx] → [Synapse{ TenantRouter → ContextVar }] → [Postgres schemas]
                                                              ↘ [Keyring]
                                                              ↘ [Media FS]
```

A glowing packet `<circle>` travels along an SVG `<path>` between components. As it arrives at each node, that node's stroke pulses (CSS class added), and the right-hand side panel swaps to that stage's code snippet + file reference. Inactive nodes are dimmed. Connecting paths animate `stroke-dashoffset` for the "flowing" effect.

The conventional, fast-to-grasp view.

### Scene B — Layered stack view

Vertical stack of 5 layered slabs (Network → Routing → Tenant Context → Storage → Response), drawn as offset rectangles like a cake from the side. As the timeline advances, each layer in turn:

1. Slides forward / brightens (active)
2. Shows an inset card with component name + 2-3 line code snippet
3. Recedes when next layer activates

Emphasizes that multi-tenancy is *cross-cutting* — touches every layer of the stack.

### Scene C — Split-screen: diagram + live log

- **Left half:** compact box-and-arrow version of Scene A (no traveling packet — the active component just lights up).
- **Right half:** a fake terminal (`<pre>` styled like a TTY) where tenant-tagged log lines stream in as each stage fires:

```
14:02:11 [acme.localhost] req GET /_matrix/client/versions
14:02:11 [acme.localhost] tenant resolved from Host header
14:02:11 [acme.localhost] context bound: tenant_acme
14:02:11 [acme.localhost] db: SET search_path TO tenant_acme, public
14:02:11 [acme.localhost] keyring: loaded ed25519:auto acme.signing.key
14:02:11 [acme.localhost] media root: /var/synapse/media/acme
14:02:11 [acme.localhost] 200 OK
```

In parallel-tenant mode, lines from `[acme.localhost]` and `[corp.localhost]` interleave, color-coded.

## Visual style

- **Theme:** dark. Background `#0d1117`, panels `#161b22`, borders `#30363d`, text `#c9d1d9`.
- **Tenant accent colors** (consistent across scenes):
  - `acme.localhost` → cyan `#22d3ee`
  - `corp.localhost` → amber `#f59e0b`
  - `tenant_startup` → violet `#a78bfa`
- **Fonts:** `ui-monospace, "JetBrains Mono", Menlo` for code/log; `system-ui` for chrome.
- **Background:** subtle dot-grid on the diagram canvas.
- **Code highlighting:** tiny hand-rolled regex highlighter for Python keywords / strings / comments. No third-party libs.

## Accessibility

- `prefers-reduced-motion` media query disables packet motion and pulses, falling back to instant state changes. Stage label still updates so the narrative is followable.
- Sufficient contrast on dark theme (text `#c9d1d9` on `#0d1117` ≈ 13:1).
- Keyboard: spacebar play/pause, arrow keys for prev/next stage, number keys 1/2/3 to switch scenes.

## Out of scope (explicit YAGNI)

- Audio narration
- Animating real Synapse execution — this is a diagrammatic explainer, not a debugger
- Mobile-optimized layout (desktop-only; README says so)
- Federation or admin API stages (timeline stays focused on the core request lifecycle)
- Build pipeline, bundler, framework, or any npm dependency

## File responsibilities

| File | Responsibility |
|---|---|
| `index.html` | Page skeleton: chrome (title, tabs, toggle), scene mount point, transport bar. Loads `data.js`, `app.js`. References `styles.css`. |
| `styles.css` | Theme variables, layout, transport bar, scene-specific layout, animation keyframes (pulse, packet glide, log scroll-in), `prefers-reduced-motion` overrides. |
| `data.js` | Exports `STAGES` array (id, label, start/end ms, fileRef, code, logLines), `TENANTS` color map, parallel-mode timing offsets. Single source of narrative truth. |
| `app.js` | Timeline loop (`requestAnimationFrame`), transport controls, scene registry & tab switching, three Scene classes (A/B/C), shared event dispatch. |
| `README.md` | One paragraph on what it is + how to view it (`open index.html` or `python -m http.server`). |

## Success criteria

- Folder opens by double-clicking `index.html` — no install, no build, no network.
- The timeline plays end-to-end automatically and loops cleanly.
- Each scene renders a recognizable, accurate depiction of the corresponding lifecycle stage; file references in the side panel match real paths in the repo.
- Tab switching mid-playback works; parallel-tenant toggle works.
- `prefers-reduced-motion` works.
- All file references in `data.js` resolve to files that exist in the repo at the time of writing (`tenant_context.py`, `tenant_registry.py`, `config/tenants.py`, `crypto/multitenant_keyring.py`, `media/multitenant_filepath.py`, `nginx/nginx.conf`).
