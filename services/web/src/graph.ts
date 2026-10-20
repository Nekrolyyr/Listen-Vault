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

  const nodeEls = nodes.map((n) => {
    const g = svg("g", { class: "graph-node", transform: `translate(${n.x ?? 0},${n.y ?? 0})`, tabindex: 0, role: "button", "aria-label": n.title });
    const dot = svg("circle", { r: 7, fill: colour(n.group) });
    const label = svg("text", { x: 10, y: 4, class: "graph-label" });
    label.textContent = n.title.length > LABEL_CHARS ? `${n.title.slice(0, LABEL_CHARS - 1)}…` : n.title;
    const title = svg("title");
    title.textContent = [n.title, n.year, n.listened ? "read" : null].filter(Boolean).join(" · ");
    g.append(dot, label, title);
    g.addEventListener("click", () => onOpen(n));
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

  const groups = new Set(nodes.map((n) => n.group).filter((g) => g >= 0)).size;
  return h(
    "div",
    { class: "graph" },
    root,
    h(
      "p",
      { class: "hint graph-hint" },
      `${nodes.length} papers in ${groups} ${groups === 1 ? "group" : "groups"}. Lines join papers with shared terms; `
        + "the thicker the line, the more they share. Hover to see a paper's neighbours, click to open it.",
    ),
  );
}
