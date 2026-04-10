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
