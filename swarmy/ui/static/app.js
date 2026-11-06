"use strict";
/* swARMY order of battle: an editable org chart (chain of command) for agent hierarchies.
   The browser owns the org being edited (an explicit tree); the server derives names, metrics,
   contact rules and wiki membership from it via /api/analyze, using the same code as the engine. */

const $ = (s, el = document) => el.querySelector(s);
const SVG = "http://www.w3.org/2000/svg";
const W = 172, H = 74, GX = 18, GY = 62, STACK_GAP = 10, INDENT = 22, ROW_GAP = 40, UNIT_GAP = 34, FRAME_TOP = 20;
let BAND_PAD = 160;

const KIND_COLOR = { manager: "var(--k-chain)", report: "var(--k-chain)", peer: "var(--k-peer)",
  cross_team: "var(--k-cross)", skip_up: "var(--k-skipup)", skip_down: "var(--k-skipdown)", other: "var(--k-other)" };
const KIND_NAME = { manager: "Commanding officer", report: "Direct subordinate", peer: "Comrade (same unit)",
  cross_team: "Same rank, other unit", skip_up: "Skip-level up", skip_down: "Skip-level down", other: "Anywhere (open ranks)" };
const FAM_COLOR = { opus: "var(--m-opus)", sonnet: "var(--m-sonnet)", haiku: "var(--m-haiku)", paid: "var(--m-paid)",
  free: "var(--m-free)", mock: "var(--m-mock)", unknown: "var(--m-unknown)" };
const WIKI_COLORS = ["#1baf7a", "#2a78d6", "#eb6834", "#6250d6", "#eda100", "#e87ba4"];
const EFFORT_NOTE = { "": "model default", low: "low", medium: "medium", high: "high", xhigh: "extra high", max: "max" };

const S = {
  catalog: null, orgs: [], file: null, config: null, analysis: null, byPath: {}, nameToId: {},
  idx: {}, layers: [], pos: {}, selected: null, selectedLevel: null, selectedWiki: null, tab: "structure",
  scope: null, collapsed: new Set(), undo: [], redo: [], view: { x: 40, y: 40, k: 1 }, run: null,
  flashes: [], pings: [], insertForm: null, customTask: false, depthY: [0], rows: [],
  layoutMode: "auto", mode: "tree", frames: [],
};
let NEXT_ID = 1;

/* ---------------- helpers ---------------- */
function h(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (k === "class") e.className = v;
    else if (k === "value") e.value = v;
    else if (k === "checked") e.checked = !!v;
    else e.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat(3)) {
    if (kid === null || kid === undefined || kid === false) continue;
    e.append(typeof kid === "object" ? kid : document.createTextNode(String(kid)));
  }
  return e;
}
function s(tag, attrs, ...kids) {
  const e = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v);
  }
  for (const kid of kids.flat(3)) {
    if (kid === null || kid === undefined || kid === false) continue;
    e.append(typeof kid === "object" ? kid : document.createTextNode(String(kid)));
  }
  return e;
}
async function api(path, body) {
  const opts = body === undefined ? {} : { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) };
  const res = await fetch(path, opts);
  const data = await res.json();
  if (!res.ok || data.error) throw new Error(data.error || res.statusText);
  return data;
}
const clone = (o) => JSON.parse(JSON.stringify(o));
const plain = (cfg) => JSON.parse(JSON.stringify(cfg, (k, v) => (k === "_id" ? undefined : v)));
const trunc = (t, n) => (t && t.length > n ? t.slice(0, n - 1) + "…" : t || "");
const money = (x, d = 4) => (x == null ? "–" : "$" + Number(x).toFixed(d));
function toast(msg) {
  const t = $("#toast"); t.textContent = msg; t.hidden = false;
  clearTimeout(toast._t); toast._t = setTimeout(() => (t.hidden = true), 2600);
}
function banner(msg) { const b = $("#banner"); b.hidden = !msg; b.textContent = msg ? "Not valid yet: " + msg : ""; }
function stripNames(n) { delete n.name; delete n._id; (n.children || []).forEach(stripNames); return n; }
function assignIds(n) { if (!n._id) n._id = NEXT_ID++; (n.children || []).forEach(assignIds); }

function family(model) {
  const c = S.catalog.models.find((m) => m.alias === model);
  const id = ((c && c.model) || model || "").toLowerCase();
  const provider = c ? c.provider : model.startsWith("openrouter:") ? "openrouter" : model.startsWith("anthropic:") ? "anthropic" : model.startsWith("mock") ? "mock" : "unknown";
  if (provider === "mock") return "mock";
  if (id.includes("opus")) return "opus";
  if (id.includes("sonnet")) return "sonnet";
  if (id.includes("haiku")) return "haiku";
  if (provider === "openrouter") return (c && c.free) || id.endsWith(":free") ? "free" : "paid";
  return "unknown";
}
const modelLabel = (m) => (m.startsWith("openrouter:") ? m.split("/").pop() : m);

/* ---------------- tree model ---------------- */
function index() {
  const idx = {}, layers = [];
  (function walk(n, depth, path, parent) {
    idx[n._id] = { node: n, depth, path, parent };
    (layers[depth] = layers[depth] || []).push(n);
    (n.children || []).forEach((c, i) => walk(c, depth + 1, path ? `${path}.${i + 1}` : `${i + 1}`, n));
  })(S.config.tree, 0, "", null);
  S.idx = idx; S.layers = layers;
  if (S.selected && !idx[S.selected]) S.selected = null;
  if (S.selectedLevel != null && S.selectedLevel >= layers.length) S.selectedLevel = null;
  S.nameToId = {};
  for (const [id, e] of Object.entries(idx)) { const a = S.byPath[e.path]; if (a) S.nameToId[a.name] = Number(id); }
}
const info = (id) => S.idx[id];
const meta = (id) => (S.idx[id] ? S.byPath[S.idx[id].path] : null);
const nameOf = (id) => (meta(id) ? meta(id).name : "…");
const subtreeSize = (n) => (n.children || []).reduce((a, c) => a + 1 + subtreeSize(c), 0);

function mutate(fn) {
  S.undo.push(JSON.stringify(S.config)); if (S.undo.length > 150) S.undo.shift();
  S.redo = [];
  fn();
  assignIds(S.config.tree);
  changed();
}
function changed() { index(); renderAll(); scheduleAnalyze(); }
function undo() { if (!S.undo.length) return; S.redo.push(JSON.stringify(S.config)); S.config = JSON.parse(S.undo.pop()); changed(); }
function redo() { if (!S.redo.length) return; S.undo.push(JSON.stringify(S.config)); S.config = JSON.parse(S.redo.pop()); changed(); }

let analyzeTimer = null, analyzeSeq = 0;
function scheduleAnalyze() { clearTimeout(analyzeTimer); analyzeTimer = setTimeout(analyze, 120); }
async function analyze() {
  const seq = ++analyzeSeq;
  let res;
  try { res = await api("/api/analyze", { config: plain(S.config) }); } catch (e) { banner(e.message); return; }
  if (seq !== analyzeSeq) return;
  if (!res.ok) { banner(res.error); return; }
  banner(""); setAnalysis(res); index(); renderAll();
}
function setAnalysis(a) { S.analysis = a; S.byPath = Object.fromEntries(a.nodes.map((n) => [n.path, n])); }

/* operations */
function defaultWorker() {
  const paid = S.catalog.models.find((m) => m.provider === "openrouter" && !m.free);
  return { role: S.catalog.ranks.worker, model: paid ? paid.alias : "haiku" };
}
function rankBetween(upper, lower) {
  // mirrors swarmy.ranks.rank_between
  const lad = S.catalog.ranks.ladder;
  if (!lad.includes(upper) && !lad.includes(lower)) return "lieutenant";
  const hi = lad.indexOf(upper), lo = lad.includes(lower) ? lad.indexOf(lower) : lad.length;
  if (hi >= 0 && hi + 1 < lo) return lad[hi + 1];
  if (hi < 0 && lo > 0) return lad[lo - 1];
  return `${upper || "officier"}-adjoint`;
}
const gloss = (role) => S.catalog.ranks.gloss[role] || null;
const titleCase = (r) => (r ? r.charAt(0).toUpperCase() + r.slice(1) : r);
function defaultManagerModel() { return S.catalog.models.find((m) => m.alias === "haiku") ? "haiku" : S.config.tree.model; }
function addReport(id) {
  mutate(() => {
    const n = info(id).node; n.children = n.children || [];
    const last = n.children[n.children.length - 1];
    n.children.push(last ? stripNames(clone(last)) : defaultWorker());
    if (!last && n.role === S.catalog.ranks.worker) n.role = rankBetween(null, S.catalog.ranks.worker);
  });
}
function duplicate(id) {
  const { node, parent } = info(id);
  if (!parent) return toast("The top position can't be duplicated");
  mutate(() => parent.children.splice(parent.children.indexOf(node) + 1, 0, stripNames(clone(node))));
}
function removeNode(id) {
  const { node, parent } = info(id);
  if (!parent) return toast("The top position can't be removed");
  const n = subtreeSize(node);
  if (n && !confirm(`Remove ${nameOf(id)} and the ${n} position(s) under it?`)) return;
  mutate(() => { parent.children.splice(parent.children.indexOf(node), 1); S.selected = null; });
}
function insertManagerAbove(id) {
  const { node, parent } = info(id);
  mutate(() => {
    const mgr = { role: rankBetween(parent ? parent.role : null, node.role), model: defaultManagerModel(), children: [node] };
    if (parent) parent.children.splice(parent.children.indexOf(node), 1, mgr);
    else S.config.tree = mgr;
  });
}
function changeSpan(depth, delta) {
  mutate(() => {
    for (const n of S.layers[depth]) {
      if (!n.children || !n.children.length) continue;
      if (delta > 0) n.children.push(stripNames(clone(n.children[n.children.length - 1])));
      else if (n.children.length > 1) n.children.pop();
    }
  });
}
function insertLayerBelow(depth, role, model, groups) {
  mutate(() => {
    for (const n of S.layers[depth]) {
      const kids = n.children || [];
      if (!kids.length) continue;
      const g = Math.max(1, Math.min(groups, kids.length));
      const size = Math.ceil(kids.length / g);
      const out = [];
      for (let i = 0; i < kids.length; i += size) out.push({ role, model, children: kids.slice(i, i + size) });
      n.children = out;
    }
  });
}
function removeLayer(depth) {
  const last = S.layers.length - 1;
  if (S.layers.length === 1) return toast("An org needs at least one level");
  if (depth === 0) {
    if ((S.config.tree.children || []).length !== 1) return toast("Remove the top level only when it has a single report");
    return mutate(() => { S.config.tree = S.config.tree.children[0]; });
  }
  if (depth === last && !confirm(`Remove all ${S.layers[depth].length} positions at the bottom level? Their managers become workers.`)) return;
  mutate(() => {
    for (const n of S.layers[depth]) {
      const parent = info(n._id).parent;
      const i = parent.children.indexOf(n);
      if (depth === last) parent.children.splice(i, 1);
      else if (n.children && n.children.length) parent.children.splice(i, 1, ...n.children);
    }
    S.selectedLevel = null;
  });
}
function targets(id) {
  const e = info(id);
  if (S.scope === "level") return S.layers[e.depth];
  if (S.scope === "role") return Object.values(S.idx).map((x) => x.node).filter((n) => n.role === e.node.role);
  return [e.node];
}
function setField(nodes, field, value) {
  mutate(() => nodes.forEach((n) => { if (value === "" || value == null) delete n[field]; else n[field] = value; }));
}

/* ---------------- layout & chart ---------------- */
const narrow = () => $("#canvas").getBoundingClientRect().width < 600;
const readableK = () => (narrow() ? 0.5 : 0.8);
function availableWidth() {
  // chart units that fit across the canvas at a readable zoom
  return ($("#canvas").getBoundingClientRect().width - 30) / readableK() - BAND_PAD - 40;
}
function layout() {
  BAND_PAD = narrow() ? 12 : 160;
  const m = S.layoutMode;
  if (m === "rows") return layoutRows(availableWidth());
  layoutTree(m !== "tree");
  if (m === "auto") {
    const b = bounds();
    if (b.maxX - b.minX > availableWidth()) layoutRows(availableWidth());
  }
}
function layoutTree(stack) {
  // Top-down tidy layout. With `stack`, a commander whose subordinates are all hands-on gets them
  // stacked vertically underneath (one column per unit), which keeps wide orgs narrower.
  const pos = {}, rows = [];
  let cursor = 0;
  const stacks = (n) => stack && (n.children || []).length >= 2 && !S.collapsed.has(n._id)
    && n.children.every((c) => !(c.children && c.children.length));
  (function place(n, depth) {
    const kids = n.children || [];
    rows[depth] = Math.max(rows[depth] || 1, 1);
    if (!kids.length || S.collapsed.has(n._id)) { pos[n._id] = { x: cursor, depth, row: 0 }; cursor += W + GX; return; }
    if (stacks(n)) {
      pos[n._id] = { x: cursor, depth, row: 0 };
      kids.forEach((c, i) => (pos[c._id] = { x: cursor + INDENT, depth: depth + 1, row: i, stackedUnder: n._id }));
      rows[depth + 1] = Math.max(rows[depth + 1] || 1, kids.length);
      cursor += W + INDENT + GX;
      return;
    }
    kids.forEach((c) => place(c, depth + 1));
    const xs = kids.map((c) => pos[c._id].x);
    pos[n._id] = { x: (Math.min(...xs) + Math.max(...xs)) / 2, depth, row: 0 };
  })(S.config.tree, 0);
  const depthY = [0];
  for (let d = 1; d < rows.length; d++) depthY[d] = depthY[d - 1] + (rows[d - 1] || 1) * (H + STACK_GAP) - STACK_GAP + GY;
  for (const p of Object.values(pos)) p.y = depthY[p.depth] + p.row * (H + STACK_GAP);
  Object.assign(S, { pos, depthY, rows, frames: [], mode: "tree", rowGap: H + STACK_GAP });
}
function layoutRows(maxW) {
  // Split levels: each level is a sequence of units (a commander's direct subordinates, in order),
  // packed into as many rows as needed to fit `maxW`. A unit wider than a row wraps onto its own rows.
  const levels = [[{ parent: null, ids: [S.config.tree._id] }]];
  for (let d = 0; d < levels.length; d++) {
    for (const g of levels[d]) for (const id of g.ids) {
      const n = info(id).node;
      if (n.children && n.children.length && !S.collapsed.has(id)) (levels[d + 1] = levels[d + 1] || []).push({ parent: id, ids: n.children.map((c) => c._id) });
    }
  }
  const pos = {}, frames = [], rows = [], depthY = [];
  const perLine = Math.max(1, Math.floor((maxW + GX) / (W + GX)));
  const width = (k) => k * (W + GX) - GX;
  let y = 0;
  levels.forEach((groups, d) => {
    const lines = [];
    let cur = { items: [], w: 0 };
    for (const g of groups) {
      if (width(g.ids.length) > maxW) {
        if (cur.items.length) { lines.push(cur); cur = { items: [], w: 0 }; }
        for (let i = 0; i < g.ids.length; i += perLine) {
          const ids = g.ids.slice(i, i + perLine);
          lines.push({ items: [{ g, ids, part: i > 0 }], w: width(ids.length) });
        }
        continue;
      }
      const need = (cur.items.length ? UNIT_GAP : 0) + width(g.ids.length);
      if (cur.items.length && cur.w + need > maxW) { lines.push(cur); cur = { items: [], w: 0 }; }
      cur.items.push({ g, ids: g.ids }); cur.w += cur.items.length > 1 ? need : width(g.ids.length);
    }
    if (cur.items.length) lines.push(cur);
    depthY[d] = y;
    const lineW = Math.max(...lines.map((l) => l.w));
    lines.forEach((line, li) => {
      let x = (Math.max(maxW, lineW) - line.w) / 2;
      const ly = y + li * (H + ROW_GAP);
      for (const it of line.items) {
        it.ids.forEach((id, k) => (pos[id] = { x: x + k * (W + GX), y: ly, depth: d, row: li }));
        if (it.g.parent != null) frames.push({ parent: it.g.parent, depth: d, part: !!it.part, ids: it.ids, x0: x, y0: ly, x1: x + width(it.ids.length), y1: ly + H });
        x += width(it.ids.length) + UNIT_GAP;
      }
    });
    rows[d] = lines.length;
    y += lines.length * (H + ROW_GAP) - ROW_GAP + GY + (levels[d + 1] ? FRAME_TOP : 0);
  });
  Object.assign(S, { pos, depthY, rows, frames, mode: "rows", rowGap: H + ROW_GAP });
}
function bounds() {
  const ps = Object.values(S.pos);
  return { minX: Math.min(...ps.map((p) => p.x)), maxX: Math.max(...ps.map((p) => p.x)) + W,
    minY: 0, maxY: Math.max(...ps.map((p) => p.y)) + H };
}
function fit(minK = 0) {
  layout();
  if (minK) minK = Math.min(minK, readableK());
  const b = bounds(), box = $("#canvas").getBoundingClientRect();
  const w = b.maxX - b.minX + BAND_PAD + 60, hh = b.maxY - b.minY + 90;
  const fitK = Math.min(1.1, (box.width - 20) / w, (box.height - 40) / hh);
  const k = minK && S.mode === "rows" ? Math.min(1.1, (box.width - 20) / w) : Math.max(minK, fitK);
  if (S.mode === "rows" || k <= fitK + 1e-6) {
    S.view = { k, x: (box.width - w * k) / 2 - (b.minX - BAND_PAD) * k, y: 30 + (narrow() ? 14 : 0) };
  } else if (k > fitK + 1e-6) {  // too big to fit at a readable size: centre on the top of the org
    const root = S.pos[S.config.tree._id];
    S.view = { k, x: box.width / 2 - (root.x + W / 2) * k, y: 30 };
  }
  renderChart();
}
const center = (id) => ({ x: S.pos[id].x + W / 2, y: S.pos[id].y + H / 2 });

function visibleId(id) {
  // nearest visible ancestor (for collapsed subtrees)
  let cur = id;
  while (cur && !S.pos[cur]) { const p = info(cur).parent; cur = p ? p._id : null; }
  return cur;
}

function renderChart() {
  if (!S.config) return;
  layout();
  const svg = $("#chart");
  svg.replaceChildren();
  const v = S.view;
  const g = s("g", { transform: `translate(${v.x},${v.y}) scale(${v.k})` });
  svg.append(g);
  const b = bounds();
  const now = Date.now();
  const run = S.run;

  // level bands
  S.layers.forEach((layer, d) => {
    if (!layer.some((n) => S.pos[n._id])) return;
    const top = S.mode === "rows" && d > 0 ? FRAME_TOP : 0;
    const y = S.depthY[d] - GY / 2 + 6 - top, bandH = (S.rows[d] || 1) * S.rowGap - (S.rowGap - H) + GY - 12 + top;
    if (d % 2 === 0) g.append(s("rect", { class: "band", x: b.minX - BAND_PAD, y, width: b.maxX - b.minX + BAND_PAD + 40, height: bandH, rx: 10 }));
    const roles = [...new Set(layer.map((n) => n.role))].join(" / ");
    const lx = narrow() ? b.minX : b.minX - BAND_PAD + 14, ly = narrow() ? S.depthY[d] - (S.mode === "rows" && d > 0 ? FRAME_TOP + 8 : 10) : S.depthY[d] + H / 2 + 4;
    const lab = s("text", { class: "band-label" + (S.selectedLevel === d ? " sel" : ""), x: lx, y: ly,
      onclick: (e) => { e.stopPropagation(); selectLevel(d); } }, `L${d} · ${trunc(roles, 18)} ×${layer.length}`);
    g.append(lab);
  });

  // wiki hulls
  const wiki = S.tab === "knowledge" && S.selectedWiki != null ? S.config.knowledge.wikis[S.selectedWiki] : null;
  let wikiReaders = null, wikiWriters = null;
  const unitWiki = {};
  if (wiki && S.analysis) {
    const color = WIKI_COLORS[S.selectedWiki % WIKI_COLORS.length];
    wikiReaders = new Set(); wikiWriters = new Set();
    for (const inst of S.analysis.wikis.filter((w) => w.name === wiki.name)) {
      inst.readers.forEach((r) => wikiReaders.add(r)); inst.writers.forEach((r) => wikiWriters.add(r));
      const ids = [...new Set(inst.members.map((m) => visibleId(S.nameToId[m])).filter(Boolean))];
      if (S.mode === "rows") {
        // in split levels a unit wiki is drawn on the unit's own frame; other scopes are shown by outlines
        if (inst.scope === "team") unitWiki[S.nameToId[inst.owner]] = { color, id: inst.id };
        continue;
      }
      if (!ids.length) continue;
      const xs = ids.map((i) => S.pos[i].x), ys = ids.map((i) => S.pos[i].y);
      const pad = inst.scope === "team" ? 7 : 11;
      const x0 = Math.min(...xs) - pad, y0 = Math.min(...ys) - pad, x1 = Math.max(...xs) + W + pad, y1 = Math.max(...ys) + H + pad;
      g.append(s("rect", { class: "hull", x: x0, y: y0, width: x1 - x0, height: y1 - y0, rx: 14, fill: color, stroke: color }));
      g.append(s("text", { class: "hull-label", x: x0 + 8, y: y0 - 4, fill: color }, inst.id));
    }
  }

  // edges
  const flashFor = {};
  S.flashes = S.flashes.filter((f) => f.until > now);
  S.flashes.forEach((f) => (flashFor[f.key] = f.cls));
  if (S.mode === "rows") {
    for (const f of S.frames) {
      const p = S.pos[f.parent]; if (!p) continue;
      const pname = nameOf(f.parent);
      const flash = f.ids.map((c) => flashFor[`${pname}>${nameOf(c)}`]).find(Boolean);
      const fx = (f.x0 + f.x1) / 2, fy = f.y0 - FRAME_TOP;
      const uw = unitWiki[f.parent];
      g.append(s("rect", { class: uw ? "hull" : "unit", x: f.x0 - 8, y: fy, width: f.x1 - f.x0 + 16, height: f.y1 - fy + 8, rx: 10,
        ...(uw ? { fill: uw.color, stroke: uw.color } : {}) }));
      g.append(s("text", { class: uw ? "hull-label" : "unit-label", x: f.x0 - 2, y: fy + 13, ...(uw ? { fill: uw.color } : {}) },
        uw ? uw.id : `${f.part ? "… " : ""}${trunc(pname, 30)}'s unit${f.part ? " (cont.)" : ""}`));
      const sx = p.x + W / 2, sy = p.y + H, my = (sy + fy) / 2;
      // connectors cross rows, so keep them faint unless they belong to the selected position
      const mine = S.selected && (S.selected === f.parent || f.ids.includes(S.selected));
      g.append(s("path", { class: "edge" + (flash ? " " + flash : mine ? " strong" : " faint"), d: `M${sx},${sy} C${sx},${my} ${fx},${my} ${fx},${fy}` }));
    }
  }
  for (const [id, p] of Object.entries(S.mode === "rows" ? {} : S.pos)) {
    const n = info(id).node;
    if (!n.children || S.collapsed.has(n._id)) continue;
    const midY = p.y + H + GY / 2;
    for (const c of n.children) {
      const q = S.pos[c._id]; if (!q) continue;
      const key = `${nameOf(n._id)}>${nameOf(c._id)}`;
      const d = q.stackedUnder ? `M${p.x + INDENT / 2},${p.y + H} V${q.y + H / 2} H${q.x}`
        : `M${p.x + W / 2},${p.y + H} V${midY} H${q.x + W / 2} V${q.y}`;
      g.append(s("path", { class: "edge" + (flashFor[key] ? " " + flashFor[key] : ""), d }));
    }
  }

  // contact lines for the selected position
  const showContacts = S.selected && S.analysis && (S.tab === "communication" || S.tab === "position");
  const contactNames = new Set();
  if (showContacts) {
    const from = S.selected; const a = center(from);
    for (const c of S.analysis.contacts[nameOf(from)] || []) {
      contactNames.add(c.to);
      const tid = visibleId(S.nameToId[c.to]); if (!tid || tid === from) continue;
      const bpt = center(tid);
      const mx = (a.x + bpt.x) / 2, my = (a.y + bpt.y) / 2;
      const dx = bpt.x - a.x, dy = bpt.y - a.y, len = Math.hypot(dx, dy) || 1;
      const bend = Math.min(120, len * 0.25);
      g.append(s("path", { class: "contact", stroke: KIND_COLOR[c.kind],
        d: `M${a.x},${a.y} Q${mx - (dy / len) * bend},${my + (dx / len) * bend} ${bpt.x},${bpt.y}` }));
    }
  }

  // live message pings
  S.pings = S.pings.filter((p) => p.until > now);
  for (const p of S.pings) {
    const f = visibleId(S.nameToId[p.from]), t = visibleId(S.nameToId[p.to]);
    if (!f || !t || f === t) continue;
    const a = center(f), bpt = center(t);
    g.append(s("path", { class: "ping", opacity: ((p.until - now) / 2200).toFixed(2),
      d: `M${a.x},${a.y} Q${(a.x + bpt.x) / 2},${Math.min(a.y, bpt.y) - 60} ${bpt.x},${bpt.y}` }));
  }

  // nodes
  for (const [idStr, p] of Object.entries(S.pos)) {
    const id = Number(idStr);
    const n = info(id).node, m = meta(id), name = m ? m.name : "…";
    const fam = family(n.model), color = FAM_COLOR[fam];
    let cls = "node";
    if (S.selected === id) cls += " sel";
    if (run && run.states[name]) cls += " st-" + run.states[name];
    if (wikiReaders && !wikiReaders.has(name) && !wikiWriters.has(name)) cls += " dim";
    if (showContacts && S.selected !== id && contactNames.size && !contactNames.has(name)) cls += " dim";
    const gn = s("g", { class: cls, transform: `translate(${p.x},${p.y})`, style: "cursor:pointer",
      onclick: (e) => { e.stopPropagation(); select(id); } });
    gn.append(s("title", {}, `${name} — ${titleCase(n.role)}${gloss(n.role) ? " (" + gloss(n.role) + ")" : ""}\nmodel: ${n.model}${n.effort ? " (" + n.effort + ")" : ""}`));
    if (run) gn.append(s("rect", { class: "state", x: -4, y: -4, width: W + 8, height: H + 8, rx: 13 }));
    const strokeAttrs = wikiWriters && wikiWriters.has(name) ? { stroke: WIKI_COLORS[S.selectedWiki % WIKI_COLORS.length], "stroke-width": 2.5 }
      : wikiReaders && wikiReaders.has(name) ? { stroke: WIKI_COLORS[S.selectedWiki % WIKI_COLORS.length], "stroke-width": 2, "stroke-dasharray": "4 3" } : {};
    gn.append(s("rect", { class: "body", width: W, height: H, rx: 10, ...strokeAttrs }));
    gn.append(s("rect", { x: 0, y: 8, width: 4, height: H - 16, rx: 2, fill: color }));
    gn.append(s("text", { class: "role", x: 14, y: 21 }, trunc(titleCase(n.role), 21)));
    gn.append(s("text", { class: "nm", x: 14, y: 36 }, trunc(name, 26)));
    const label = trunc(modelLabel(n.model), 16), bw = label.length * 6.2 + 12;
    gn.append(s("rect", { x: 13, y: 46, width: bw, height: 18, rx: 5, fill: color }));
    gn.append(s("text", { class: "mdl", x: 19, y: 59 }, label));
    if (n.effort) gn.append(s("text", { class: "eff", x: 13 + bw + 6, y: 59 }, n.effort));
    // icons: wiki access / contacts
    if (S.analysis && m) {
      const nW = S.analysis.wikis.filter((w) => w.readers.includes(name) || w.writers.includes(name)).length;
      const nC = (S.analysis.contacts[name] || []).length;
      let icons = [];
      if (nW) icons.push(`▤${nW}`);
      if (nC) icons.push(`⇄${nC}`);
      if (icons.length) gn.append(s("text", { class: "ico", x: W - 8, y: 18, "text-anchor": "end" }, icons.join("  ")));
    }
    if (run && run.costs[name] != null) gn.append(s("text", { class: "cost", x: W - 8, y: H - 10, "text-anchor": "end" }, money(run.costs[name])));
    if (n.children && n.children.length) {
      const collapsed = S.collapsed.has(id);
      const stackedKids = S.pos[n.children[0]._id] && S.pos[n.children[0]._id].stackedUnder === id;
      const tg = s("g", { class: "toggle", transform: stackedKids ? `translate(${W - 14},${H})` : `translate(${W / 2},${H + 9})`,
        onclick: (e) => { e.stopPropagation(); collapsed ? S.collapsed.delete(id) : S.collapsed.add(id); renderChart(); } });
      tg.append(s("circle", { r: collapsed ? 11 : 8 }));
      tg.append(s("text", { y: 3.5 }, collapsed ? `+${subtreeSize(n)}` : "−"));
      gn.append(tg);
    }
    g.append(gn);
  }
  renderLegend();
}

function renderLegend() {
  const lg = $("#legend");
  const items = [];
  if (S.selected && S.analysis && (S.tab === "communication" || S.tab === "position")) {
    const kinds = [...new Set((S.analysis.contacts[nameOf(S.selected)] || []).map((c) => c.kind))];
    if (kinds.length) {
      items.push(h("div", { class: "small", style: "font-weight:600;margin-bottom:3px" }, `${nameOf(S.selected)} can contact`));
      kinds.forEach((k) => items.push(h("div", { class: "item" }, h("span", { class: "sw", style: `border-color:${KIND_COLOR[k]}` }), KIND_NAME[k])));
    }
  } else if (S.tab === "knowledge" && S.selectedWiki != null) {
    const c = WIKI_COLORS[S.selectedWiki % WIKI_COLORS.length];
    items.push(h("div", { class: "item" }, h("span", { class: "box", style: `border:2.5px solid ${c}` }), "can write"));
    items.push(h("div", { class: "item" }, h("span", { class: "box", style: `border:2px dashed ${c}` }), "read only"));
    items.push(h("div", { class: "item" }, h("span", { class: "box", style: `background:${c};opacity:.25` }), "one wiki instance"));
  } else {
    const fams = new Set(Object.values(S.idx).map((e) => family(e.node.model)));
    const names = { opus: "Opus", sonnet: "Sonnet", haiku: "Haiku", paid: "OpenRouter (paid)", free: "OpenRouter (free)", mock: "Mock", unknown: "Unknown model" };
    Object.keys(names).filter((f) => fams.has(f)).forEach((f) => items.push(h("div", { class: "item" }, h("span", { class: "box", style: `background:${FAM_COLOR[f]}` }), names[f])));
  }
  lg.replaceChildren(...items);
  lg.hidden = !items.length;
}

/* ---------------- selection ---------------- */
function select(id) {
  S.selected = id; S.selectedLevel = null;
  const e = info(id), layer = S.layers[e.depth];
  const uniform = layer.every((n) => n.model === e.node.model && (n.effort || "") === (e.node.effort || ""));
  S.scope = uniform && layer.length > 1 ? "level" : "node";
  if (S.tab !== "communication" && S.tab !== "run") S.tab = "position";
  renderAll();
}
function selectLevel(d) { S.selectedLevel = d; S.selected = null; S.tab = "structure"; renderAll(); }

/* ---------------- panels ---------------- */
function renderAll() { renderMetrics(); renderChart(); renderTabs(); renderPanel(); }
function renderTabs() { document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === S.tab)); }
function renderMetrics() {
  const a = S.analysis; const el = $("#metrics");
  if (!a) return el.replaceChildren();
  const m = a.metrics;
  const chip = (label, val, title) => h("span", { class: "chip", title }, label + " ", h("b", {}, val));
  el.replaceChildren(
    chip("agents", m.n_agents), chip("workers", m.n_workers), chip("managers", m.n_managers), chip("depth", m.depth),
    chip("avg span", m.avg_span), chip("mgr:worker", m.manager_ratio), chip("wikis", m.wikis),
    chip("contact", m.communication, "communication rules beyond delegate/report"),
    chip("≈ $/round", money(a.est_round_cost, 3), "rough cost if every position made one typical call"),
  );
}
function renderPanel() {
  const panel = $("#panel");
  const act = document.activeElement, key = act && act.dataset ? act.dataset.k : null;
  const ss = act && act.selectionStart, se = act && act.selectionEnd, scroll = panel.scrollTop;
  const build = { structure: structurePanel, position: positionPanel, knowledge: knowledgePanel, communication: commPanel, run: runPanel }[S.tab];
  panel.replaceChildren(...[].concat(build()));
  panel.scrollTop = scroll;
  if (key) {
    const e = panel.querySelector(`[data-k="${key}"]`);
    if (e) { e.focus(); try { e.setSelectionRange(ss, se); } catch (_) { /* not a text input */ } }
  }
}
function field(label, input, hint) {
  return h("div", { class: "field" }, h("label", {}, label), hint ? h("div", {}, input, h("div", { class: "muted small" }, hint)) : input);
}
function check(label, value, onchange, help, k) {
  return h("label", { class: "check" }, h("input", { type: "checkbox", checked: value, "data-k": k, onchange: (e) => onchange(e.target.checked) }),
    h("span", {}, label, help ? h("small", {}, help) : null));
}
function select_(options, value, onchange, k) {
  const e = h("select", { "data-k": k, onchange: (ev) => onchange(ev.target.value) },
    options.map((o) => (o.group ? h("optgroup", { label: o.group }, o.items.map((i) => h("option", { value: i.value }, i.label))) : h("option", { value: o.value }, o.label))));
  e.value = value == null ? "" : value;
  return e;
}
function text(value, onchange, k, attrs = {}) {
  return h("input", { type: "text", value: value || "", "data-k": k, ...attrs, onchange: (e) => onchange(e.target.value.trim()) });
}
function num(value, onchange, k, attrs = {}) {
  return h("input", { type: "number", value: value == null ? "" : value, "data-k": k, ...attrs,
    onchange: (e) => onchange(e.target.value === "" ? null : Number(e.target.value)) });
}
function modelSelect(value, onchange, k) {
  const ms = S.catalog.models;
  const opt = (m) => ({ value: m.alias, label: `${m.alias} · ${m.free ? "free" : m.provider === "mock" ? "offline" : `$${m.input}/$${m.output}`}` });
  const groups = [
    { group: "Anthropic", items: ms.filter((m) => m.provider === "anthropic").map(opt) },
    { group: "OpenRouter", items: ms.filter((m) => m.provider === "openrouter" && !m.free).map(opt) },
    { group: "OpenRouter free (rate-limited)", items: ms.filter((m) => m.free).map(opt) },
    { group: "Offline", items: ms.filter((m) => m.provider === "mock").map(opt) },
  ].filter((g) => g.items.length);
  const known = ms.some((m) => m.alias === value);
  const options = [...(known ? [] : [{ value, label: `${value} (custom)` }]), ...groups, { value: "__custom", label: "Other OpenRouter model…" }];
  return select_(options, value, (v) => {
    if (v === "__custom") {
      const id = prompt("OpenRouter model id (e.g. qwen/qwen3.7-flash). Must support tool calling.");
      if (id) onchange(id.startsWith("openrouter:") ? id : "openrouter:" + id.trim()); else renderPanel();
    } else onchange(v);
  }, k);
}
const effortOptions = () => [{ value: "", label: "model default" }, ...S.catalog.efforts.map((e) => ({ value: e, label: EFFORT_NOTE[e] }))];

function structurePanel() {
  const c = S.config, out = [];
  out.push(h("h2", {}, "Organization"));
  out.push(field("Name", text(c.name, (v) => v && mutate(() => (c.name = v)), "org-name")));
  out.push(field("Description", text(c.description, (v) => mutate(() => (c.description = v)), "org-desc")));
  out.push(h("h3", {}, "Levels"));
  out.push(h("p", {}, "Each level is a band on the chart. Change how many reports each manager has, insert or remove a management layer, or click a level to edit every position in it."));
  const list = h("div", { class: "level-list" });
  const last = S.layers.length - 1;
  S.layers.forEach((layer, d) => {
    const roles = [...new Set(layer.map((n) => n.role))].join(" / ");
    const models = {}; layer.forEach((n) => { const k = n.model + (n.effort ? "/" + n.effort : ""); models[k] = (models[k] || 0) + 1; });
    const modelTxt = Object.entries(models).map(([k, v]) => (Object.keys(models).length > 1 ? `${k} ×${v}` : k)).join(", ");
    const spans = [...new Set(layer.filter((n) => n.children && n.children.length).map((n) => n.children.length))];
    const spanTxt = spans.length ? (spans.length === 1 ? `${spans[0]} reports each` : `${Math.min(...spans)}–${Math.max(...spans)} reports`) : "hands-on workers";
    const item = h("div", { class: "level" + (S.selectedLevel === d ? " sel" : "") },
      h("div", { class: "linklike", onclick: () => selectLevel(d) },
        h("div", { class: "title" }, `L${d} · ${roles}`),
        h("div", { class: "sub" }, `${layer.length} position${layer.length > 1 ? "s" : ""} · ${modelTxt} · ${spanTxt}`)),
      h("div", { class: "ctl" },
        d < last ? h("span", { class: "span-ctl" },
          h("button", { title: "One fewer report per manager", onclick: () => changeSpan(d, -1) }, "−"),
          h("button", { title: "One more report per manager", onclick: () => changeSpan(d, +1) }, "+")) : null,
        d < last ? h("button", { title: "Insert a management layer below this level", onclick: () => { S.insertForm = d; renderPanel(); } }, "+ layer") : null,
        h("button", { class: "danger", title: "Remove this level", onclick: () => removeLayer(d) }, "✕")));
    list.append(item);
    if (S.insertForm === d) list.append(insertLayerForm(d));
  });
  out.push(list);
  if (S.selectedLevel != null) out.push(levelEditor(S.selectedLevel));

  out.push(h("h3", {}, "Quick build"));
  out.push(h("p", {}, "Replace the chart with a balanced org: a fixed number of workers under a chosen number of management layers."));
  const qb = S.quick = S.quick || { workers: 8, depth: 2, top: "sonnet", mid: defaultManagerModel(), worker: defaultWorker().model };
  out.push(field("Workers", num(qb.workers, (v) => (qb.workers = v || 1), "qb-w", { min: 1, max: 64 })));
  out.push(field("Mgmt layers", num(qb.depth, (v) => (qb.depth = v || 0), "qb-d", { min: 0, max: 6 })));
  out.push(field("Top model", modelSelect(qb.top, (v) => (qb.top = v), "qb-top")));
  out.push(field("Managers", modelSelect(qb.mid, (v) => (qb.mid = v), "qb-mid")));
  out.push(field("Workers' model", modelSelect(qb.worker, (v) => (qb.worker = v), "qb-wm")));
  out.push(h("div", { class: "row end" }, h("button", { onclick: quickBuild }, "Build")));

  out.push(h("h3", {}, "Behaviour"));
  out.push(field("Memory", select_([{ value: "persistent", label: "persistent: remembers past assignments" }, { value: "fresh", label: "fresh: starts clean each assignment" }],
    c.memory, (v) => mutate(() => (c.memory = v)), "mem")));
  out.push(check("Tell managers which model each report runs", c.disclose_models, (v) => mutate(() => (c.disclose_models = v)), null, "disc"));
  out.push(field("Manager turns", num(c.manager_max_turns, (v) => v && mutate(() => (c.manager_max_turns = v)), "mmt", { min: 1 })));
  out.push(field("Worker turns", num(c.worker_max_turns, (v) => v && mutate(() => (c.worker_max_turns = v)), "wmt", { min: 1 })));
  return out;
}
function insertLayerForm(d) {
  if (!S.insertDraft || S.insertDraft.depth !== d) {
    const below = S.layers[d + 1] ? S.layers[d + 1][0].role : null;
    S.insertDraft = { depth: d, role: rankBetween(S.layers[d][0].role, below), model: defaultManagerModel(), groups: 2 };
  }
  const f = S.insertDraft;
  return h("div", { class: "card" },
    h("div", { class: "small muted" }, `New managers go between L${d} and L${d + 1}. Each L${d} manager's reports are split into groups, one per new manager.`),
    field("Rank", text(f.role, (v) => (f.role = v || "lieutenant"), "il-role")),
    field("Model", modelSelect(f.model, (v) => (f.model = v), "il-model")),
    field("Groups", num(f.groups, (v) => (f.groups = v || 1), "il-g", { min: 1 }), "new managers per existing manager"),
    h("div", { class: "row end" }, h("button", { onclick: () => { S.insertForm = null; renderPanel(); } }, "Cancel"),
      h("button", { class: "primary", onclick: () => { S.insertForm = null; insertLayerBelow(d, f.role, f.model, f.groups); } }, "Insert layer")));
}
function levelEditor(d) {
  const layer = S.layers[d], first = layer[0];
  const same = (k) => layer.every((n) => (n[k] ?? "") === (first[k] ?? "")) ? first[k] : null;
  return h("div", { class: "card sel" },
    h("div", { class: "head" }, h("strong", {}, `Edit all ${layer.length} position${layer.length > 1 ? "s" : ""} at L${d}`)),
    field("Role", text(same("role") || "", (v) => v && setField(layer, "role", v), "lv-role", { placeholder: "mixed" })),
    field("Model", modelSelect(same("model") || first.model, (v) => setField(layer, "model", v), "lv-model")),
    field("Effort", select_(effortOptions(), same("effort") || "", (v) => setField(layer, "effort", v || null), "lv-eff")));
}
async function quickBuild() {
  const q = S.quick;
  if (!confirm("Replace the current chart with a generated org? (Undo is available.)")) return;
  try {
    const res = await api("/api/generate", { name: S.config.name, workers: q.workers, depth: q.depth,
      ladder: [{ model: q.top, effort: "low" }, { model: q.mid }], worker: { model: q.worker } });
    mutate(() => { S.config.tree = res.config.tree; S.collapsed.clear(); });
    setTimeout(() => fit(0.8), 50);
  } catch (e) { toast(e.message); }
}

function positionPanel() {
  if (!S.selected) return [h("h2", {}, "No position selected"), h("p", {}, "Click a card on the chart to see its role, model, knowledge access and who it can talk to.")];
  const id = S.selected, e = info(id), n = e.node, m = meta(id) || {};
  const out = [];
  out.push(h("h2", {}, titleCase(n.role)));
  out.push(h("div", { class: "muted small" }, `${m.name || "…"} · level ${e.depth} of ${S.layers.length - 1}`));
  if (gloss(n.role)) out.push(h("p", { class: "small gloss" }, gloss(n.role)));
  const dl = h("dl", { class: "facts" });
  const add = (k, v) => dl.append(h("dt", {}, k), h("dd", {}, v));
  add("Reports to", e.parent ? h("span", { class: "linklike", onclick: () => select(e.parent._id) }, nameOf(e.parent._id)) : "the client");
  if (n.children && n.children.length) add("Direct reports", n.children.map((c, i) => [i ? ", " : "", h("span", { class: "linklike", onclick: () => select(c._id) }, nameOf(c._id))]));
  else add("Direct reports", "none (hands-on)");
  if (subtreeSize(n)) add("Under them", `${subtreeSize(n)} positions`);
  add("≈ per call", m.est_call_cost != null ? money(m.est_call_cost) : "–");
  out.push(dl);

  out.push(h("h3", {}, "Model"));
  const layer = S.layers[e.depth], sameRole = Object.values(S.idx).filter((x) => x.node.role === n.role).length;
  out.push(h("div", { class: "scope" },
    ["node", "level", "role"].map((sc) => h("button", { class: S.scope === sc ? "on" : "", onclick: () => { S.scope = sc; renderPanel(); } },
      sc === "node" ? "This position" : sc === "level" ? `Level ${e.depth} (${layer.length})` : `All "${trunc(n.role, 10)}" (${sameRole})`))));
  out.push(field("Model", modelSelect(n.model, (v) => setField(targets(id), "model", v), "p-model")));
  out.push(field("Effort", select_(effortOptions(), n.effort || "", (v) => setField(targets(id), "effort", v || null), "p-eff"),
    "thinking depth (Claude effort / OpenRouter reasoning)"));
  out.push(field("Max output", num(n.max_tokens, (v) => setField(targets(id), "max_tokens", v), "p-mt", { placeholder: String(m.max_tokens || "") }), "tokens per call"));
  out.push(field("Max turns", num(n.max_turns, (v) => setField(targets(id), "max_turns", v), "p-turns", { placeholder: "org default" })));
  if (n.children && n.children.length) {
    out.push(check("Can also do hands-on work", n.can_do_work, (v) => setField(targets(id), "can_do_work", v || null), "gets workspace, code and research tools", "p-cdw"));
    out.push(check("Can review the workspace", n.review_tools !== false, (v) => setField(targets(id), "review_tools", v ? null : false), "read files and run public tests", "p-rev"));
  }

  out.push(h("h3", {}, "Rank and role"));
  out.push(field("Rank", text(n.role, (v) => v && setField(targets(id), "role", v), "p-role", { list: "rank-list" }), "any title works; it's only a label"));
  out.push(h("datalist", { id: "rank-list" }, [...S.catalog.ranks.ladder, S.catalog.ranks.worker, "éclaireur", "sapeur", S.catalog.ranks.solo].map((r) => h("option", { value: r }))));
  out.push(field("Name", text(n.name, (v) => mutate(() => { if (v) n.name = v; else delete n.name; }), "p-name", { placeholder: m.name || "" }), "leave empty for the automatic name"));
  out.push(h("div", { class: "field wide" }, h("label", {}, "Role notes (added to the system prompt)"),
    h("textarea", { rows: 3, "data-k": "p-prompt", onchange: (ev) => setField(targets(id), "prompt", ev.target.value.trim() || null) }, n.prompt || "")));

  out.push(h("h3", {}, "Knowledge access"));
  const ws = S.analysis ? S.analysis.wikis.filter((w) => w.readers.includes(m.name) || w.writers.includes(m.name)) : [];
  out.push(ws.length ? h("div", { class: "small" }, ws.map((w) => h("div", {}, h("code", {}, w.id), ` · ${w.label} · ${w.writers.includes(m.name) ? "read/write" : "read only"}`)))
    : h("p", { class: "small" }, "No wikis. Add one in the Knowledge tab."));

  out.push(h("h3", {}, "Can contact directly"));
  const cs = (S.analysis && S.analysis.contacts[m.name]) || [];
  out.push(cs.length ? h("div", { class: "contact-list" }, cs.map((c) => h("div", {}, h("span", { class: "kind-dot", style: `background:${KIND_COLOR[c.kind]}` }),
    h("span", { class: "linklike", onclick: () => S.nameToId[c.to] && select(S.nameToId[c.to]) }, c.to), h("span", { class: "muted small" }, ` ${KIND_NAME[c.kind].toLowerCase()}`))))
    : h("p", { class: "small" }, "Only delegate and report along the tree. Open up contact in the Communication tab."));

  out.push(h("h3", {}, "Structure"));
  out.push(h("div", { class: "row" },
    h("button", { onclick: () => addReport(id) }, "+ Direct report"),
    e.parent ? h("button", { onclick: () => duplicate(id) }, "Duplicate") : null,
    h("button", { onclick: () => insertManagerAbove(id) }, "Insert manager above"),
    e.parent ? h("button", { class: "danger", onclick: () => removeNode(id) }, "Remove") : null));
  return out;
}

function knowledgePanel() {
  const k = S.config.knowledge = S.config.knowledge || { wikis: [] };
  const out = [h("h2", {}, "Knowledge sharing"),
    h("p", {}, "Wikis are shared pages agents can read, search and write. Scope decides who shares one: the whole army, each unit (an officer and their direct subordinates), or each division (everyone under a position at a given level). Select a wiki to see its reach on the chart.")];
  const addWiki = (preset) => mutate(() => {
    let base = preset.name, i = 2;
    while (k.wikis.some((w) => w.name === preset.name)) preset.name = `${base}-${i++}`;
    k.wikis.push(preset); S.selectedWiki = k.wikis.length - 1;
  });
  out.push(h("div", { class: "row" },
    h("button", { title: "one wiki for the whole army", onclick: () => addWiki({ name: "ordres", scope: "org" }) }, "+ Army-wide"),
    h("button", { title: "one notebook per unit: an officer and their direct subordinates", onclick: () => addWiki({ name: "carnet", scope: "team" }) }, "+ Per unit"),
    h("button", { disabled: S.layers.length < 3, title: "one dossier per division (needs at least 3 levels)", onclick: () => addWiki({ name: "dossier", scope: "department", level: 1 }) }, "+ Per division")));
  if (!k.wikis.length) out.push(h("p", { class: "muted small", style: "margin-top:10px" }, "No wikis: knowledge moves only through orders and reports."));
  out.push(h("p", { class: "small" }, "To read what agents wrote, with who changed what, open the ", h("span", { class: "linklike", onclick: openWikiBrowser }, "wiki browser"), "."));
  k.wikis.forEach((w, i) => {
    const color = WIKI_COLORS[i % WIKI_COLORS.length];
    const insts = S.analysis ? S.analysis.wikis.filter((x) => x.name === w.name) : [];
    const upd = (key, val) => mutate(() => { if (val === null || val === "") delete w[key]; else w[key] = val; });
    const card = h("div", { class: "card" + (S.selectedWiki === i ? " sel" : ""), onclick: (ev) => { if (S.selectedWiki !== i && ev.target.tagName !== "BUTTON") { S.selectedWiki = i; renderChart(); renderPanel(); } } },
      h("div", { class: "head" }, h("span", { class: "swatch", style: `background:${color}` }),
        text(w.name, (v) => v && /^[A-Za-z0-9_-]+$/.test(v) ? upd("name", v) : toast("Use letters, digits, - and _"), `w${i}-name`),
        h("button", { class: "danger icon", title: "Delete wiki", onclick: (ev) => { ev.stopPropagation(); mutate(() => { k.wikis.splice(i, 1); S.selectedWiki = null; }); } }, "✕")),
      field("Scope", select_([{ value: "org", label: "Whole army" }, { value: "team", label: "Each unit (officer + direct subordinates)" }, { value: "department", label: "Each division (everyone under a position)" }],
        w.scope || "org", (v) => mutate(() => { w.scope = v; if (v === "department" && !w.level) w.level = 1; }), `w${i}-scope`)),
      (w.scope === "department") ? field("Divisions at", select_(S.layers.map((layer, d) => ({ value: String(d), label: `L${d} · ${[...new Set(layer.map((n) => n.role))].join("/")} (${layer.length})` })).slice(1, -1).concat(S.layers.length <= 2 ? [{ value: "1", label: "L1" }] : []),
        String(w.level ?? 1), (v) => upd("level", Number(v)), `w${i}-lvl`), "one wiki per position at this level, shared with everyone under it") : null,
      field("Who reads", select_([{ value: "all", label: "everyone in scope" }, { value: "managers", label: "managers only" }, { value: "workers", label: "workers only" }], w.read || "all", (v) => upd("read", v === "all" ? null : v), `w${i}-r`)),
      field("Who writes", select_([{ value: "all", label: "everyone in scope" }, { value: "managers", label: "managers only" }, { value: "workers", label: "workers only" }], w.write || "all", (v) => upd("write", v === "all" ? null : v), `w${i}-w`)),
      check("Oversight: managers above can read it", w.oversight !== false, (v) => upd("oversight", v ? null : false), null, `w${i}-o`),
      check("Post the client's brief on it", !!w.publish_brief, (v) => upd("publish_brief", v || null), "everyone in scope can read the original assignment", `w${i}-b`),
      field("Purpose", text(w.description, (v) => upd("description", v || null), `w${i}-d`, { placeholder: "shown to agents (optional)" })),
      h("div", { class: "muted small" }, insts.length === 1 ? `${insts[0].members.length} members · ${insts[0].writers.length} can write · ${insts[0].readers.length} can read`
        : `${insts.length} separate wikis` + (insts[0] ? ` · e.g. ${insts[0].id}: ${insts[0].members.length} members` : "")));
    out.push(card);
  });
  return out;
}

function commPanel() {
  const c = S.config.communication = S.config.communication || {};
  const upd = (key, val) => mutate(() => (c[key] = val));
  const maxD = Math.max(0, S.layers.length - 2);
  const out = [h("h2", {}, "Signals: communication rules"),
    h("p", {}, "Orders always flow down the chain of command and reports flow up. These rules add direct contact: a question that gets an answer (ask) or a note left in someone's inbox (message). Select a position to see who it can reach.")];
  out.push(h("h3", {}, "Who can talk"));
  out.push(check("Commander ↔ direct subordinate", !!c.chain, (v) => upd("chain", v), "talk mid-assignment, not just give orders and report", "c-chain"));
  out.push(check("Comrades in the same unit", !!c.peers, (v) => upd("peers", v), "positions with the same commander", "c-peers"));
  out.push(check("Same rank, other units", !!c.cross_team, (v) => upd("cross_team", v), null, "c-cross"));
  out.push(check("Open ranks: anyone can contact anyone", !!c.open, (v) => upd("open", v), "overrides the other rules", "c-open"));
  out.push(h("h3", {}, "Skip-level"));
  const slider = (key, label, desc) => {
    const val = c[key] || 0;
    return h("div", { class: "field" }, h("label", {}, label), h("div", {},
      h("input", { type: "range", min: 0, max: Math.max(maxD, val), step: 1, value: val, "data-k": "c-" + key, style: "width:100%",
        onchange: (e) => upd(key, Number(e.target.value)) }),
      h("div", { class: "muted small" }, val ? desc(val) : "not allowed")));
  };
  out.push(slider("skip_up", "Upward", (v) => `may contact up to ${v} rank${v > 1 ? "s" : ""} above their own commander`));
  out.push(slider("skip_down", "Downward", (v) => `officers may contact up to ${v} rank${v > 1 ? "s" : ""} below their direct subordinates`));
  out.push(check("Copy the officers who are skipped", c.cc_skipped !== false, (v) => upd("cc_skipped", v), "they get a cc in their inbox", "c-cc"));
  out.push(h("h3", {}, "How"));
  const modes = c.modes || ["ask", "message"];
  const setMode = (mode, on) => upd("modes", on ? [...new Set([...modes, mode])] : modes.filter((x) => x !== mode));
  out.push(check("Ask", modes.includes("ask"), (v) => setMode("ask", v), "question and answer; the recipient answers from what they know so far", "c-ask"));
  out.push(check("Message", modes.includes("message"), (v) => setMode("message", v), "a note they see the next time they act", "c-msg"));
  out.push(field("Limit", num(c.max_messages ?? 8, (v) => upd("max_messages", v ?? 0), "c-max", { min: 0 }), "messages per agent per run"));
  if (S.analysis) {
    const pairs = Object.values(S.analysis.contacts).reduce((a, l) => a + l.length, 0);
    const reach = Object.values(S.analysis.contacts).filter((l) => l.length).length;
    out.push(h("p", { class: "small", style: "margin-top:12px" }, pairs ? `${pairs} allowed contact pairs; ${reach} of ${S.analysis.metrics.n_agents} positions can reach someone.` : "Strict tree: nobody can contact anyone outside delegate/report."));
  }
  out.push(h("h3", {}, "Legend"));
  out.push(h("div", { class: "small" }, Object.entries(KIND_NAME).filter(([k]) => k !== "report").map(([k, label]) =>
    h("div", {}, h("span", { class: "kind-dot", style: `background:${KIND_COLOR[k]}` }), k === "manager" ? "Commander ↔ subordinate" : label))));
  return out;
}

/* ---------------- runs ---------------- */
function runPanel() {
  const r = S.run, running = r && r.status === "running";
  const cfg = S.runCfg = S.runCfg || { task: S.catalog.tasks[0].presets[0].spec, seed: 1, budget: 0.5, mock: true, confirm: false };
  const out = [h("h2", {}, "Test run"),
    h("p", {}, "Run this org on a task and watch it work. Mock mode uses scripted models: free and instant, good for checking the wiring before spending money.")];
  const presetOpts = S.catalog.tasks.map((f) => ({ group: f.label, items: f.presets.map((p) => ({ value: p.spec, label: p.label })) }));
  const isPreset = S.catalog.tasks.some((f) => f.presets.some((p) => p.spec === cfg.task));
  out.push(field("Task", select_([...presetOpts, { value: "__custom", label: "Custom spec…" }], isPreset && !S.customTask ? cfg.task : "__custom",
    (v) => { if (v === "__custom") S.customTask = true; else { S.customTask = false; cfg.task = v; } renderPanel(); }, "r-task")));
  if (S.customTask || !isPreset) out.push(field("Spec", text(cfg.task, (v) => (cfg.task = v), "r-spec", { placeholder: "e.g. code:n=8,coupling=0.5" })));
  out.push(field("Seed", num(cfg.seed, (v) => (cfg.seed = v || 1), "r-seed", { min: 1 })));
  out.push(field("Budget", num(cfg.budget, (v) => (cfg.budget = v || 0.1), "r-budget", { min: 0.01, step: 0.05 }), "hard cap in USD for this run"));
  out.push(check("Mock models (no API calls)", cfg.mock, (v) => { cfg.mock = v; renderPanel(); }, null, "r-mock"));
  if (!cfg.mock) {
    const keys = S.catalog.keys;
    out.push(h("p", { class: "small" }, `API keys: Anthropic ${keys.anthropic ? "✓" : "✗ missing"} · OpenRouter ${keys.openrouter ? "✓" : "✗ missing"} (set them in .env and restart).`));
    out.push(check(`I accept spending up to $${Number(cfg.budget).toFixed(2)}`, cfg.confirm, (v) => { cfg.confirm = v; renderPanel(); }, null, "r-confirm"));
  }
  out.push(h("div", { class: "row end" },
    running && S.config.knowledge && S.config.knowledge.wikis && S.config.knowledge.wikis.length ? h("button", { onclick: openWikiBrowser }, "Watch the wikis") : null,
    r && !running ? h("button", { onclick: () => { S.run = null; renderAll(); } }, "Clear") : null,
    h("button", { class: "primary", disabled: running || (!cfg.mock && !cfg.confirm), onclick: startRun }, running ? "Running…" : "Start run")));
  if (r) {
    const secs = ((r.finished || Date.now() / 1000) - r.started).toFixed(1);
    out.push(h("p", { class: "small" }, `${r.status} · ${secs}s · spent ${money(r.total_cost)} of $${Number(r.budget).toFixed(2)}${r.mock ? " (mock)" : ""} · ${r.task}`));
    if (r.summary) {
      const sm = r.summary;
      out.push(h("div", { class: "result" },
        h("div", { class: "big" }, `score ${Number(sm.score).toFixed(2)} `, h("span", { class: sm.passed ? "pill-ok" : "pill-bad" }, sm.passed ? "PASS" : "not passed")),
        h("div", { class: "small" }, `status ${sm.status} · ${money(sm.cost_usd)} · ${Number(sm.wall_s).toFixed(1)}s · ${sm.n_calls} calls`),
        sm.answer != null ? h("div", { class: "small" }, `answer: ${trunc(String(sm.answer), 120)}`) : null,
        sm.grade && sm.grade.expected != null ? h("div", { class: "small" }, `expected: ${sm.grade.expected}`) : null,
        h("div", { class: "small muted" }, `transcript: ${sm.run_dir}/transcript.jsonl`),
        S.config.knowledge && S.config.knowledge.wikis && S.config.knowledge.wikis.length ? h("div", { class: "row", style: "margin-top:6px" }, h("button", { onclick: openWikiBrowser }, "Read this run's wikis")) : null));
    }
    if (r.error) out.push(h("pre", { class: "small", style: "white-space:pre-wrap;color:var(--danger)" }, r.error));
    out.push(h("h3", {}, "Activity"));
    const feed = h("div", { class: "feed" });
    (r.feed || []).slice(-250).reverse().forEach((ev) => feed.append(ev));
    out.push(feed);
  }
  return out;
}
function fmtEvent(ev) {
  const d = ev.data || {}, who = ev.node;
  const pv = (t) => (t ? h("span", { class: "pv" }, trunc(t, 220)) : null);
  switch (ev.kind) {
    case "assign": return h("div", { class: "ev assign" }, `${who} → ${d.to}: assignment (${d.chars} chars${d.round > 1 ? ", rework" : ""})`, pv(d.preview));
    case "report": return h("div", { class: "ev report" }, `${who} → ${d.to}: report [${d.status}]`, pv(d.preview));
    case "ask": return h("div", { class: "ev ask" }, `${who} asked ${d.to} (${KIND_NAME[d.kind] || d.kind})${d.cc && d.cc.length ? `, cc ${d.cc.join(", ")}` : ""}`, pv(`Q: ${d.preview}\nA: ${d.answer_preview}`));
    case "message": return h("div", { class: "ev message" }, `${who} → ${d.to}: message (${KIND_NAME[d.kind] || d.kind})${d.cc && d.cc.length ? `, cc ${d.cc.join(", ")}` : ""}`, pv(d.preview));
    case "wiki_write": return h("div", { class: "ev wiki" }, `${who} wrote ${d.wiki} / ${d.page}`, pv(d.preview));
    case "wiki_read": return h("div", { class: "ev wiki" }, `${who} read ${d.wiki}${d.page ? " / " + d.page : d.query ? ` (search "${d.query}")` : ""}`);
    case "inbox": return h("div", { class: "ev" }, `${who} read ${d.count} inbox message${d.count > 1 ? "s" : ""}`);
    case "progress": return h("div", { class: "ev report" }, `workspace score ${Number(d.score).toFixed(2)} after ${who}'s delegation round ${d.round}`);
    case "write": return h("div", { class: "ev" }, `${who} wrote ${d.path} (${d.chars} chars)`);
    case "nudge": return h("div", { class: "ev warn" }, `${who} replied without a tool call (nudged)`);
    case "turn_limit": return h("div", { class: "ev bad" }, `${who} hit the turn limit`);
    case "error": return h("div", { class: "ev bad" }, `${who}: ${d.error}`);
    case "refusal": return h("div", { class: "ev bad" }, `${who} declined`);
    case "undelivered": return h("div", { class: "ev warn" }, `${d.count} inbox message(s) never read`);
    case "tool": return d.ok ? null : h("div", { class: "ev warn" }, `${who}: ${d.name} failed: ${d.error}`);
    default: return null;
  }
}
async function startRun() {
  const cfg = S.runCfg;
  try {
    const res = await api("/api/runs", { config: plain(S.config), task: cfg.task, seed: cfg.seed, budget: cfg.budget, mock: cfg.mock, confirm_spend: cfg.confirm });
    S.run = { key: res.run, status: "running", states: {}, costs: {}, total_cost: 0, budget: cfg.budget, mock: cfg.mock, task: res.task, started: Date.now() / 1000, feed: [], next: 0 };
    S.collapsed.clear();
    renderAll();
    pollRun();
  } catch (e) { toast(e.message); }
}
async function pollRun() {
  const r = S.run; if (!r) return;
  let st;
  try { st = await api(`/api/runs/${r.key}?since=${r.next}`); } catch (e) { toast(e.message); return; }
  if (S.run !== r) return;
  Object.assign(r, { status: st.status, states: st.states, costs: st.costs, total_cost: st.total_cost, summary: st.summary, error: st.error, started: st.started, next: st.next });
  const now = Date.now();
  for (const ev of st.events) {
    if (ev.type === "event") {
      const line = fmtEvent(ev); if (line) r.feed.push(line);
      const d = ev.data || {};
      if (ev.kind === "assign") S.flashes.push({ key: `${ev.node}>${d.to}`, cls: "flash", until: now + 1400 });
      if (ev.kind === "report") S.flashes.push({ key: `${d.to}>${ev.node}`, cls: "flash-up", until: now + 1400 });
      if (ev.kind === "ask" || ev.kind === "message") S.pings.push({ from: ev.node, to: d.to, until: now + 2200 });
    }
  }
  if (st.status !== "running") r.finished = Date.now() / 1000;
  renderChart();
  if (S.tab === "run") renderPanel();
  if (st.status === "running" || S.flashes.length || S.pings.length) setTimeout(pollRun, st.status === "running" ? 500 : 900);
}


/* ---------------- wiki browser ---------------- */
const RANK_COLORS = ["#6250d6", "#2a78d6", "#1baf7a", "#eb6834", "#008300", "#e87ba4", "#b77900", "#e34948", "#52514e"];
function rankColor(role) {
  if (role === "client") return "#8a8983";
  const lad = S.catalog.ranks.ladder, i = lad.indexOf(role);
  if (i >= 0) return RANK_COLORS[Math.min(i, RANK_COLORS.length - 2)];
  let hsh = 0; for (const ch of role || "") hsh = (hsh * 31 + ch.charCodeAt(0)) >>> 0;
  return RANK_COLORS[hsh % RANK_COLORS.length];
}
const rankBadge = (role) => h("span", { class: "rank", style: `background:${rankColor(role)}`, title: gloss(role) || role }, titleCase(role));
function author(r) {
  return h("span", {}, h("strong", {}, r.author), " ", rankBadge(r.role),
    r.level != null ? h("span", { class: "muted small" }, ` L${r.level}`) : null,
    r.model ? h("span", { class: "muted small" }, ` · ${r.model}`) : null);
}
function esc(t) { return t.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }
function inline(t) {
  return esc(t).replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*]+)\*/g, "$1<em>$2</em>");
}
function md(text) {
  // Small, safe Markdown renderer: everything is escaped first; only a few constructs are styled.
  const out = [], lines = (text || "").split("\n");
  let i = 0, list = null, para = [];
  const flushPara = () => { if (para.length) { out.push(`<p>${inline(para.join(" "))}</p>`); para = []; } };
  const flushList = () => { if (list) { out.push(`<${list.tag}>${list.items.map((x) => `<li>${inline(x)}</li>`).join("")}</${list.tag}>`); list = null; } };
  while (i < lines.length) {
    const ln = lines[i];
    if (/^```/.test(ln)) {
      flushPara(); flushList();
      const buf = []; i++;
      while (i < lines.length && !/^```/.test(lines[i])) buf.push(lines[i++]);
      out.push(`<pre><code>${esc(buf.join("\n"))}</code></pre>`); i++; continue;
    }
    const hm = ln.match(/^(#{1,4})\s+(.*)/);
    if (hm) { flushPara(); flushList(); out.push(`<h${hm[1].length}>${inline(hm[2])}</h${hm[1].length}>`); i++; continue; }
    const lm = ln.match(/^\s*([-*]|\d+\.)\s+(.*)/);
    if (lm) { flushPara(); const tag = /\d/.test(lm[1]) ? "ol" : "ul"; if (!list || list.tag !== tag) { flushList(); list = { tag, items: [] }; } list.items.push(lm[2]); i++; continue; }
    if (/^\|.*\|\s*$/.test(ln)) {
      flushPara(); flushList();
      const rows = [];
      while (i < lines.length && /^\|.*\|\s*$/.test(lines[i])) { if (!/^\|[\s:-|]+\|\s*$/.test(lines[i])) rows.push(lines[i]); i++; }
      out.push(`<table>${rows.map((r) => `<tr>${r.trim().slice(1, -1).split("|").map((c) => `<td>${inline(c.trim())}</td>`).join("")}</tr>`).join("")}</table>`);
      continue;
    }
    if (!ln.trim()) { flushPara(); flushList(); i++; continue; }
    flushList(); para.push(ln); i++;
  }
  flushPara(); flushList();
  const d = h("div", { class: "md" }); d.innerHTML = out.join("\n"); return d;
}
function diffLines(a, b) {
  const A = (a || "").split("\n"), B = (b || "").split("\n");
  if (A.length * B.length > 400000) return [...A.map((l) => ["del", l]), ...B.map((l) => ["add", l])];
  const dp = Array.from({ length: A.length + 1 }, () => new Uint16Array(B.length + 1));
  for (let x = A.length - 1; x >= 0; x--) for (let y = B.length - 1; y >= 0; y--)
    dp[x][y] = A[x] === B[y] ? dp[x + 1][y + 1] + 1 : Math.max(dp[x + 1][y], dp[x][y + 1]);
  const out = []; let x = 0, y = 0;
  while (x < A.length && y < B.length) {
    if (A[x] === B[y]) { out.push(["ctx", A[x]]); x++; y++; }
    else if (dp[x + 1][y] >= dp[x][y + 1]) out.push(["del", A[x++]]);
    else out.push(["add", B[y++]]);
  }
  while (x < A.length) out.push(["del", A[x++]]);
  while (y < B.length) out.push(["add", B[y++]]);
  return out;
}

async function openWikiBrowser() {
  S.wb = S.wb || { source: null, data: null, inst: null, page: null, view: "page", rev: null, role: "" };
  let archive = [];
  try { archive = await api("/api/archive"); } catch (e) { toast(e.message); }
  const sel = $("#wb-source");
  const opts = [];
  if (S.run && S.run.key) opts.push(h("option", { value: "live:" + S.run.key }, `Current run (${S.run.status}) · ${S.run.task}`));
  archive.forEach((r) => opts.push(h("option", { value: "run:" + r.run_id },
    `${r.run_id.replace("run_", "")} · ${r.org || "?"} · ${r.task || "?"} · ${r.pages} page${r.pages === 1 ? "" : "s"}, ${r.edits} edit${r.edits === 1 ? "" : "s"}`)));
  if (!opts.length) opts.push(h("option", { value: "" }, "No wiki pages yet: run an org that has wikis"));
  sel.replaceChildren(...opts);
  if (!S.wb.source || ![...sel.options].some((o) => o.value === S.wb.source)) S.wb.source = sel.options[0].value;
  sel.value = S.wb.source;
  $("#wiki-dialog").showModal();
  await loadWikiSource();
}
async function loadWikiSource() {
  const src = S.wb.source;
  if (!src) { S.wb.data = null; return renderWikiBrowser(); }
  try {
    S.wb.data = src.startsWith("live:") ? await api(`/api/runs/${src.slice(5)}/wikis`) : await api(`/api/archive/${src.slice(4)}/wikis`);
  } catch (e) { S.wb.data = null; toast(e.message); }
  const insts = (S.wb.data && S.wb.data.instances) || [];
  const all = insts.flatMap((i) => i.pages.map((p) => [i.id, p.title]));
  if (!all.some(([i, t]) => i === S.wb.inst && t === S.wb.page)) {
    const first = all.find(([i, t]) => t !== "Client brief") || all[0];
    [S.wb.inst, S.wb.page] = first || [null, null]; S.wb.rev = null;
  }
  renderWikiBrowser();
  clearTimeout(S.wb.timer);
  if (src.startsWith("live:") && $("#wiki-dialog").open && S.run && S.run.status === "running") S.wb.timer = setTimeout(loadWikiSource, 1500);
}
function renderWikiBrowser() {
  const wb = S.wb, data = wb.data, nav = $("#wb-nav"), main = $("#wb-main");
  const insts = (data && data.instances) || [];
  const roles = [...new Set(insts.flatMap((i) => i.pages.flatMap((p) => p.revisions.map((r) => r.role))))];
  const rsel = $("#wb-role");
  rsel.replaceChildren(h("option", { value: "" }, "Edited by any rank"), ...roles.map((r) => h("option", { value: r }, `Edited by: ${titleCase(r)}`)));
  rsel.value = roles.includes(wb.role) ? wb.role : "";
  if (!data) { nav.replaceChildren(h("p", { class: "muted" }, "Nothing to show.")); main.replaceChildren(); return; }
  const navItems = [h("div", { class: "muted small", style: "margin-bottom:8px" }, `${data.org || ""} · ${data.task || ""}`)];
  for (const inst of insts) {
    const pages = inst.pages.filter((p) => !wb.role || p.revisions.some((r) => r.role === wb.role));
    if (!pages.length) continue;
    navItems.push(h("div", { class: "wb-inst" },
      h("div", { class: "head" }, inst.id), h("div", { class: "sub" }, `${inst.label} · ${inst.members.length} members`),
      pages.map((p) => {
        const cur = p.revisions[p.revisions.length - 1];
        const editors = [...new Set(p.revisions.map((r) => r.author))].length;
        return h("button", { class: "wb-page" + (inst.id === wb.inst && p.title === wb.page ? " sel" : ""),
          onclick: () => { wb.inst = inst.id; wb.page = p.title; wb.rev = null; renderWikiBrowser(); } },
          h("div", { class: "t" }, p.title),
          h("div", { class: "m" }, `v${cur.version} · ${cur.author} `, rankBadge(cur.role), ` · ${editors} editor${editors > 1 ? "s" : ""}`));
      })));
  }
  if (navItems.length === 1) navItems.push(h("p", { class: "muted" }, wb.role ? "No pages edited by that rank." : "No pages yet."));
  nav.replaceChildren(...navItems);

  const inst = insts.find((i) => i.id === wb.inst);
  const page = inst && inst.pages.find((p) => p.title === wb.page);
  if (!page) { main.replaceChildren(h("p", { class: "muted" }, "Select a page.")); return; }
  const revs = page.revisions, cur = revs[revs.length - 1];
  const shown = wb.rev ? revs.find((r) => r.version === wb.rev) || cur : cur;
  const people = {};
  revs.forEach((r) => { people[r.author] = people[r.author] || { r, n: 0 }; people[r.author].n++; });
  const out = [
    h("h2", { style: "margin:0" }, page.title),
    h("div", { class: "wb-meta" },
      h("div", {}, h("code", {}, inst.id), ` · ${inst.label} · ${revs.length} revision${revs.length > 1 ? "s" : ""}`),
      h("div", {}, `Last edited at ${cur.t.toFixed(1)}s by `, author(cur)),
      h("div", {}, "Contributors: ", Object.values(people).map(({ r, n }, i) => [i ? ", " : "", h("strong", {}, r.author), " ", rankBadge(r.role), h("span", { class: "muted small" }, ` ×${n}`)]))),
    h("div", { class: "wb-tabs" },
      h("button", { class: wb.view === "page" ? "on" : "", onclick: () => { wb.view = "page"; renderWikiBrowser(); } }, "Page"),
      h("button", { class: wb.view === "history" ? "on" : "", onclick: () => { wb.view = "history"; renderWikiBrowser(); } }, `History (${revs.length})`)),
  ];
  if (wb.view === "page") {
    if (shown !== cur) out.push(h("p", { class: "small" }, `Showing v${shown.version} by `, author(shown), " · ", h("span", { class: "linklike", onclick: () => { wb.rev = null; renderWikiBrowser(); } }, "back to latest")));
    out.push(md(shown.content));
  } else {
    const table = h("table", { class: "hist" }, h("tr", {}, ["v", "when", "who", "change", ""].map((c) => h("th", {}, c))));
    [...revs].reverse().forEach((r) => {
      const dim = wb.role && r.role !== wb.role;
      table.append(h("tr", { class: (shown.version === r.version && wb.rev ? "sel" : ""), style: dim ? "opacity:.45" : "",
        onclick: () => { wb.rev = r.version; renderWikiBrowser(); } },
        h("td", {}, `v${r.version}`), h("td", {}, `${r.t.toFixed(1)}s`), h("td", {}, author(r)),
        h("td", {}, `${r.mode} `, h("span", { class: r.chars_delta >= 0 ? "pill-ok" : "pill-bad" }, `${r.chars_delta >= 0 ? "+" : ""}${r.chars_delta}`), " chars"),
        h("td", {}, h("span", { class: "linklike", onclick: (e) => { e.stopPropagation(); wb.rev = r.version; wb.view = "page"; renderWikiBrowser(); } }, "view"))));
    });
    out.push(table);
    const sel = wb.rev ? revs.find((r) => r.version === wb.rev) : cur;
    const prev = revs.find((r) => r.version === sel.version - 1);
    out.push(h("h3", {}, prev ? `What v${sel.version} changed (vs v${prev.version})` : `v${sel.version} created the page`));
    out.push(h("p", { class: "small" }, "By ", author(sel), ` at ${sel.t.toFixed(1)}s`));
    const diff = h("div", { class: "diff" });
    const lines = diffLines(prev ? prev.content : "", sel.content);
    const line = ([k, l]) => h("div", { class: k }, (k === "add" ? "+ " : k === "del" ? "− " : "  ") + l);
    for (let i = 0; i < lines.length;) {  // collapse long unchanged stretches, keeping 2 lines of context
      if (lines[i][0] !== "ctx") { diff.append(line(lines[i++])); continue; }
      let j = i; while (j < lines.length && lines[j][0] === "ctx") j++;
      const run = lines.slice(i, j), headKeep = i === 0 ? 0 : 2, tailKeep = j === lines.length ? 0 : 2;
      if (run.length > headKeep + tailKeep + 2) {
        run.slice(0, headKeep).forEach((l) => diff.append(line(l)));
        diff.append(h("div", { class: "skip" }, `… ${run.length - headKeep - tailKeep} unchanged lines`));
        run.slice(run.length - tailKeep).forEach((l) => diff.append(line(l)));
      } else run.forEach((l) => diff.append(line(l)));
      i = j;
    }
    out.push(diff);
  }
  main.replaceChildren(...out);
}

/* ---------------- org files ---------------- */
async function refreshOrgs() {
  S.orgs = await api("/api/orgs");
  const sel = $("#org-select");
  sel.replaceChildren(h("option", { value: "" }, S.file ? "— open —" : "— unsaved —"), ...S.orgs.map((o) => h("option", { value: o.file }, `${o.file}${o.shape ? " · " + o.shape : ""}`)));
  sel.value = S.file || "";
}
async function openOrg(file) {
  try {
    const data = await api(`/api/orgs/${file}`);
    loadConfig(data.config, data.analysis);
    S.file = file; $("#org-select").value = file;
  } catch (e) { toast(e.message); }
}
function loadConfig(config, analysis) {
  S.config = config; assignIds(S.config.tree);
  S.undo = []; S.redo = []; S.selected = null; S.selectedLevel = null; S.selectedWiki = null; S.collapsed.clear(); S.run = null;
  if (analysis) setAnalysis(analysis);
  index();
  if (Object.keys(S.idx).length > 40) S.layers.forEach((layer, d) => { if (d >= 2) layer.forEach((n) => n.children && n.children.length && S.collapsed.add(n._id)); });
  renderAll(); requestAnimationFrame(() => fit(0.8));
  if (!analysis) scheduleAnalyze();
}
async function newOrg() {
  try {
    const res = await api("/api/generate", { name: "nouvelle-compagnie", workers: 3, depth: 1, ladder: [{ model: "sonnet", effort: "low" }], worker: { model: defaultWorker().model } });
    loadConfig(res.config, res.analysis); S.file = null; await refreshOrgs(); S.tab = "structure"; renderAll();
  } catch (e) { toast(e.message); }
}
async function save() {
  const suggestion = S.file || S.config.name.toLowerCase().replace(/[^a-z0-9_-]+/g, "-").replace(/^-+|-+$/g, "") || "my-org";
  const file = prompt("Save as configs/orgs/<name>.yaml", suggestion);
  if (!file) return;
  if (file !== S.file && S.orgs.some((o) => o.file === file) && !confirm(`${file}.yaml exists. Overwrite?`)) return;
  try {
    const res = await api(`/api/orgs/${file}`, { config: plain(S.config) });
    S.file = file; await refreshOrgs();
    toast(`Saved ${res.path} (${res.form} form)`);
  } catch (e) { toast(e.message); }
}

/* ---------------- canvas interaction ---------------- */
function bindCanvas() {
  const svg = $("#chart");
  let drag = null;
  svg.addEventListener("pointerdown", (e) => {
    if (e.target.closest(".node") || e.target.classList.contains("band-label")) return;
    drag = { x: e.clientX, y: e.clientY, vx: S.view.x, vy: S.view.y, moved: false };
    svg.setPointerCapture(e.pointerId); svg.classList.add("panning");
  });
  svg.addEventListener("pointermove", (e) => {
    if (!drag) return;
    const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
    if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
    S.view.x = drag.vx + dx; S.view.y = drag.vy + dy;
    const g = svg.firstChild; if (g) g.setAttribute("transform", `translate(${S.view.x},${S.view.y}) scale(${S.view.k})`);
  });
  svg.addEventListener("pointerup", () => {
    if (drag && !drag.moved && (S.selected || S.selectedLevel != null)) { S.selected = null; S.selectedLevel = null; renderAll(); }
    drag = null; svg.classList.remove("panning");
  });
  svg.addEventListener("wheel", (e) => {
    e.preventDefault();
    if (!e.ctrlKey && !e.metaKey) {  // plain scroll pans; Ctrl/Cmd+scroll (and pinch) zooms
      S.view.x -= e.shiftKey ? e.deltaY : e.deltaX; S.view.y -= e.shiftKey ? 0 : e.deltaY;
      const g = svg.firstChild; if (g) g.setAttribute("transform", `translate(${S.view.x},${S.view.y}) scale(${S.view.k})`);
      return;
    }
    const r = svg.getBoundingClientRect(), mx = e.clientX - r.left, my = e.clientY - r.top;
    const k2 = Math.max(0.15, Math.min(2.5, S.view.k * Math.exp(-e.deltaY * 0.0015)));
    S.view.x = mx - ((mx - S.view.x) * k2) / S.view.k; S.view.y = my - ((my - S.view.y) * k2) / S.view.k; S.view.k = k2;
    renderChart();
  }, { passive: false });
  const zoom = (f) => { const r = svg.getBoundingClientRect(), cx = r.width / 2, cy = r.height / 2, k2 = Math.max(0.15, Math.min(2.5, S.view.k * f));
    S.view.x = cx - ((cx - S.view.x) * k2) / S.view.k; S.view.y = cy - ((cy - S.view.y) * k2) / S.view.k; S.view.k = k2; renderChart(); };
  $("#zoom-in").onclick = () => zoom(1.2); $("#zoom-out").onclick = () => zoom(1 / 1.2); $("#zoom-fit").onclick = () => fit(0);
  $("#layout-mode").onchange = (e) => { S.layoutMode = e.target.value; fit(0.8); };
  let lastMode = null;
  window.addEventListener("resize", () => { renderChart(); if (S.mode !== lastMode) { lastMode = S.mode; fit(0.8); } });
}
function bindUI() {
  document.querySelectorAll(".tabs button").forEach((b) => (b.onclick = () => { S.tab = b.dataset.tab; renderAll(); }));
  $("#org-select").onchange = (e) => e.target.value && openOrg(e.target.value);
  $("#btn-new").onclick = newOrg;
  $("#btn-save").onclick = save;
  $("#btn-undo").onclick = undo; $("#btn-redo").onclick = redo;
  $("#btn-wikis").onclick = openWikiBrowser;
  $("#wb-close").onclick = () => { clearTimeout(S.wb && S.wb.timer); $("#wiki-dialog").close(); };
  $("#wb-refresh").onclick = loadWikiSource;
  $("#wb-source").onchange = (e) => { S.wb.source = e.target.value; S.wb.inst = null; loadWikiSource(); };
  $("#wb-role").onchange = (e) => { S.wb.role = e.target.value; renderWikiBrowser(); };
  $("#btn-yaml").onclick = () => { $("#yaml-text").value = S.analysis ? S.analysis.yaml : ""; $("#yaml-dialog").showModal(); };
  $("#yaml-copy").onclick = async () => { try { await navigator.clipboard.writeText($("#yaml-text").value); toast("Copied"); } catch (_) { $("#yaml-text").select(); } };
  $("#yaml-apply").onclick = async () => {
    try {
      const res = await api("/api/yaml", { yaml: $("#yaml-text").value });
      mutate(() => { const keep = S.config; Object.keys(keep).forEach((k) => delete keep[k]); Object.assign(keep, res.config); });
      $("#yaml-dialog").close(); setTimeout(() => fit(0.8), 50);
    } catch (e) { toast(e.message); }
  };
  document.addEventListener("keydown", (e) => {
    const typing = ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName);
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z") { if (typing) return; e.preventDefault(); e.shiftKey ? redo() : undo(); return; }
    if (typing) return;
    if (e.key === "Escape") { S.selected = null; S.selectedLevel = null; renderAll(); }
    if ((e.key === "Delete" || e.key === "Backspace") && S.selected) removeNode(S.selected);
    if (e.key === "f") fit(0);
  });
}

async function init() {
  try {
    S.catalog = await api("/api/catalog");
    await refreshOrgs();
    bindCanvas(); bindUI();
    const pick = S.orgs.find((o) => o.file === "grande-armee") || S.orgs.find((o) => !o.error);
    if (pick) await openOrg(pick.file); else await newOrg();
  } catch (e) { banner(e.message); }
}
init();
