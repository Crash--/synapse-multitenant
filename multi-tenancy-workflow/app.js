import { STAGES, TENANTS, TOTAL_DURATION_MS } from "./data.js";

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
    if (this.currentMs >= this.totalMs) this.currentMs = 0;
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
    this.pause();
    this.currentMs = 0;
    this.currentStageIndex = -1;
    this._updateStage(true);
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
      this._updateStage(false);
      this.pause();
      return;
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
// Shared SVG layout (used by Scene A and Scene C)
// ---------------------------------------------------------------------------

const SVG_NS = "http://www.w3.org/2000/svg";

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

function buildDiagramSvg() {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 1000 500");
  svg.setAttribute("preserveAspectRatio", "xMidYMid meet");

  const linkEls = {};
  LINKS.forEach(([a, b]) => {
    const A = NODES[a], B = NODES[b];
    const x1 = A.x + A.w, y1 = A.y + A.h / 2;
    const x2 = B.x,       y2 = B.y + B.h / 2;
    const mx = (x1 + x2) / 2;
    const path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`);
    path.setAttribute("class", "link");
    path.dataset.from = a;
    path.dataset.to = b;
    svg.appendChild(path);
    linkEls[`${a}->${b}`] = path;
  });

  const nodeEls = {};
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
    svg.appendChild(g);
    nodeEls[id] = g;
  });

  return { svg, nodeEls, linkEls };
}

function applyActiveHighlight(nodeEls, linkEls, stage) {
  Object.entries(nodeEls).forEach(([id, el]) => {
    el.classList.toggle("active", id === stage.component || stage.component === "all");
  });
  if (linkEls) {
    Object.entries(linkEls).forEach(([key, el]) => {
      const [, to] = key.split("->");
      el.classList.toggle("flowing", to === stage.component);
    });
  }
}

// ---------------------------------------------------------------------------
// Scene A — animated flow diagram
// ---------------------------------------------------------------------------

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

    const built = buildDiagramSvg();
    this.svg = built.svg;
    this.nodeEls = built.nodeEls;
    this.linkEls = built.linkEls;

    // Packet
    this.packet = document.createElementNS(SVG_NS, "circle");
    this.packet.setAttribute("class", "packet");
    this.packet.setAttribute("r", "8");
    this.packet.setAttribute("cx", NODES.client.x + NODES.client.w / 2);
    this.packet.setAttribute("cy", NODES.client.y + NODES.client.h / 2);
    this.svg.appendChild(this.packet);

    this.diagramEl.appendChild(this.svg);

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
    applyActiveHighlight(this.nodeEls, this.linkEls, stage);

    const target = NODES[stage.component] || NODES.client;
    this.packet.setAttribute("cx", target.x + target.w / 2);
    this.packet.setAttribute("cy", target.y + target.h / 2);

    this.sideEl.querySelector("#a-stage").textContent = stage.label;
    this.sideEl.querySelector("#a-fileref").textContent = stage.fileRef || "—";
    this.sideEl.querySelector("#a-code code").innerHTML = highlight(stage.code, stage.codeLang);
  }

  unmount() {
    this.timeline.removeEventListener("stageChange", this._onStage);
  }
}

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

// ---------------------------------------------------------------------------
// Scene C — diagram + log
// ---------------------------------------------------------------------------

class SceneC {
  constructor(timeline) {
    this.timeline = timeline;
    this._onStage = (e) => this.onStageChange(e.detail.stage, e.detail.index);
    this._seenIndices = new Set();
    this._lastIndex = -1;
  }

  mount(root) {
    root.innerHTML = "";
    root.classList.add("scene-c");
    root.classList.remove("scene-a", "scene-b");

    this.mini = document.createElement("div");
    this.mini.className = "mini-diagram scene-a"; // borrow scene-a SVG styles
    const built = buildDiagramSvg();
    this.svg = built.svg;
    this.nodeEls = built.nodeEls;
    this.linkEls = built.linkEls;
    this.mini.appendChild(this.svg);

    this.term = document.createElement("div");
    this.term.className = "terminal";
    this.term.innerHTML = `
      <div class="terminal-header">synapse · request log</div>
      <ul class="log" id="c-log"></ul>
    `;
    this.logEl = this.term.querySelector("#c-log");

    root.appendChild(this.mini);
    root.appendChild(this.term);

    this._seenIndices = new Set();
    this._lastIndex = -1;

    this.timeline.addEventListener("stageChange", this._onStage);
  }

  onStageChange(stage, index) {
    if (!stage) return;
    applyActiveHighlight(this.nodeEls, this.linkEls, stage);

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

// ---------------------------------------------------------------------------
// Transport bar
// ---------------------------------------------------------------------------

function mountTransport(timeline) {
  const playPauseBtn = document.getElementById("play-pause");
  const restartBtn = document.getElementById("restart");
  const scrubber = document.getElementById("scrubber");
  const stageName = document.getElementById("stage-name");
  const stageIndex = document.getElementById("stage-index");

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
// Scene registry
// ---------------------------------------------------------------------------

class SceneRegistry {
  constructor(timeline, mountEl) {
    this.timeline = timeline;
    this.mountEl = mountEl;
    this.scenes = new Map();
    this.active = null;
    this.activeId = null;
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
    this.activeId = id;
    this.active.mount(this.mountEl);
    document.querySelectorAll(".tab").forEach((t) => {
      t.classList.toggle("active", t.dataset.scene === id);
    });
    // Replay current stage so the new scene snaps into place.
    this.timeline._updateStage(true);
  }
}

// ---------------------------------------------------------------------------
// Keyboard shortcuts
// ---------------------------------------------------------------------------

function mountKeyboard(timeline, registry) {
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
      case "1": registry.activate("A"); break;
      case "2": registry.activate("B"); break;
      case "3": registry.activate("C"); break;
    }
  });
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

registry.register("A", SceneA);
registry.register("B", SceneB);
registry.register("C", SceneC);
registry.activate("A");

// Parallel-tenant toggle
const parallelToggle = document.getElementById("parallel-mode");
window.__parallelMode = false;
parallelToggle.addEventListener("change", () => {
  window.__parallelMode = parallelToggle.checked;
  // Restart so new mode takes effect from a clean log.
  // Re-mount the active scene to clear its internal state cleanly.
  if (registry.activeId) registry.activate(registry.activeId);
  timeline.restart();
});

timeline.play();

// Self-checks
console.assert(STAGES.length === 10, "expected 10 stages");
console.assert(TOTAL_DURATION_MS === 16500, "expected 16500ms total");

window.__timeline = timeline;
window.__registry = registry;

console.log("multi-tenancy-workflow ready");
