import {
  STAGES,
  TENANTS,
  TOTAL_DURATION_MS,
  FANOUT_CALLSITES,
  FANOUT_MEASUREMENTS,
  FANOUT_SUMMARY,
} from "./data.js";

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

// `added: true` marks a component introduced by this multi-tenant fork.
// Coordinates are tuned to fit inside the Synapse process wrapper drawn
// behind the nodes (see FORK_WRAPPER below).
// Fork layout. The wrapper holds the in-process Synapse pieces.
// Existing upstream components (Servlet, Handlers, DataStore) are
// kept around the fork-added pieces (TenantRouter, ContextVar,
// MultiKeyring, Media root) so the additions are always shown
// in the broader Synapse context.
const NODES = {
  client:       { x: 30,  y: 255, w: 95,  h: 56, label: "Client",         added: false },
  traefik:      { x: 140, y: 255, w: 95,  h: 56, label: "Traefik",        added: false },
  servlet:      { x: 280, y: 240, w: 110, h: 56, label: "Servlet",        added: false },
  router:       { x: 405, y: 120, w: 140, h: 40, label: "TenantRouter",   added: true  },
  context:      { x: 405, y: 168, w: 140, h: 40, label: "ContextVar",     added: true  },
  ratelimiter:  { x: 405, y: 216, w: 140, h: 40, label: "RateLimiter",    added: true  },
  handlers:     { x: 405, y: 280, w: 140, h: 46, label: "Handlers",       added: false },
  keyring:      { x: 560, y: 120, w: 140, h: 40, label: "MultiKeyring",   added: true  },
  media:        { x: 560, y: 168, w: 140, h: 40, label: "Media root",     added: true  },
  appservices:  { x: 560, y: 216, w: 140, h: 40, label: "AppServices",    added: true  },
  datastore:    { x: 560, y: 280, w: 140, h: 46, label: "DataStore",      added: false },
  postgres:     { x: 755, y: 255, w: 110, h: 56, label: "Postgres",       added: false },
  controlplane: { x: 755, y: 140, w: 110, h: 56, label: "Control Plane",  added: true  },
};

const LINKS = [
  ["client", "traefik"],
  ["traefik", "servlet"],
  ["servlet", "router"],
  ["router", "context"],
  ["context", "ratelimiter"],
  ["ratelimiter", "handlers"],
  ["handlers", "keyring"],
  ["handlers", "media"],
  ["handlers", "appservices"],
  ["handlers", "datastore"],
  ["datastore", "postgres"],
  ["controlplane", "postgres"],
];

const FORK_WRAPPER = { x: 270, y: 100, w: 445, h: 250, label: "Synapse process" };

// Upstream (vanilla, single-tenant) Synapse layout. No TenantRouter,
// no ContextVar — the HomeServer instance is created once at startup
// and code reads `self.hs.hostname` directly. Same wrapper, same
// outer structure for visual parity with the fork.
const UPSTREAM_NODES = {
  uclient:    { x: 30,  y: 255, w: 95,  h: 56, label: "Client",     added: false },
  unginx:     { x: 140, y: 255, w: 95,  h: 56, label: "Nginx",      added: false },
  uservlet:   { x: 280, y: 240, w: 110, h: 56, label: "Servlet",    added: false },
  usynapse:   { x: 405, y: 175, w: 140, h: 50, label: "HomeServer", added: false },
  uhandlers:  { x: 405, y: 270, w: 140, h: 46, label: "Handlers",   added: false },
  ukeyring:   { x: 560, y: 145, w: 140, h: 46, label: "Keyring",    added: false },
  umedia:     { x: 560, y: 200, w: 140, h: 46, label: "Media root", added: false },
  udatastore: { x: 560, y: 270, w: 140, h: 46, label: "DataStore",  added: false },
  upostgres:  { x: 755, y: 255, w: 110, h: 56, label: "Postgres",   added: false },
};

const UPSTREAM_LINKS = [
  ["uclient", "unginx"],
  ["unginx", "uservlet"],
  ["uservlet", "usynapse"],
  ["usynapse", "uhandlers"],
  ["uhandlers", "ukeyring"],
  ["uhandlers", "umedia"],
  ["uhandlers", "udatastore"],
  ["udatastore", "upostgres"],
];

const UPSTREAM_WRAPPER = { x: 270, y: 120, w: 475, h: 220, label: "Synapse process" };

function buildDiagramSvg(nodes = NODES, links = LINKS, wrapper = null) {
  const svg = document.createElementNS(SVG_NS, "svg");
  // Tight viewBox cropped to actual content bounds (with a little
  // breathing room for the wrapper label), so the diagram fills
  // narrow containers (Scenes C and D) cleanly.
  svg.setAttribute("viewBox", "15 75 870 290");
  svg.setAttribute("preserveAspectRatio", "xMidYMid meet");

  // Draw the Synapse-process wrapper FIRST so it sits behind the
  // links and nodes. The label hangs off the top-left corner.
  if (wrapper) {
    const g = document.createElementNS(SVG_NS, "g");
    g.setAttribute("class", "synapse-wrapper");
    const rect = document.createElementNS(SVG_NS, "rect");
    rect.setAttribute("x", wrapper.x);
    rect.setAttribute("y", wrapper.y);
    rect.setAttribute("width", wrapper.w);
    rect.setAttribute("height", wrapper.h);
    rect.setAttribute("rx", 12);
    const label = document.createElementNS(SVG_NS, "text");
    label.setAttribute("x", wrapper.x + 12);
    label.setAttribute("y", wrapper.y - 8);
    label.textContent = wrapper.label;
    g.appendChild(rect);
    g.appendChild(label);
    svg.appendChild(g);
  }

  const linkEls = {};
  links.forEach(([a, b]) => {
    const A = nodes[a], B = nodes[b];
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
  Object.entries(nodes).forEach(([id, n]) => {
    const g = document.createElementNS(SVG_NS, "g");
    g.setAttribute("class", "node" + (n.added ? " added" : ""));
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

// Whether a stage represents fork-added code (not just an upstream pass-through).
function stageIsForkAdded(stage) {
  if (!stage) return false;
  if (stage.component === "all") return false;
  const node = NODES[stage.component];
  return !!(node && node.added);
}

function buildLegend() {
  const el = document.createElement("div");
  el.className = "fork-legend";
  el.innerHTML = `
    <span><span class="swatch added"></span>Fork addition</span>
    <span><span class="swatch upstream"></span>Upstream / infra</span>
  `;
  return el;
}

function applyActiveHighlight(nodeEls, linkEls, activeId) {
  Object.entries(nodeEls).forEach(([id, el]) => {
    el.classList.toggle("active", id === activeId || activeId === "all");
  });
  if (linkEls) {
    Object.entries(linkEls).forEach(([key, el]) => {
      const [from, to] = key.split("->");
      el.classList.toggle("flowing", to === activeId || from === activeId);
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

    const built = buildDiagramSvg(NODES, LINKS, FORK_WRAPPER);
    this.svg = built.svg;
    this.nodeEls = built.nodeEls;
    this.linkEls = built.linkEls;

    // Packet
    this.packet = document.createElementNS(SVG_NS, "circle");
    this.packet.setAttribute("class", "packet");
    this.packet.setAttribute("r", "11");
    this.packet.setAttribute("cx", NODES.client.x + NODES.client.w / 2);
    this.packet.setAttribute("cy", NODES.client.y + NODES.client.h / 2);
    this.svg.appendChild(this.packet);

    this.diagramEl.appendChild(this.svg);
    this.diagramEl.appendChild(buildLegend());

    this.sideEl = document.createElement("aside");
    this.sideEl.className = "side";
    this.sideEl.innerHTML = `
      <span class="fork-badge" id="a-badge">Fork addition</span>
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
    applyActiveHighlight(this.nodeEls, this.linkEls, stage.component);

    const target = NODES[stage.component] || NODES.client;
    this.packet.setAttribute("cx", target.x + target.w / 2);
    this.packet.setAttribute("cy", target.y + target.h / 2);

    this.sideEl.querySelector("#a-stage").textContent = stage.label;
    this.sideEl.querySelector("#a-fileref").textContent = stage.fileRef || "—";
    this.sideEl.querySelector("#a-code code").innerHTML = highlight(stage.code, stage.codeLang);
    this.sideEl.querySelector("#a-badge").classList.toggle("visible", stageIsForkAdded(stage));
  }

  unmount() {
    this.timeline.removeEventListener("stageChange", this._onStage);
  }
}

// ---------------------------------------------------------------------------
// Scene B — layered stack
// ---------------------------------------------------------------------------

const LAYERS = [
  { id: "network",       label: "Network",          sub: "Client · Traefik",                              added: false },
  { id: "routing",       label: "Routing",          sub: "TenantRouter · TenantConfig",                   added: true  },
  { id: "context",       label: "Tenant Context",   sub: "contextvars.ContextVar",                        added: true  },
  { id: "storage",       label: "Storage",          sub: "Postgres · Keyring · Media",                    added: true  },
  { id: "tenant_config", label: "Per-Tenant Config",sub: "SSO · Email · Push · RateLimits · AppServices", added: true  },
  { id: "lifecycle",     label: "Lifecycle",         sub: "Control Plane · Reload · Backup/Restore",      added: true  },
  { id: "response",      label: "Response",          sub: "Tagged with tenant",                           added: false },
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
      el.className = "layer" + (L.added ? " added" : "");
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
      <span class="fork-badge" id="b-badge">Fork addition</span>
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
    const layer = LAYERS.find((L) => L.id === stage.layer);
    this.sideEl.querySelector("#b-badge").classList.toggle("visible", !!(layer && layer.added));
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
    const built = buildDiagramSvg(NODES, LINKS, FORK_WRAPPER);
    this.svg = built.svg;
    this.nodeEls = built.nodeEls;
    this.linkEls = built.linkEls;
    this.mini.appendChild(this.svg);
    this.mini.appendChild(buildLegend());

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
    applyActiveHighlight(this.nodeEls, this.linkEls, stage.component);

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

    const isAdded = stageIsForkAdded(stage);
    tenants.forEach((t, i) => {
      stage.logLines.forEach((line) => {
        const li = document.createElement("li");
        if (isAdded) li.classList.add("added");
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
// Scene D — side-by-side comparison with upstream Synapse
// ---------------------------------------------------------------------------

class SceneD {
  constructor(timeline) {
    this.timeline = timeline;
    this._onStage = (e) => this.onStageChange(e.detail.stage);
  }

  mount(root) {
    root.innerHTML = "";
    root.classList.add("scene-d");
    root.classList.remove("scene-a", "scene-b", "scene-c");

    // Upstream side (left)
    const upWrap = document.createElement("section");
    upWrap.className = "compare-pane upstream";
    upWrap.innerHTML = `<header class="pane-header">Upstream Synapse <span class="pane-tag">single-tenant</span></header>`;
    const upDiagram = document.createElement("div");
    upDiagram.className = "mini-diagram scene-a"; // borrow scene-a SVG styles
    const upBuilt = buildDiagramSvg(UPSTREAM_NODES, UPSTREAM_LINKS, UPSTREAM_WRAPPER);
    this.upSvg = upBuilt.svg;
    this.upNodeEls = upBuilt.nodeEls;
    this.upLinkEls = upBuilt.linkEls;
    upDiagram.appendChild(this.upSvg);
    upWrap.appendChild(upDiagram);

    const upCode = document.createElement("pre");
    upCode.className = "compare-code";
    upCode.innerHTML = `<div class="file-ref" id="d-up-fileref">—</div><code></code>`;
    upWrap.appendChild(upCode);

    // Fork side (right)
    const fkWrap = document.createElement("section");
    fkWrap.className = "compare-pane fork";
    fkWrap.innerHTML = `<header class="pane-header">Multi-tenant fork <span class="pane-tag added">N tenants · 1 process</span></header>`;
    const fkDiagram = document.createElement("div");
    fkDiagram.className = "mini-diagram scene-a";
    const fkBuilt = buildDiagramSvg(NODES, LINKS, FORK_WRAPPER);
    this.fkSvg = fkBuilt.svg;
    this.fkNodeEls = fkBuilt.nodeEls;
    this.fkLinkEls = fkBuilt.linkEls;
    fkDiagram.appendChild(this.fkSvg);
    fkDiagram.appendChild(buildLegend());
    fkWrap.appendChild(fkDiagram);

    const fkCode = document.createElement("pre");
    fkCode.className = "compare-code";
    fkCode.innerHTML = `<div class="file-ref" id="d-fk-fileref">—</div><span class="fork-badge" id="d-fk-badge">Fork addition</span><code></code>`;
    fkWrap.appendChild(fkCode);

    root.appendChild(upWrap);
    root.appendChild(fkWrap);

    this._upCodeEl = upCode.querySelector("code");
    this._fkCodeEl = fkCode.querySelector("code");
    this._upFileEl = upCode.querySelector("#d-up-fileref");
    this._fkFileEl = fkCode.querySelector("#d-fk-fileref");
    this._fkBadgeEl = fkCode.querySelector("#d-fk-badge");

    this.timeline.addEventListener("stageChange", this._onStage);
  }

  onStageChange(stage) {
    if (!stage) return;
    applyActiveHighlight(this.fkNodeEls, this.fkLinkEls, stage.component);
    applyActiveHighlight(this.upNodeEls, this.upLinkEls, stage.upstream);

    this._upCodeEl.innerHTML = highlight(stage.upstreamCode || "", stage.upstreamCodeLang || "python");
    this._upFileEl.textContent = stage.upstreamFileRef || "—";
    this._fkCodeEl.innerHTML = highlight(stage.code, stage.codeLang);
    this._fkFileEl.textContent = stage.fileRef || "—";
    this._fkBadgeEl.classList.toggle("visible", stageIsForkAdded(stage));
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

  document.getElementById("stage-total").textContent = String(STAGES.length);

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

// ---------------------------------------------------------------------------
// Scene E — fan-out & density bottleneck
//
// Self-contained visual explainer for the 2026-04-21 density finding.
// Runs its own animation loop (independent of the main timeline) because
// the cadence of the fan-out tick has nothing to do with request lifecycle
// stages; it's an always-on architectural property.
//
// Layout (top → bottom):
//   1. Dependency graph: 14 callsites → 1 helper → reactor
//   2. Tick mechanic: every 3 s (visual), helper fires N bursts onto
//      the reactor lane; a /versions request that arrives during a
//      burst has to wait for it to drain.
//   3. Measured impact: 4 / 10 / 50 (fan-out on) vs 100 (fan-out off).
//   4. Conclusion strip: one sentence that names the bottleneck.
// ---------------------------------------------------------------------------

class SceneE {
  constructor(timeline) {
    this.timeline = timeline;
    this._tickTimer = null;
    this._requestTimer = null;
    this._requestIdSeq = 0;
    this._state = { n: 50, fanoutOn: true };
  }

  mount(root) {
    root.innerHTML = "";
    root.classList.add("scene-e");
    root.classList.remove("scene-a", "scene-b", "scene-c", "scene-d");

    root.innerHTML = `
      <div class="e-controls">
        <label class="e-ctrl">
          <span>Tenants (N)</span>
          <select id="e-n">
            <option value="4">4</option>
            <option value="10">10</option>
            <option value="50" selected>50</option>
            <option value="100">100</option>
          </select>
        </label>
        <label class="e-ctrl e-fanout-toggle">
          <input type="checkbox" id="e-fanout" checked>
          <span>Fan-out enabled</span>
        </label>
        <button class="e-pulse-btn" id="e-pulse">Fire a tick now</button>
        <div class="e-state-badge" id="e-state-badge"></div>
      </div>

      <section class="e-deps">
        <h3>Dependencies · <span class="e-deps-count">${FANOUT_CALLSITES.length}</span> callsites feed one helper</h3>
        <div class="e-deps-grid">
          <div class="e-deps-left">
            ${FANOUT_CALLSITES.map(
              (c) => `
                <div class="e-dep e-dep-${c.severity}" title="${c.interval}">
                  <div class="e-dep-mod">${c.module}</div>
                  <div class="e-dep-proc">${c.proc}</div>
                  <div class="e-dep-int">${c.interval}</div>
                </div>
              `,
            ).join("")}
          </div>
          <svg class="e-deps-arrows" viewBox="0 0 120 100" preserveAspectRatio="none" aria-hidden="true">
            <path d="M 0 10  C 60 10 60 50 120 50" class="e-arrow-line" />
            <path d="M 0 30  C 60 30 60 50 120 50" class="e-arrow-line" />
            <path d="M 0 50  C 60 50 60 50 120 50" class="e-arrow-line" />
            <path d="M 0 70  C 60 70 60 50 120 50" class="e-arrow-line" />
            <path d="M 0 90  C 60 90 60 50 120 50" class="e-arrow-line" />
          </svg>
          <div class="e-deps-right">
            <div class="e-helper-box">
              <div class="e-helper-title">run_as_background_process_per_tenant</div>
              <div class="e-helper-file">synapse/tenant_background.py</div>
              <div class="e-helper-note">iterates <code>registry.get_all_tenants()</code><br>spawns one bg-process per tenant<br>synchronously on the reactor</div>
            </div>
          </div>
        </div>
      </section>

      <section class="e-mechanic">
        <h3>Tick mechanic · reactor thread is the contested resource</h3>
        <div class="e-mechanic-body">
          <div class="e-clock" id="e-clock" title="every 5 s (visualised every 3 s)">
            <div class="e-clock-face"></div>
            <div class="e-clock-hand"></div>
            <div class="e-clock-label">tick</div>
          </div>
          <div class="e-fanout-stage">
            <svg id="e-burst-svg" viewBox="0 0 800 220" preserveAspectRatio="xMidYMid meet">
              <!-- helper anchor -->
              <rect x="8" y="96" width="132" height="28" rx="6" class="e-helper-pill" />
              <text x="74" y="114" text-anchor="middle" class="e-helper-pill-text">helper</text>
              <!-- reactor lane (a single-thread highway) -->
              <rect x="560" y="60" width="230" height="100" rx="8" class="e-reactor-bg" />
              <text x="675" y="52" text-anchor="middle" class="e-reactor-label">REACTOR THREAD</text>
              <rect id="e-reactor-fill" x="562" y="62" width="0" height="96" rx="6" class="e-reactor-fill" />
              <text x="675" y="178" text-anchor="middle" class="e-reactor-sub">LogContext × N · ContextVar × N · Prom counters × N</text>
              <!-- burst dots injected dynamically -->
              <g id="e-burst-dots"></g>
            </svg>
          </div>
        </div>
      </section>

      <section class="e-impact">
        <h3>Request impact · GET /_matrix/client/versions (static, unauth)</h3>
        <svg id="e-req-svg" viewBox="0 0 800 90" preserveAspectRatio="xMidYMid meet">
          <line x1="20" y1="45" x2="780" y2="45" class="e-req-track" />
          <text x="20"  y="20" class="e-req-caption-left">arrive →</text>
          <text x="780" y="20" text-anchor="end" class="e-req-caption-right">→ response</text>
          <!-- reactor-busy marker -->
          <rect id="e-req-busy" x="0" y="30" width="0" height="30" class="e-req-busy" rx="3" />
          <g id="e-req-dots"></g>
        </svg>
      </section>

      <section class="e-measured">
        <h3>Measured impact · /_matrix/client/versions p95 by run</h3>
        <table class="e-measure-table">
          <thead><tr>
            <th>Tenants</th><th>Fan-out</th><th>p95</th><th>Note</th>
          </tr></thead>
          <tbody>
            ${FANOUT_MEASUREMENTS.map(
              (m) => `
                <tr class="e-measure-row${m.fanout === "off" ? " e-measure-row-off" : ""}">
                  <td>${m.n}</td>
                  <td><span class="e-pill e-pill-${m.fanout}">${m.fanout.toUpperCase()}</span></td>
                  <td class="e-measure-p95">${m.p95} ms</td>
                  <td class="e-measure-note">${m.note}</td>
                </tr>
              `,
            ).join("")}
          </tbody>
        </table>
        <p class="e-conclusion">
          <strong>What this picture shows:</strong> ${FANOUT_SUMMARY.mechanic}
          <br><br>
          <strong>Why it matters at density:</strong> ${FANOUT_SUMMARY.consequence}
        </p>
      </section>
    `;

    this.el = root;
    this.nSelect = root.querySelector("#e-n");
    this.fanoutCheck = root.querySelector("#e-fanout");
    this.pulseBtn = root.querySelector("#e-pulse");
    this.burstDotsG = root.querySelector("#e-burst-dots");
    this.reactorFill = root.querySelector("#e-reactor-fill");
    this.reqBusy = root.querySelector("#e-req-busy");
    this.reqDotsG = root.querySelector("#e-req-dots");
    this.clockEl = root.querySelector("#e-clock");
    this.badgeEl = root.querySelector("#e-state-badge");

    this.nSelect.addEventListener("change", () => {
      this._state.n = parseInt(this.nSelect.value, 10);
      this._updateBadge();
    });
    this.fanoutCheck.addEventListener("change", () => {
      this._state.fanoutOn = this.fanoutCheck.checked;
      this._updateBadge();
      this._syncMeasuredHighlight();
    });
    this.pulseBtn.addEventListener("click", () => this._fireTick());

    this._updateBadge();
    this._syncMeasuredHighlight();

    // Start two independent animation loops:
    //   - a tick every ~3 s (visualising the fan-out burst)
    //   - a new /versions request every ~350 ms (so ~8 per tick)
    this._tickTimer = setInterval(() => {
      if (this._state.fanoutOn) this._fireTick();
    }, 3000);
    this._requestTimer = setInterval(() => this._spawnRequest(), 350);
  }

  _updateBadge() {
    const match = FANOUT_MEASUREMENTS.find(
      (m) =>
        m.n === this._state.n &&
        m.fanout === (this._state.fanoutOn ? "on" : "off"),
    );
    const text = match
      ? `N=${this._state.n} · fan-out ${this._state.fanoutOn ? "on" : "off"} · measured p95 ≈ ${match.p95} ms`
      : `N=${this._state.n} · fan-out ${this._state.fanoutOn ? "on" : "off"} · (combination not measured)`;
    this.badgeEl.textContent = text;
    this.badgeEl.classList.toggle("e-badge-hot", this._state.fanoutOn && this._state.n >= 50);
    this.badgeEl.classList.toggle("e-badge-cool", !this._state.fanoutOn || this._state.n <= 10);
  }

  _syncMeasuredHighlight() {
    this.el.querySelectorAll(".e-measure-row").forEach((row) => row.classList.remove("e-measure-row-active"));
    const match = FANOUT_MEASUREMENTS.findIndex(
      (m) =>
        m.n === this._state.n &&
        m.fanout === (this._state.fanoutOn ? "on" : "off"),
    );
    if (match >= 0) {
      this.el.querySelectorAll(".e-measure-row")[match].classList.add("e-measure-row-active");
    }
  }

  _fireTick() {
    // pulse the clock
    this.clockEl.classList.remove("e-clock-pulse");
    // force reflow to restart the animation
    void this.clockEl.offsetWidth;
    this.clockEl.classList.add("e-clock-pulse");

    const n = this._state.n;
    const svg = this.burstDotsG.ownerSVGElement;
    const w = 800, h = 220;
    const helperX = 140, helperY = 110;
    const reactorX = 562, reactorY = 110;

    // Spawn N dots flying from helper to reactor. Staggered by a tiny
    // amount so the human eye reads them as a burst, not a swarm.
    for (let i = 0; i < n; i++) {
      const dot = document.createElementNS(SVG_NS, "circle");
      dot.setAttribute("r", n > 40 ? 3 : 5);
      dot.setAttribute("class", "e-burst-dot");
      dot.setAttribute("cx", helperX);
      dot.setAttribute("cy", helperY + (Math.random() - 0.5) * 10);
      this.burstDotsG.appendChild(dot);

      const delay = i * Math.max(4, 400 / n); // whole burst inside ~400 ms
      // Animate with Web Animations API for precise timing + easy cleanup.
      const anim = dot.animate(
        [
          { cx: helperX, cy: dot.getAttribute("cy"), opacity: 1 },
          {
            cx: reactorX + Math.random() * 220,
            cy: reactorY + (Math.random() - 0.5) * 80,
            opacity: 0.3,
          },
        ],
        { duration: 700, delay, fill: "forwards", easing: "cubic-bezier(.4,.1,.2,1)" },
      );
      anim.onfinish = () => dot.remove();
    }

    // Reactor fill: grows proportional to N over the burst duration,
    // then drains. This is the "reactor busy" indicator.
    const busyMs = 200 + n * 8; // empirically: ~8ms reactor time per tenant bookkeeping
    const capped = Math.min(1, busyMs / 1200);
    this.reactorFill.animate(
      [
        { width: 0, opacity: 0 },
        { width: `${capped * 226}px`, opacity: 0.9, offset: 0.2 },
        { width: `${capped * 226}px`, opacity: 0.9, offset: 0.7 },
        { width: 0, opacity: 0 },
      ],
      { duration: 1400, fill: "forwards", easing: "linear" },
    );

    // Request track: mark the reactor as "busy" for the same window.
    // Requests spawned during this window are visibly queued.
    this._reactorBusyUntil = performance.now() + busyMs;
    const busyDuration = Math.min(1200, busyMs);
    this.reqBusy.animate(
      [
        { width: 0, opacity: 0 },
        { width: `${760 * (busyDuration / 1200)}px`, opacity: 0.7, offset: 0.2 },
        { width: `${760 * (busyDuration / 1200)}px`, opacity: 0.7, offset: 0.7 },
        { width: 0, opacity: 0 },
      ],
      { duration: 1400, fill: "forwards", easing: "linear" },
    );
  }

  _spawnRequest() {
    const now = performance.now();
    const reactorBusy = this._reactorBusyUntil && now < this._reactorBusyUntil;
    const dot = document.createElementNS(SVG_NS, "circle");
    dot.setAttribute("r", 6);
    dot.setAttribute("class", reactorBusy ? "e-req-dot e-req-dot-stall" : "e-req-dot");
    dot.setAttribute("cx", 20);
    dot.setAttribute("cy", 45);
    this.reqDotsG.appendChild(dot);

    // Fast path: zip across in ~900 ms. Stalled path: hold for 500 ms
    // at some intermediate x while reactor busy, then dash.
    if (reactorBusy) {
      const stallMs = Math.max(0, this._reactorBusyUntil - now);
      const anim = dot.animate(
        [
          { cx: 20,  offset: 0 },
          { cx: 220, offset: 0.1 },
          { cx: 220, offset: 0.1 + Math.min(0.6, stallMs / 2000) },
          { cx: 780, offset: 1 },
        ],
        { duration: 900 + stallMs, fill: "forwards", easing: "linear" },
      );
      anim.onfinish = () => dot.remove();
    } else {
      const anim = dot.animate(
        [
          { cx: 20 },
          { cx: 780 },
        ],
        { duration: 900, fill: "forwards", easing: "linear" },
      );
      anim.onfinish = () => dot.remove();
    }
  }

  unmount() {
    if (this._tickTimer) clearInterval(this._tickTimer);
    if (this._requestTimer) clearInterval(this._requestTimer);
    this._tickTimer = null;
    this._requestTimer = null;
    if (this.burstDotsG) this.burstDotsG.innerHTML = "";
    if (this.reqDotsG) this.reqDotsG.innerHTML = "";
  }
}

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
      case "4": registry.activate("D"); break;
      case "5": registry.activate("E"); break;
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
registry.register("D", SceneD);
registry.register("E", SceneE);
registry.activate("A");

// Fork-additions toggle
const forkToggle = document.getElementById("fork-mode");
forkToggle.addEventListener("change", () => {
  document.body.classList.toggle("show-added", forkToggle.checked);
  // Re-apply badge visibility for the current stage.
  timeline._updateStage(true);
});

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
console.assert(STAGES.length === 24, "expected 24 stages, got " + STAGES.length);
console.assert(TOTAL_DURATION_MS === 46000, "expected 46000ms total, got " + TOTAL_DURATION_MS);

window.__timeline = timeline;
window.__registry = registry;

console.log("multi-tenancy-workflow ready");
