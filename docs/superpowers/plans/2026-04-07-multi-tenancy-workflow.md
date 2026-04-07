# Multi-Tenancy Workflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a self-contained, no-build animated HTML page (`multi-tenancy-workflow/`) explaining how multi-tenancy is implemented in this Synapse fork, with three synchronized scenes driven by one shared timeline.

**Architecture:** Plain HTML/CSS/JS, no framework, no build step, no network. One `index.html` page with three scenes (flow diagram / layered stack / diagram + live log) sharing a single `requestAnimationFrame`-driven timeline. Stage data lives in one place (`data.js`) and is the single source of narrative truth. Each scene is a class implementing `mount/onStageChange/unmount`.

**Tech Stack:** Vanilla HTML5, CSS3 (custom properties, keyframes, `prefers-reduced-motion`), ES2020 JavaScript modules, inline SVG. Zero dependencies.

**Note on TDD:** This is a static visual deliverable with no runtime test surface (no DOM-testing framework, no CI). "Testing" each task means: open `index.html` in a browser, observe specific behavior, confirm no console errors. Where logic is non-trivial (timeline math, parallel-mode interleaving), the plan adds `console.assert()` self-checks runnable from the browser DevTools.

---

## File Structure

```
multi-tenancy-workflow/
├── index.html      # page skeleton + chrome + scene mount + transport bar
├── styles.css      # theme vars, layout, scene-specific layout, keyframes
├── data.js         # STAGES, TENANTS, parallel offsets — single source of narrative
├── app.js          # timeline loop, transport, scene registry, three Scene classes
└── README.md       # what it is + how to view it
```

Decomposition rationale: `data.js` is isolated so the narrative can be edited without touching engine code. `app.js` holds the engine and all three scene classes — they share enough infrastructure (color map, stage event shape, code highlighter) that splitting them would create unhelpful churn. Total target: ~700 LOC across all files.

---

### Task 1: Project skeleton + README

**Files:**
- Create: `multi-tenancy-workflow/README.md`
- Create: `multi-tenancy-workflow/index.html`
- Create: `multi-tenancy-workflow/styles.css`
- Create: `multi-tenancy-workflow/data.js`
- Create: `multi-tenancy-workflow/app.js`

- [ ] **Step 1: Create folder and README**

Create `multi-tenancy-workflow/README.md`:

```markdown
# Multi-Tenancy Workflow — Animated Explainer

A self-contained animated HTML page explaining how multi-tenancy is
implemented in this Synapse fork. Designed for developer onboarding.

## What it shows

A `GET /_matrix/client/versions` request with `Host: acme.localhost`
flowing through the request lifecycle:

1. Client → Nginx (Host header preserved)
2. Synapse `TenantRouter` resolves the tenant
3. `TenantConfig` loaded and bound to a `contextvars.ContextVar`
4. Postgres `search_path` switched to the tenant's schema
5. `MultiTenantKeyring` returns the tenant's signing key
6. Media path resolves under the tenant's directory
7. Response leaves tagged with the tenant

Three synchronized scenes (switchable via tabs) show this same lifecycle
from different angles: a flow diagram, a layered stack, and a
diagram-plus-log split screen.

## Viewing

Just open `index.html` in a modern browser:

    xdg-open index.html      # Linux
    open index.html          # macOS

If your browser blocks `file://` module imports, serve over HTTP:

    python -m http.server -d multi-tenancy-workflow 8000
    # then open http://localhost:8000/

No build step, no npm, no network.

## Keyboard

- Space — play / pause
- ← / → — previous / next stage
- 1 / 2 / 3 — switch scene
- R — restart
```

- [ ] **Step 2: Create minimal `index.html`**

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Multi-Tenant Synapse — Request Lifecycle</title>
  <link rel="stylesheet" href="styles.css" />
</head>
<body>
  <header class="chrome">
    <h1>Multi-Tenant Synapse <span class="subtitle">— Request Lifecycle</span></h1>
    <nav class="tabs" role="tablist">
      <button class="tab active" data-scene="A" role="tab">A · Flow</button>
      <button class="tab" data-scene="B" role="tab">B · Stack</button>
      <button class="tab" data-scene="C" role="tab">C · Log</button>
    </nav>
    <label class="parallel-toggle">
      <input type="checkbox" id="parallel-mode" />
      <span>Parallel tenants</span>
    </label>
  </header>

  <main id="scene-mount" class="scene-mount" aria-live="polite"></main>

  <footer class="transport">
    <button id="play-pause" aria-label="Play / pause">▶</button>
    <button id="restart" aria-label="Restart">↻</button>
    <div class="scrubber" id="scrubber"></div>
    <div class="stage-label">
      <span id="stage-name">Idle</span>
      <span class="stage-counter"><span id="stage-index">0</span>/10</span>
    </div>
  </footer>

  <script type="module" src="data.js"></script>
  <script type="module" src="app.js"></script>
</body>
</html>
```

- [ ] **Step 3: Create minimal `styles.css`**

```css
:root {
  --bg:        #0d1117;
  --panel:     #161b22;
  --border:    #30363d;
  --text:      #c9d1d9;
  --text-dim:  #8b949e;
  --acme:      #22d3ee;
  --corp:      #f59e0b;
  --startup:   #a78bfa;
  --accent:    var(--acme);
  --mono: ui-monospace, "JetBrains Mono", "Fira Code", Menlo, monospace;
}

* { box-sizing: border-box; }

html, body {
  margin: 0;
  height: 100%;
  background: var(--bg);
  color: var(--text);
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
}

body {
  display: grid;
  grid-template-rows: auto 1fr auto;
}

.chrome {
  display: flex;
  align-items: center;
  gap: 1.5rem;
  padding: 0.75rem 1.25rem;
  border-bottom: 1px solid var(--border);
  background: var(--panel);
}

.chrome h1 {
  font-size: 1rem;
  margin: 0;
  font-weight: 600;
}

.chrome .subtitle {
  color: var(--text-dim);
  font-weight: 400;
}

.tabs { display: flex; gap: 0.25rem; margin-left: auto; }

.tab {
  background: transparent;
  color: var(--text-dim);
  border: 1px solid var(--border);
  padding: 0.35rem 0.75rem;
  border-radius: 6px;
  cursor: pointer;
  font: inherit;
}

.tab.active {
  color: var(--text);
  background: #21262d;
  border-color: var(--accent);
}

.parallel-toggle {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  color: var(--text-dim);
  font-size: 0.85rem;
  user-select: none;
}

.scene-mount {
  position: relative;
  overflow: hidden;
  padding: 1rem;
}

.transport {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  padding: 0.5rem 1.25rem;
  border-top: 1px solid var(--border);
  background: var(--panel);
}

.transport button {
  background: #21262d;
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: 6px;
  width: 2rem;
  height: 2rem;
  cursor: pointer;
  font-size: 0.9rem;
}

.scrubber {
  flex: 1;
  display: flex;
  gap: 4px;
  height: 8px;
}

.scrubber .tick {
  flex: 1;
  background: #21262d;
  border-radius: 2px;
  cursor: pointer;
  transition: background 200ms;
}

.scrubber .tick.past { background: var(--accent); opacity: 0.5; }
.scrubber .tick.current { background: var(--accent); }

.stage-label {
  font-family: var(--mono);
  font-size: 0.8rem;
  color: var(--text-dim);
  min-width: 14rem;
  text-align: right;
}

.stage-counter { margin-left: 0.5rem; color: var(--text); }

@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: 0.01ms !important;
    transition-duration: 0.01ms !important;
  }
}
```

- [ ] **Step 4: Create stub `data.js` and `app.js`**

`data.js`:

```js
// Single source of narrative truth. Filled in Task 2.
export const STAGES = [];
export const TENANTS = {
  acme: { id: "acme", host: "acme.localhost", schema: "tenant_acme", color: "#22d3ee" },
  corp: { id: "corp", host: "corp.localhost", schema: "tenant_corp", color: "#f59e0b" },
  startup: { id: "startup", host: "startup.localhost", schema: "tenant_startup", color: "#a78bfa" },
};
export const PARALLEL_OFFSET_MS = 200;
```

`app.js`:

```js
import { STAGES, TENANTS } from "./data.js";

console.log("multi-tenancy-workflow loaded", { stages: STAGES.length, tenants: Object.keys(TENANTS) });
```

- [ ] **Step 5: Verify the skeleton loads**

Open `multi-tenancy-workflow/index.html` in a browser. Expected:
- Page renders with header, empty middle, transport bar.
- DevTools console shows: `multi-tenancy-workflow loaded { stages: 0, tenants: [...] }`
- No errors.

If `file://` blocks the module imports, run `python -m http.server -d multi-tenancy-workflow 8000` and open `http://localhost:8000/`.

- [ ] **Step 6: Commit**

```bash
git add multi-tenancy-workflow/
git commit -m "feat(workflow): scaffold multi-tenancy-workflow page"
```

---

### Task 2: Stage data ("the script")

**Files:**
- Modify: `multi-tenancy-workflow/data.js`

- [ ] **Step 1: Define the full STAGES array**

Replace the contents of `data.js` with:

```js
// Single source of narrative truth.
// Each stage has start/end ms (relative to playback start), label,
// fileRef (real repo path), code (Python snippet), and logLines.

export const TENANTS = {
  acme: { id: "acme", host: "acme.localhost", schema: "tenant_acme", color: "#22d3ee" },
  corp: { id: "corp", host: "corp.localhost", schema: "tenant_corp", color: "#f59e0b" },
  startup: { id: "startup", host: "startup.localhost", schema: "tenant_startup", color: "#a78bfa" },
};

export const PARALLEL_OFFSET_MS = 200;

export const STAGES = [
  {
    id: "client",
    label: "Client request",
    component: "client",
    layer: "network",
    startMs: 0,
    endMs: 1500,
    fileRef: null,
    code: `GET /_matrix/client/versions HTTP/1.1
Host: acme.localhost
Accept: application/json`,
    codeLang: "http",
    logLines: ["req GET /_matrix/client/versions"],
  },
  {
    id: "proxy",
    label: "Reverse proxy (nginx)",
    component: "nginx",
    layer: "network",
    startMs: 1500,
    endMs: 3000,
    fileRef: "docker-multitenant/nginx/nginx.conf",
    code: `location /_matrix {
    proxy_pass http://synapse:8008;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $remote_addr;
}`,
    codeLang: "nginx",
    logLines: ["nginx forwarded Host header"],
  },
  {
    id: "router",
    label: "Synapse · TenantRouter",
    component: "router",
    layer: "routing",
    startMs: 3000,
    endMs: 5000,
    fileRef: "synapse/tenant_registry.py",
    code: `def get_tenant_by_host(host: str) -> TenantConfig | None:
    """Look up the tenant for an incoming Host header."""
    tenant = self._by_server_name.get(host)
    if tenant is None:
        tenant = self._by_alias.get(host)
    return tenant`,
    codeLang: "python",
    logLines: ["tenant resolved from Host header"],
  },
  {
    id: "config",
    label: "TenantConfig resolved",
    component: "router",
    layer: "routing",
    startMs: 5000,
    endMs: 6500,
    fileRef: "synapse/config/tenants.py",
    code: `@dataclass
class TenantConfig:
    server_name: str          # "acme.localhost"
    database_schema: str      # "tenant_acme"
    signing_key_path: str     # ".../keys/acme.signing.key"
    media_store_path: str     # ".../media/acme"
    registration_enabled: bool
    federation_enabled: bool`,
    codeLang: "python",
    logLines: ["TenantConfig{server_name=acme.localhost, schema=tenant_acme}"],
  },
  {
    id: "context",
    label: "Bind tenant to ContextVar",
    component: "context",
    layer: "context",
    startMs: 6500,
    endMs: 8000,
    fileRef: "synapse/tenant_context.py",
    code: `_current_tenant: ContextVar[TenantConfig | None] = ContextVar(
    "current_tenant", default=None,
)

def set_current_tenant(tenant: TenantConfig) -> None:
    _current_tenant.set(tenant)`,
    codeLang: "python",
    logLines: ["context bound: tenant_acme"],
  },
  {
    id: "schema",
    label: "Postgres search_path switch",
    component: "postgres",
    layer: "storage",
    startMs: 8000,
    endMs: 10000,
    fileRef: "synapse/storage/database.py",
    code: `tenant = get_current_tenant()
if tenant is not None:
    txn.execute(
        f'SET search_path TO {tenant.database_schema}, public'
    )`,
    codeLang: "python",
    logLines: ["db: SET search_path TO tenant_acme, public"],
  },
  {
    id: "keyring",
    label: "MultiTenantKeyring lookup",
    component: "keyring",
    layer: "storage",
    startMs: 10000,
    endMs: 11500,
    fileRef: "synapse/crypto/multitenant_keyring.py",
    code: `def get_signing_key(self, server_name: str) -> SigningKey:
    """Return the signing key for a tenant's server_name."""
    return self._keys_by_server[server_name]`,
    codeLang: "python",
    logLines: ["keyring: loaded ed25519:auto acme.signing.key"],
  },
  {
    id: "media",
    label: "Media path resolved",
    component: "media",
    layer: "storage",
    startMs: 11500,
    endMs: 13000,
    fileRef: "synapse/media/multitenant_filepath.py",
    code: `def resolve(self, *parts: str) -> str:
    tenant = get_current_tenant()
    root = tenant.media_store_path if tenant else self._default_root
    return os.path.join(root, *parts)`,
    codeLang: "python",
    logLines: ["media root: /var/synapse/media/acme"],
  },
  {
    id: "response",
    label: "Response",
    component: "client",
    layer: "response",
    startMs: 13000,
    endMs: 14500,
    fileRef: null,
    code: `HTTP/1.1 200 OK
Content-Type: application/json

{ "versions": ["v1.13", "v1.14"] }`,
    codeLang: "http",
    logLines: ["200 OK"],
  },
  {
    id: "recap",
    label: "Isolation recap",
    component: "all",
    layer: "response",
    startMs: 14500,
    endMs: 16500,
    fileRef: null,
    code: `# Three tenants, one process, zero shared state:
#   acme.localhost    →  tenant_acme
#   corp.localhost    →  tenant_corp
#   startup.localhost →  tenant_startup`,
    codeLang: "python",
    logLines: ["all tenants isolated · 1 process"],
  },
];

export const TOTAL_DURATION_MS = STAGES[STAGES.length - 1].endMs;
```

- [ ] **Step 2: Verify in browser**

Reload the page. Expected console output:

```
multi-tenancy-workflow loaded { stages: 10, tenants: [ "acme", "corp", "startup" ] }
```

- [ ] **Step 3: Commit**

```bash
git add multi-tenancy-workflow/data.js
git commit -m "feat(workflow): define request lifecycle stages"
```

---

### Task 3: Timeline engine + transport bar

**Files:**
- Modify: `multi-tenancy-workflow/app.js`

- [ ] **Step 1: Implement the Timeline class and transport wiring**

Replace `app.js` with:

```js
import { STAGES, TENANTS, TOTAL_DURATION_MS, PARALLEL_OFFSET_MS } from "./data.js";

// ---------------------------------------------------------------------------
// Timeline: drives currentTimeMs, fires stageChange events.
// ---------------------------------------------------------------------------

class Timeline extends EventTarget {
  constructor(stages, totalMs) {
    super();
    this.stages = stages;
    this.totalMs = totalMs;
    this.currentMs = 0;
    this.playing = false;
    this.lastFrameTs = 0;
    this.currentStageIndex = -1;
  }

  play() {
    if (this.playing) return;
    this.playing = true;
    this.lastFrameTs = performance.now();
    requestAnimationFrame((ts) => this._tick(ts));
    this.dispatchEvent(new Event("playStateChange"));
  }

  pause() {
    this.playing = false;
    this.dispatchEvent(new Event("playStateChange"));
  }

  togglePlay() {
    this.playing ? this.pause() : this.play();
  }

  restart() {
    this.seek(0);
    this.play();
  }

  seek(ms) {
    this.currentMs = Math.max(0, Math.min(this.totalMs, ms));
    this._updateStage(true);
  }

  jumpToStage(index) {
    const stage = this.stages[index];
    if (stage) this.seek(stage.startMs);
  }

  _tick(ts) {
    if (!this.playing) return;
    const dt = ts - this.lastFrameTs;
    this.lastFrameTs = ts;
    this.currentMs += dt;
    if (this.currentMs >= this.totalMs) {
      this.currentMs = this.totalMs;
      this.pause();
    }
    this._updateStage(false);
    if (this.playing) requestAnimationFrame((ts2) => this._tick(ts2));
  }

  _updateStage(force) {
    const idx = this._stageIndexAt(this.currentMs);
    if (force || idx !== this.currentStageIndex) {
      this.currentStageIndex = idx;
      const stage = this.stages[idx] || null;
      this.dispatchEvent(new CustomEvent("stageChange", {
        detail: { stage, index: idx, currentMs: this.currentMs },
      }));
    }
    this.dispatchEvent(new CustomEvent("tick", {
      detail: { currentMs: this.currentMs },
    }));
  }

  _stageIndexAt(ms) {
    for (let i = this.stages.length - 1; i >= 0; i--) {
      if (ms >= this.stages[i].startMs) return i;
    }
    return 0;
  }
}

// ---------------------------------------------------------------------------
// Transport bar: play/pause, restart, scrubber, stage label.
// ---------------------------------------------------------------------------

function mountTransport(timeline) {
  const playPauseBtn = document.getElementById("play-pause");
  const restartBtn = document.getElementById("restart");
  const scrubber = document.getElementById("scrubber");
  const stageName = document.getElementById("stage-name");
  const stageIndex = document.getElementById("stage-index");

  // Build scrubber ticks (one per stage).
  scrubber.innerHTML = "";
  STAGES.forEach((stage, i) => {
    const tick = document.createElement("button");
    tick.className = "tick";
    tick.title = stage.label;
    tick.addEventListener("click", () => {
      timeline.pause();
      timeline.jumpToStage(i);
    });
    scrubber.appendChild(tick);
  });

  playPauseBtn.addEventListener("click", () => timeline.togglePlay());
  restartBtn.addEventListener("click", () => timeline.restart());

  timeline.addEventListener("playStateChange", () => {
    playPauseBtn.textContent = timeline.playing ? "⏸" : "▶";
  });

  timeline.addEventListener("stageChange", (e) => {
    const { stage, index } = e.detail;
    if (!stage) return;
    stageName.textContent = stage.label;
    stageIndex.textContent = String(index + 1);
    [...scrubber.children].forEach((tick, i) => {
      tick.classList.toggle("past", i < index);
      tick.classList.toggle("current", i === index);
    });
  });
}

// ---------------------------------------------------------------------------
// Keyboard shortcuts
// ---------------------------------------------------------------------------

function mountKeyboard(timeline, sceneRegistry) {
  document.addEventListener("keydown", (e) => {
    if (e.target.tagName === "INPUT") return;
    switch (e.key) {
      case " ":
        e.preventDefault();
        timeline.togglePlay();
        break;
      case "ArrowRight":
        timeline.pause();
        timeline.jumpToStage(Math.min(STAGES.length - 1, timeline.currentStageIndex + 1));
        break;
      case "ArrowLeft":
        timeline.pause();
        timeline.jumpToStage(Math.max(0, timeline.currentStageIndex - 1));
        break;
      case "r": case "R":
        timeline.restart();
        break;
      case "1": sceneRegistry.activate("A"); break;
      case "2": sceneRegistry.activate("B"); break;
      case "3": sceneRegistry.activate("C"); break;
    }
  });
}

// ---------------------------------------------------------------------------
// Scene registry stub (scenes added in later tasks)
// ---------------------------------------------------------------------------

class SceneRegistry {
  constructor(timeline, mountEl) {
    this.timeline = timeline;
    this.mountEl = mountEl;
    this.scenes = new Map();
    this.active = null;
  }
  register(id, sceneCtor) {
    this.scenes.set(id, sceneCtor);
  }
  activate(id) {
    if (!this.scenes.has(id)) return;
    if (this.active) this.active.unmount();
    this.mountEl.innerHTML = "";
    const SceneCls = this.scenes.get(id);
    this.active = new SceneCls(this.timeline);
    this.active.mount(this.mountEl);
    document.querySelectorAll(".tab").forEach((t) => {
      t.classList.toggle("active", t.dataset.scene === id);
    });
    // Replay current stage so the new scene snaps into place.
    this.timeline._updateStage(true);
  }
}

// ---------------------------------------------------------------------------
// Bootstrap
// ---------------------------------------------------------------------------

const timeline = new Timeline(STAGES, TOTAL_DURATION_MS);
const registry = new SceneRegistry(timeline, document.getElementById("scene-mount"));

mountTransport(timeline);
mountKeyboard(timeline, registry);

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => registry.activate(tab.dataset.scene));
});

// Self-checks (visible in DevTools)
console.assert(STAGES.length === 10, "expected 10 stages");
console.assert(TOTAL_DURATION_MS === 16500, "expected 16500ms total");

// Make timeline reachable from DevTools for debugging.
window.__timeline = timeline;
window.__registry = registry;

console.log("multi-tenancy-workflow ready");
```

- [ ] **Step 2: Verify the engine in the browser**

Reload. Expected:
- Console: `multi-tenancy-workflow ready` and no failed assertions.
- Click ▶: button changes to ⏸. The stage label cycles through "Client request" → "Reverse proxy (nginx)" → … → "Isolation recap" over ~16s.
- Scrubber ticks light up as stages advance.
- Click any tick → label jumps and playback pauses.
- ↻ restarts.
- Spacebar toggles play/pause.

(Scenes are not mounted yet — the middle area is empty. Fixed in Task 4.)

- [ ] **Step 3: Commit**

```bash
git add multi-tenancy-workflow/app.js
git commit -m "feat(workflow): timeline engine + transport bar"
```

---

### Task 4: Scene A — animated request flow diagram

**Files:**
- Modify: `multi-tenancy-workflow/app.js`
- Modify: `multi-tenancy-workflow/styles.css`

- [ ] **Step 1: Add Scene A styles**

Append to `styles.css`:

```css
/* ----- Scene A: flow diagram ----- */

.scene-a {
  display: grid;
  grid-template-columns: 1fr 22rem;
  gap: 1rem;
  height: 100%;
}

.scene-a .diagram {
  background:
    radial-gradient(circle at center, #161b22 0, #0d1117 70%),
    repeating-linear-gradient(0deg, transparent 0 19px, #1f242c 19px 20px),
    repeating-linear-gradient(90deg, transparent 0 19px, #1f242c 19px 20px);
  border: 1px solid var(--border);
  border-radius: 8px;
  position: relative;
  overflow: hidden;
}

.scene-a svg { width: 100%; height: 100%; }

.scene-a .node rect {
  fill: #161b22;
  stroke: #30363d;
  stroke-width: 1.5;
  transition: stroke 250ms, filter 250ms;
}

.scene-a .node text {
  fill: var(--text-dim);
  font: 11px var(--mono);
  text-anchor: middle;
  pointer-events: none;
  transition: fill 250ms;
}

.scene-a .node.active rect {
  stroke: var(--accent);
  filter: drop-shadow(0 0 6px var(--accent));
}
.scene-a .node.active text { fill: var(--text); }

.scene-a .link {
  fill: none;
  stroke: #30363d;
  stroke-width: 1.5;
  stroke-dasharray: 4 3;
}
.scene-a .link.flowing {
  stroke: var(--accent);
  animation: dash 800ms linear infinite;
}

@keyframes dash {
  to { stroke-dashoffset: -14; }
}

.scene-a .packet {
  fill: var(--accent);
  filter: drop-shadow(0 0 6px var(--accent));
  transition: cx 400ms ease, cy 400ms ease;
}

.scene-a .side {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 1rem;
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
  overflow: hidden;
}

.scene-a .side h3 {
  margin: 0;
  font-size: 0.85rem;
  color: var(--text);
  letter-spacing: 0.02em;
}

.scene-a .side .file-ref {
  font: 11px var(--mono);
  color: var(--text-dim);
  word-break: break-all;
}

.scene-a .side pre {
  margin: 0;
  font: 12px var(--mono);
  color: var(--text);
  background: #0d1117;
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 0.65rem 0.75rem;
  overflow: auto;
  flex: 1;
  white-space: pre-wrap;
}

/* code highlight */
.tok-kw { color: #ff7b72; }
.tok-str { color: #a5d6ff; }
.tok-com { color: #8b949e; font-style: italic; }
.tok-num { color: #79c0ff; }
.tok-fn  { color: #d2a8ff; }
```

- [ ] **Step 2: Add a tiny code highlighter and Scene A class to `app.js`**

Add to `app.js` *before* the bootstrap section:

```js
// ---------------------------------------------------------------------------
// Tiny regex-based syntax highlighter (Python-flavored, "good enough")
// ---------------------------------------------------------------------------

const PY_KEYWORDS = new Set([
  "def","class","return","if","else","elif","for","while","import","from",
  "as","with","in","is","not","and","or","None","True","False","pass",
  "raise","try","except","finally","lambda","yield","async","await","self",
]);

function escapeHtml(s) {
  return s.replace(/[&<>]/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
}

function highlight(code, lang) {
  const escaped = escapeHtml(code);
  if (lang !== "python") {
    // Minimal: highlight strings + comments only.
    return escaped
      .replace(/(#[^\n]*)/g, '<span class="tok-com">$1</span>')
      .replace(/("[^"\n]*"|'[^'\n]*')/g, '<span class="tok-str">$1</span>');
  }
  return escaped
    .replace(/(#[^\n]*)/g, '<span class="tok-com">$1</span>')
    .replace(/("""[\s\S]*?"""|'''[\s\S]*?'''|"[^"\n]*"|'[^'\n]*')/g,
             '<span class="tok-str">$1</span>')
    .replace(/\b([A-Za-z_][A-Za-z0-9_]*)\b/g, (m, w) => {
      if (PY_KEYWORDS.has(w)) return `<span class="tok-kw">${w}</span>`;
      return m;
    });
}

// ---------------------------------------------------------------------------
// Scene A — animated flow diagram
// ---------------------------------------------------------------------------

const SVG_NS = "http://www.w3.org/2000/svg";

// Layout (in SVG viewport units, 1000x500).
const NODES = {
  client:   { x: 60,  y: 240, w: 110, h: 60, label: "Client" },
  nginx:    { x: 220, y: 240, w: 110, h: 60, label: "Nginx" },
  router:   { x: 400, y: 200, w: 160, h: 60, label: "TenantRouter" },
  context:  { x: 400, y: 290, w: 160, h: 60, label: "ContextVar" },
  postgres: { x: 640, y: 130, w: 180, h: 60, label: "Postgres schemas" },
  keyring:  { x: 640, y: 230, w: 180, h: 60, label: "MultiTenantKeyring" },
  media:    { x: 640, y: 330, w: 180, h: 60, label: "Media FS" },
};

const LINKS = [
  ["client", "nginx"],
  ["nginx", "router"],
  ["router", "context"],
  ["context", "postgres"],
  ["context", "keyring"],
  ["context", "media"],
];

class SceneA {
  constructor(timeline) {
    this.timeline = timeline;
    this._onStage = (e) => this.onStageChange(e.detail.stage);
  }

  mount(root) {
    root.innerHTML = "";
    root.classList.add("scene-a");
    root.classList.remove("scene-b", "scene-c");

    this.diagramEl = document.createElement("div");
    this.diagramEl.className = "diagram";

    this.svg = document.createElementNS(SVG_NS, "svg");
    this.svg.setAttribute("viewBox", "0 0 1000 500");
    this.svg.setAttribute("preserveAspectRatio", "xMidYMid meet");

    // Links
    this.linkEls = {};
    LINKS.forEach(([a, b]) => {
      const path = document.createElementNS(SVG_NS, "path");
      const A = NODES[a], B = NODES[b];
      const x1 = A.x + A.w, y1 = A.y + A.h / 2;
      const x2 = B.x,       y2 = B.y + B.h / 2;
      const mx = (x1 + x2) / 2;
      path.setAttribute("d", `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`);
      path.setAttribute("class", "link");
      path.dataset.from = a;
      path.dataset.to = b;
      this.svg.appendChild(path);
      this.linkEls[`${a}->${b}`] = path;
    });

    // Nodes
    this.nodeEls = {};
    Object.entries(NODES).forEach(([id, n]) => {
      const g = document.createElementNS(SVG_NS, "g");
      g.setAttribute("class", "node");
      g.dataset.node = id;
      const rect = document.createElementNS(SVG_NS, "rect");
      rect.setAttribute("x", n.x);
      rect.setAttribute("y", n.y);
      rect.setAttribute("width", n.w);
      rect.setAttribute("height", n.h);
      rect.setAttribute("rx", 8);
      const text = document.createElementNS(SVG_NS, "text");
      text.setAttribute("x", n.x + n.w / 2);
      text.setAttribute("y", n.y + n.h / 2 + 4);
      text.textContent = n.label;
      g.appendChild(rect);
      g.appendChild(text);
      this.svg.appendChild(g);
      this.nodeEls[id] = g;
    });

    // Packet
    this.packet = document.createElementNS(SVG_NS, "circle");
    this.packet.setAttribute("class", "packet");
    this.packet.setAttribute("r", "7");
    this.packet.setAttribute("cx", NODES.client.x + NODES.client.w / 2);
    this.packet.setAttribute("cy", NODES.client.y + NODES.client.h / 2);
    this.svg.appendChild(this.packet);

    this.diagramEl.appendChild(this.svg);

    // Side panel
    this.sideEl = document.createElement("aside");
    this.sideEl.className = "side";
    this.sideEl.innerHTML = `
      <h3 id="a-stage">—</h3>
      <div class="file-ref" id="a-fileref">—</div>
      <pre id="a-code"><code></code></pre>
    `;

    root.appendChild(this.diagramEl);
    root.appendChild(this.sideEl);

    this.timeline.addEventListener("stageChange", this._onStage);
  }

  onStageChange(stage) {
    if (!stage) return;

    // Highlight active node
    Object.entries(this.nodeEls).forEach(([id, el]) => {
      el.classList.toggle("active", id === stage.component || stage.component === "all");
    });

    // Highlight inbound links
    Object.entries(this.linkEls).forEach(([key, el]) => {
      const [, to] = key.split("->");
      el.classList.toggle("flowing", to === stage.component);
    });

    // Move packet to active node center
    const target = NODES[stage.component] || NODES.client;
    this.packet.setAttribute("cx", target.x + target.w / 2);
    this.packet.setAttribute("cy", target.y + target.h / 2);

    // Side panel
    this.sideEl.querySelector("#a-stage").textContent = stage.label;
    const fileEl = this.sideEl.querySelector("#a-fileref");
    fileEl.textContent = stage.fileRef || "—";
    const codeEl = this.sideEl.querySelector("#a-code code");
    codeEl.innerHTML = highlight(stage.code, stage.codeLang);
  }

  unmount() {
    this.timeline.removeEventListener("stageChange", this._onStage);
  }
}
```

Then in the bootstrap section, register and activate Scene A:

```js
registry.register("A", SceneA);
registry.activate("A");
timeline.play();
```

(Add these three lines just before the final `console.log("multi-tenancy-workflow ready");`)

- [ ] **Step 2b: Note on the `component: "all"` stage**

The "Isolation recap" stage uses `component: "all"`. In Scene A this lights up every node simultaneously, which is exactly the intended visual.

- [ ] **Step 3: Verify Scene A**

Reload. Expected:
- Page auto-plays. The flow diagram is visible with all nodes drawn.
- The packet visibly hops from Client → Nginx → TenantRouter → ContextVar → Postgres → Keyring → Media → Client.
- The active node has a cyan glow; inactive nodes are dim.
- The side panel updates with stage label, file path, and a syntax-highlighted code snippet.
- During "Isolation recap" all nodes glow.
- No console errors.

- [ ] **Step 4: Commit**

```bash
git add multi-tenancy-workflow/app.js multi-tenancy-workflow/styles.css
git commit -m "feat(workflow): scene A — animated flow diagram"
```

---

### Task 5: Scene B — layered stack view

**Files:**
- Modify: `multi-tenancy-workflow/app.js`
- Modify: `multi-tenancy-workflow/styles.css`

- [ ] **Step 1: Add Scene B styles**

Append to `styles.css`:

```css
/* ----- Scene B: layered stack ----- */

.scene-b {
  display: grid;
  grid-template-columns: 1fr 22rem;
  gap: 1rem;
  height: 100%;
}

.scene-b .stack {
  position: relative;
  background: #0d1117;
  border: 1px solid var(--border);
  border-radius: 8px;
  display: flex;
  align-items: center;
  justify-content: center;
  perspective: 800px;
  overflow: hidden;
}

.scene-b .layers {
  display: flex;
  flex-direction: column;
  gap: 0.6rem;
  transform: rotateX(18deg) rotateZ(-2deg);
  transform-origin: center;
}

.scene-b .layer {
  width: 26rem;
  padding: 1.1rem 1.4rem;
  border-radius: 10px;
  background: linear-gradient(180deg, #161b22, #0e1217);
  border: 1px solid var(--border);
  color: var(--text-dim);
  font: 13px var(--mono);
  box-shadow: 0 6px 0 #0a0e13;
  transition: transform 350ms ease, border-color 350ms, color 350ms, box-shadow 350ms;
}

.scene-b .layer.active {
  border-color: var(--accent);
  color: var(--text);
  transform: translateZ(40px) scale(1.02);
  box-shadow: 0 10px 24px rgba(34,211,238,0.2), 0 6px 0 #0a0e13;
}

.scene-b .layer .label {
  font-size: 0.7rem;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--text-dim);
  margin-bottom: 0.25rem;
}

.scene-b .side {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 1rem;
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
  overflow: hidden;
}

.scene-b .side h3 { margin: 0; font-size: 0.85rem; }
.scene-b .side .file-ref { font: 11px var(--mono); color: var(--text-dim); }
.scene-b .side pre {
  margin: 0;
  font: 12px var(--mono);
  background: #0d1117;
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 0.65rem 0.75rem;
  overflow: auto;
  flex: 1;
  white-space: pre-wrap;
}
```

- [ ] **Step 2: Add Scene B class to `app.js`**

Add after Scene A's class definition:

```js
// ---------------------------------------------------------------------------
// Scene B — layered stack
// ---------------------------------------------------------------------------

const LAYERS = [
  { id: "network",  label: "Network",        sub: "Client · Nginx" },
  { id: "routing",  label: "Routing",        sub: "TenantRouter · TenantConfig" },
  { id: "context",  label: "Tenant Context", sub: "contextvars.ContextVar" },
  { id: "storage",  label: "Storage",        sub: "Postgres · Keyring · Media" },
  { id: "response", label: "Response",       sub: "Tagged with tenant" },
];

class SceneB {
  constructor(timeline) {
    this.timeline = timeline;
    this._onStage = (e) => this.onStageChange(e.detail.stage);
  }

  mount(root) {
    root.innerHTML = "";
    root.classList.add("scene-b");
    root.classList.remove("scene-a", "scene-c");

    const stack = document.createElement("div");
    stack.className = "stack";
    const layers = document.createElement("div");
    layers.className = "layers";

    this.layerEls = {};
    LAYERS.forEach((L) => {
      const el = document.createElement("div");
      el.className = "layer";
      el.dataset.layer = L.id;
      el.innerHTML = `
        <div class="label">${L.label}</div>
        <div>${L.sub}</div>
      `;
      layers.appendChild(el);
      this.layerEls[L.id] = el;
    });
    stack.appendChild(layers);

    this.sideEl = document.createElement("aside");
    this.sideEl.className = "side";
    this.sideEl.innerHTML = `
      <h3 id="b-stage">—</h3>
      <div class="file-ref" id="b-fileref">—</div>
      <pre id="b-code"><code></code></pre>
    `;

    root.appendChild(stack);
    root.appendChild(this.sideEl);

    this.timeline.addEventListener("stageChange", this._onStage);
  }

  onStageChange(stage) {
    if (!stage) return;
    Object.entries(this.layerEls).forEach(([id, el]) => {
      el.classList.toggle("active", id === stage.layer);
    });
    this.sideEl.querySelector("#b-stage").textContent = stage.label;
    this.sideEl.querySelector("#b-fileref").textContent = stage.fileRef || "—";
    this.sideEl.querySelector("#b-code code").innerHTML = highlight(stage.code, stage.codeLang);
  }

  unmount() {
    this.timeline.removeEventListener("stageChange", this._onStage);
  }
}
```

Register it in bootstrap (right after `registry.register("A", SceneA);`):

```js
registry.register("B", SceneB);
```

- [ ] **Step 3: Verify Scene B**

Reload. Click the "B · Stack" tab (or press `2`). Expected:
- The five layers are drawn as a slightly tilted 3D stack.
- As the timeline plays, exactly one layer at a time pops forward and brightens (Network → Routing → Context → Storage → Response).
- Side panel shows the same stage info as Scene A.
- Switching back to A mid-playback resumes A in the right state.

- [ ] **Step 4: Commit**

```bash
git add multi-tenancy-workflow/app.js multi-tenancy-workflow/styles.css
git commit -m "feat(workflow): scene B — layered stack"
```

---

### Task 6: Scene C — diagram + live log

**Files:**
- Modify: `multi-tenancy-workflow/app.js`
- Modify: `multi-tenancy-workflow/styles.css`

- [ ] **Step 1: Add Scene C styles**

Append to `styles.css`:

```css
/* ----- Scene C: split screen with log ----- */

.scene-c {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 1rem;
  height: 100%;
}

.scene-c .mini-diagram {
  background: #0d1117;
  border: 1px solid var(--border);
  border-radius: 8px;
  position: relative;
}

.scene-c .mini-diagram svg { width: 100%; height: 100%; }

.scene-c .terminal {
  background: #0a0e13;
  border: 1px solid var(--border);
  border-radius: 8px;
  font: 12px var(--mono);
  color: var(--text);
  padding: 0.85rem 1rem;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}

.scene-c .terminal-header {
  color: var(--text-dim);
  font-size: 0.7rem;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  margin-bottom: 0.6rem;
  border-bottom: 1px solid var(--border);
  padding-bottom: 0.4rem;
}

.scene-c .log {
  list-style: none;
  margin: 0;
  padding: 0;
  flex: 1;
  overflow-y: auto;
}

.scene-c .log li {
  white-space: pre-wrap;
  padding: 1px 0;
  opacity: 0;
  transform: translateY(4px);
  animation: logIn 250ms ease forwards;
}

@keyframes logIn {
  to { opacity: 1; transform: translateY(0); }
}

.scene-c .log .tag {
  display: inline-block;
  min-width: 14rem;
  color: var(--accent);
}

.scene-c .log .ts { color: var(--text-dim); margin-right: 0.5rem; }

/* Per-tenant log color via inline style */
```

- [ ] **Step 2: Add Scene C class to `app.js`**

```js
// ---------------------------------------------------------------------------
// Scene C — diagram + log
// ---------------------------------------------------------------------------

class SceneC {
  constructor(timeline) {
    this.timeline = timeline;
    this._onStage = (e) => this.onStageChange(e.detail.stage, e.detail.index);
    this._seenIndices = new Set();
  }

  mount(root) {
    root.innerHTML = "";
    root.classList.add("scene-c");
    root.classList.remove("scene-a", "scene-b");

    // Mini diagram (reuse SceneA layout, no packet)
    this.mini = document.createElement("div");
    this.mini.className = "mini-diagram";
    this.svg = document.createElementNS(SVG_NS, "svg");
    this.svg.setAttribute("viewBox", "0 0 1000 500");
    this.svg.setAttribute("preserveAspectRatio", "xMidYMid meet");

    LINKS.forEach(([a, b]) => {
      const A = NODES[a], B = NODES[b];
      const x1 = A.x + A.w, y1 = A.y + A.h / 2;
      const x2 = B.x,       y2 = B.y + B.h / 2;
      const mx = (x1 + x2) / 2;
      const path = document.createElementNS(SVG_NS, "path");
      path.setAttribute("d", `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`);
      path.setAttribute("class", "link");
      this.svg.appendChild(path);
    });

    this.nodeEls = {};
    Object.entries(NODES).forEach(([id, n]) => {
      const g = document.createElementNS(SVG_NS, "g");
      g.setAttribute("class", "node");
      const rect = document.createElementNS(SVG_NS, "rect");
      rect.setAttribute("x", n.x); rect.setAttribute("y", n.y);
      rect.setAttribute("width", n.w); rect.setAttribute("height", n.h);
      rect.setAttribute("rx", 8);
      const text = document.createElementNS(SVG_NS, "text");
      text.setAttribute("x", n.x + n.w / 2);
      text.setAttribute("y", n.y + n.h / 2 + 4);
      text.textContent = n.label;
      g.appendChild(rect); g.appendChild(text);
      this.svg.appendChild(g);
      this.nodeEls[id] = g;
    });
    this.mini.appendChild(this.svg);

    // Terminal
    this.term = document.createElement("div");
    this.term.className = "terminal";
    this.term.innerHTML = `
      <div class="terminal-header">synapse · request log</div>
      <ul class="log" id="c-log"></ul>
    `;
    this.logEl = this.term.querySelector("#c-log");

    root.appendChild(this.mini);
    root.appendChild(this.term);

    // Reuse Scene A's `.scene-a` SVG styles by also tagging the SVG container
    this.mini.classList.add("scene-a");

    this.timeline.addEventListener("stageChange", this._onStage);
  }

  onStageChange(stage, index) {
    if (!stage) return;

    // Highlight node
    Object.entries(this.nodeEls).forEach(([id, el]) => {
      el.classList.toggle("active", id === stage.component || stage.component === "all");
    });

    // Append log lines (one stage = one or more lines).
    // If user scrubbed backwards, reset.
    if (index < this._lastIndex) {
      this.logEl.innerHTML = "";
      this._seenIndices.clear();
    }
    this._lastIndex = index;

    if (this._seenIndices.has(index)) return;
    this._seenIndices.add(index);

    const tenants = window.__parallelMode
      ? [TENANTS.acme, TENANTS.corp]
      : [TENANTS.acme];

    tenants.forEach((t, i) => {
      stage.logLines.forEach((line) => {
        const li = document.createElement("li");
        const ts = new Date().toLocaleTimeString("en-GB", { hour12: false });
        li.style.animationDelay = `${i * 120}ms`;
        li.innerHTML =
          `<span class="ts">${ts}</span>` +
          `<span class="tag" style="color:${t.color}">[${t.host}]</span>` +
          escapeHtml(line);
        this.logEl.appendChild(li);
      });
    });
    this.logEl.scrollTop = this.logEl.scrollHeight;
  }

  unmount() {
    this.timeline.removeEventListener("stageChange", this._onStage);
  }
}
```

Register it in bootstrap:

```js
registry.register("C", SceneC);
```

- [ ] **Step 3: Verify Scene C**

Reload. Click the "C · Log" tab (or press `3`). Expected:
- Left: same component diagram, no traveling packet, the active component lights up.
- Right: a fake terminal where each stage adds one tenant-tagged log line that fades in, scrolling automatically.
- Restarting clears the log.
- Scrubbing backwards clears the log and rebuilds from current stage forward.

- [ ] **Step 4: Commit**

```bash
git add multi-tenancy-workflow/app.js multi-tenancy-workflow/styles.css
git commit -m "feat(workflow): scene C — diagram + live log"
```

---

### Task 7: Parallel-tenant mode + final polish

**Files:**
- Modify: `multi-tenancy-workflow/app.js`

- [ ] **Step 1: Wire the parallel-mode toggle**

In the bootstrap section of `app.js`, add:

```js
const parallelToggle = document.getElementById("parallel-mode");
window.__parallelMode = false;
parallelToggle.addEventListener("change", () => {
  window.__parallelMode = parallelToggle.checked;
  // Restart so new mode takes effect from a clean log.
  timeline.restart();
});
```

- [ ] **Step 2: Verify parallel mode**

Reload. Switch to Scene C. Tick "Parallel tenants". Expected:
- Timeline restarts.
- Each stage now writes two log lines: one cyan `[acme.localhost]`, one amber `[corp.localhost]`.
- The amber line is offset slightly later via the animation delay.
- Untick → restarts back to single-tenant.

(In Scenes A and B the parallel-mode toggle has no visible effect — the diagrams are tenant-agnostic. That is acceptable; the toggle's purpose is to dramatize isolation in the log view, which is where it lives.)

- [ ] **Step 3: Verify the full deliverable end to end**

Manual checklist:
- [ ] Open `index.html` directly via `file://` (or `python -m http.server -d multi-tenancy-workflow 8000`).
- [ ] Page renders with header + transport + Scene A active.
- [ ] Auto-plays through all 10 stages in ~16s.
- [ ] All three tabs work; switching mid-playback shows the new scene already at the right stage.
- [ ] Spacebar pauses/resumes; ←/→ steps; `R` restarts; `1`/`2`/`3` switch scenes.
- [ ] Scrubber ticks are clickable.
- [ ] Parallel-tenants toggle works in Scene C.
- [ ] No console errors or failed assertions.
- [ ] In OS reduced-motion mode (Linux: `gsettings set org.gnome.desktop.interface enable-animations false`; macOS: System Settings → Accessibility → Display → Reduce motion), animations stop but stage progression still works. Revert when done.
- [ ] Verify file refs in `data.js` actually point to existing files: `ls synapse/tenant_context.py synapse/tenant_registry.py synapse/config/tenants.py synapse/crypto/multitenant_keyring.py synapse/media/multitenant_filepath.py docker-multitenant/nginx/nginx.conf`

- [ ] **Step 4: Commit**

```bash
git add multi-tenancy-workflow/app.js
git commit -m "feat(workflow): parallel-tenant mode + final polish"
```

---

## Self-review notes

- **Spec coverage:** All 10 stages, 3 scenes, parallel mode, accessibility, keyboard, side panel with code/file refs, transport bar, no-build constraint — all covered.
- **File refs are real:** `synapse/storage/database.py` is the one path used in stage data that wasn't pre-verified during brainstorming. Task 7 step 3 includes a verification step. If `synapse/storage/database.py` doesn't exist, update `data.js` to point at the actual schema-switching site (likely under `synapse/storage/`); the code snippet remains illustrative either way.
- **Type / name consistency:** `Scene.mount/onStageChange/unmount` contract is identical across A/B/C. `STAGES` shape is consistent. `TENANTS` color values match `--acme`/`--corp`/`--startup` CSS vars.
- **No placeholders.**
