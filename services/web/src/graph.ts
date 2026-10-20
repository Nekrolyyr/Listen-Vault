// Similarity graph of the library: a dot per paper, a line between papers that share search terms (the
// thicker the line, the more they share). The layout is computed once with d3-force, then drawn as SVG.
import {
  forceCenter,
  forceCollide,
  forceLink,
  forceManyBody,
  forceSimulation,
  type SimulationLinkDatum,
  type SimulationNodeDatum,
} from "d3-force";
import type { Graph, GraphNode } from "./api";
import { h } from "./dom";

const SVG_NS = "http://www.w3.org/2000/svg";
const LAYOUT_TICKS = 400;
const LABEL_CHARS = 34;
const HIT_RADIUS = 22; // screen pixels around a paper that open it, so a finger does not have to be exact
const DRAG_PX = 6; // movement beyond this is a pan or pinch, not a tap
const MIN_ZOOM = 0.5;
const MAX_ZOOM = 8;

interface SimNode extends SimulationNodeDatum, GraphNode {}
interface SimLink extends SimulationLinkDatum<SimNode> {
  weight: number;
  terms: string[];
}

function svg<K extends keyof SVGElementTagNameMap>(tag: K, attrs: Record<string, string | number> = {}): SVGElementTagNameMap[K] {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, String(v));
  return el;
}

/** One colour per group; papers without a similar partner are grey. */
function colour(group: number): string {
  return group < 0 ? "var(--muted)" : `hsl(${(group * 137.5) % 360} 58% 42%)`;
}

export function graphView(data: Graph, onOpen: (node: GraphNode) => void): HTMLElement {
  if (!data.nodes.length) return h("p", { class: "empty" }, "No papers match these filters.");
  const nodes: SimNode[] = data.nodes.map((n) => ({ ...n }));
  const links: SimLink[] = data.links.map((l) => ({ source: l.source, target: l.target, weight: l.weight, terms: l.terms }));

  const sim = forceSimulation<SimNode>(nodes)
    .force("link", forceLink<SimNode, SimLink>(links).id((d) => d.id).distance((d) => 40 + 160 * (1 - d.weight)).strength((d) => d.weight))
    .force("charge", forceManyBody<SimNode>().strength(-140))
    .force("collide", forceCollide<SimNode>(20))
    .force("center", forceCenter(0, 0))
    .stop();
  for (let i = 0; i < LAYOUT_TICKS; i++) sim.tick();

  // fit the drawing to its nodes
  const xs = nodes.map((n) => n.x ?? 0);
  const ys = nodes.map((n) => n.y ?? 0);
  const pad = 60;
  const minX = Math.min(...xs) - pad;
  const minY = Math.min(...ys) - pad;
  const width = Math.max(...xs) - minX + pad;
  const height = Math.max(...ys) - minY + pad;

  const root = svg("svg", {
    viewBox: `${minX} ${minY} ${width} ${height}`,
    class: "graph-svg",
    role: "img",
    "aria-label": `${nodes.length} papers, ${links.length} similarity links`,
  });
  const linkLayer = svg("g");
  const nodeLayer = svg("g");
  root.append(linkLayer, nodeLayer);

  const linkEls = links.map((l) => {
    const s = l.source as SimNode;
    const t = l.target as SimNode;
    const line = svg("line", {
      x1: s.x ?? 0, y1: s.y ?? 0, x2: t.x ?? 0, y2: t.y ?? 0,
      class: "graph-link", "stroke-width": (1 + 3 * l.weight).toFixed(2),
    });
    const title = svg("title");
    title.textContent = `Shared terms: ${l.terms.join(", ") || "none"}`;
    line.append(title);
    linkLayer.append(line);
    return { line, s, t };
  });

  const hits: SVGCircleElement[] = [];
  let dragged = false; // the last pointer movement was a pan or pinch, so the click that follows opens nothing
  const nodeEls = nodes.map((n) => {
    const g = svg("g", { class: "graph-node", transform: `translate(${n.x ?? 0},${n.y ?? 0})`, tabindex: 0, role: "button", "aria-label": n.title });
    const hit = svg("circle", { r: HIT_RADIUS, class: "graph-hit" });
    hits.push(hit);
    const dot = svg("circle", { r: 7, fill: colour(n.group), class: "graph-dot" });
    const label = svg("text", { x: 10, y: 4, class: "graph-label" });
    label.textContent = n.title.length > LABEL_CHARS ? `${n.title.slice(0, LABEL_CHARS - 1)}…` : n.title;
    const title = svg("title");
    title.textContent = [n.title, n.year, n.listened ? "read" : null].filter(Boolean).join(" · ");
    g.append(hit, dot, label, title);
    g.addEventListener("click", () => {
      if (!dragged) onOpen(n);
    });
    g.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        onOpen(n);
      }
    });
    nodeLayer.append(g);
    return { g, n };
  });

  // hovering a paper highlights its links and neighbours
  const neighbours = new Map<string, Set<string>>();
  for (const { s, t } of linkEls) {
    if (!neighbours.has(s.id)) neighbours.set(s.id, new Set());
    if (!neighbours.has(t.id)) neighbours.set(t.id, new Set());
    neighbours.get(s.id)!.add(t.id);
    neighbours.get(t.id)!.add(s.id);
  }
  const focus = (id: string | null) => {
    const near = id ? neighbours.get(id) ?? new Set<string>() : null;
    for (const { line, s, t } of linkEls) {
      const on = id !== null && (s.id === id || t.id === id);
      line.classList.toggle("on", on);
      line.classList.toggle("dim", id !== null && !on);
    }
    for (const { g, n } of nodeEls) {
      g.classList.toggle("dim", id !== null && n.id !== id && !near?.has(n.id));
    }
  };
  for (const { g, n } of nodeEls) {
    g.addEventListener("mouseenter", () => focus(n.id));
    g.addEventListener("focus", () => focus(n.id));
    g.addEventListener("mouseleave", () => focus(null));
    g.addEventListener("blur", () => focus(null));
  }

  // zoom and pan: the view is the viewBox. Drag to pan, pinch or scroll to zoom, and the buttons for the same.
  const fit = { x: minX, y: minY, w: width, h: height };
  const view = { ...fit };
  const apply = () => {
    const scale = fit.w / view.w;
    root.setAttribute("viewBox", `${view.x} ${view.y} ${view.w} ${view.h}`);
    for (const hit of hits) hit.setAttribute("r", (HIT_RADIUS / scale).toFixed(2));
  };
  const toSvg = (clientX: number, clientY: number): DOMPoint => {
    const m = root.getScreenCTM();
    return m ? new DOMPoint(clientX, clientY).matrixTransform(m.inverse()) : new DOMPoint(view.x + view.w / 2, view.y + view.h / 2);
  };
  const zoomAt = (clientX: number, clientY: number, factor: number) => {
    const w = Math.min(Math.max(view.w / factor, fit.w / MAX_ZOOM), fit.w / MIN_ZOOM);
    const ratio = w / view.w;
    const p = toSvg(clientX, clientY);
    view.x = p.x - (p.x - view.x) * ratio;
    view.y = p.y - (p.y - view.y) * ratio;
    view.w = w;
    view.h *= ratio;
    apply();
  };
  const pan = (fromX: number, fromY: number, toX: number, toY: number) => {
    const a = toSvg(fromX, fromY);
    const b = toSvg(toX, toY);
    view.x -= b.x - a.x;
    view.y -= b.y - a.y;
    apply();
  };

  const pointers = new Map<number, { x: number; y: number }>();
  let travel = 0;
  let pinch: { x: number; y: number; dist: number } | null = null;
  const onDown = (e: PointerEvent) => {
    if (pointers.size === 0) {
      dragged = false;
      travel = 0;
      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", onUp);
      window.addEventListener("pointercancel", onUp);
    }
    pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    pinch = null;
  };
  const onMove = (e: PointerEvent) => {
    const prev = pointers.get(e.pointerId);
    if (!prev) return;
    pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
    travel += Math.hypot(e.clientX - prev.x, e.clientY - prev.y);
    if (travel > DRAG_PX) dragged = true;
    if (pointers.size === 1) {
      pan(prev.x, prev.y, e.clientX, e.clientY);
      return;
    }
    const [a, b] = [...pointers.values()];
    const mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
    const dist = Math.hypot(a.x - b.x, a.y - b.y);
    if (pinch) {
      pan(pinch.x, pinch.y, mid.x, mid.y);
      if (pinch.dist > 0) zoomAt(mid.x, mid.y, dist / pinch.dist);
    }
    pinch = { x: mid.x, y: mid.y, dist };
  };
  const onUp = (e: PointerEvent) => {
    pointers.delete(e.pointerId);
    pinch = null;
    if (pointers.size === 0) {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);
    }
  };
  root.addEventListener("pointerdown", onDown);
  root.addEventListener("wheel", (e) => {
    e.preventDefault();
    zoomAt(e.clientX, e.clientY, Math.exp(-e.deltaY * 0.002));
  }, { passive: false });

  const zoomCentre = (factor: number) => {
    const r = root.getBoundingClientRect();
    zoomAt(r.left + r.width / 2, r.top + r.height / 2, factor);
  };
  const control = (label: string, title: string, onclick: () => void) =>
    h("button", { type: "button", class: "btn graph-zoom", title, "aria-label": title, onclick }, label);
  const tools = h(
    "div",
    { class: "graph-tools" },
    control("+", "Zoom in", () => zoomCentre(1.25)),
    control("−", "Zoom out", () => zoomCentre(1 / 1.25)),
    control("Fit", "Show all papers", () => {
      Object.assign(view, fit);
      apply();
    }),
  );
  apply();

  const groups = new Set(nodes.map((n) => n.group).filter((g) => g >= 0)).size;
  return h(
    "div",
    { class: "graph" },
    tools,
    root,
    h(
      "p",
      { class: "hint graph-hint" },
      `${nodes.length} papers in ${groups} ${groups === 1 ? "group" : "groups"}. Lines join papers with shared terms; `
        + "the thicker the line, the more they share. Drag or pinch to move and zoom. Hover to see a paper's neighbours, click to open it.",
    ),
  );
}
