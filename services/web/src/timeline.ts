// Timeline of a topic (or one keyword): every paper and article that has it, placed by publication date.
// Papers and articles get their own lanes; papers without a date are listed at the end.
import { api, NO_FILTERS, type Timeline, type TimelineItem, type Topic } from "./api";
import { h, toast } from "./dom";

const SVG_NS = "http://www.w3.org/2000/svg";
const W = 960;
const H = 250;
const LEFT = 44;
const RIGHT = 20;
const LANES: Record<"pdf" | "url", number> = { pdf: 95, url: 175 };
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

export type Selection = { topic: number } | { term: string };

function svg<K extends keyof SVGElementTagNameMap>(tag: K, attrs: Record<string, string | number> = {}): SVGElementTagNameMap[K] {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, String(v));
  return el;
}

/** "2024-03-05" with its precision → "5 Mar 2024", "Mar 2024" or "2024". Built by hand so no time zone shifts it. */
export function formatDate(date: string, precision: TimelineItem["precision"]): string {
  const [y, m, d] = date.split("-").map(Number);
  if (precision === "year") return String(y);
  if (precision === "month") return `${MONTHS[m - 1]} ${y}`;
  return `${d} ${MONTHS[m - 1]} ${y}`;
}

function fraction(date: string): number {
  const [y, m, d] = date.split("-").map(Number);
  return y + (m - 1) / 12 + ((d || 1) - 1) / 366;
}

function chart(items: TimelineItem[], onOpen: (item: TimelineItem) => void): SVGSVGElement {
  const dated = items.filter((i): i is TimelineItem & { date: string } => i.date !== null);
  const years = dated.map((i) => fraction(i.date));
  const first = Math.floor(Math.min(...years));
  const last = Math.ceil(Math.max(...years)) + (Math.max(...years) - Math.min(...years) < 2 ? 1 : 0);
  const from = first - (first === last ? 1 : 0);
  const span = Math.max(1, last - from);
  const x = (year: number) => LEFT + ((year - from) / span) * (W - LEFT - RIGHT);
  const axisY = H - 30;

  const root = svg("svg", {
    viewBox: `0 0 ${W} ${H}`,
    class: "timeline-svg",
    role: "img",
    "aria-label": `${dated.length} papers and articles, ${first} to ${last}`,
  });

  // one bar per year: how many papers came out that year
  const perYear = new Map<number, number>();
  for (const i of dated) perYear.set(Math.floor(fraction(i.date)), (perYear.get(Math.floor(fraction(i.date))) ?? 0) + 1);
  const maxCount = Math.max(1, ...perYear.values());
  for (const [year, count] of perYear) {
    const barH = (count / maxCount) * 52;
    root.append(svg("rect", { x: x(year) + 1, y: axisY - barH, width: Math.max(4, (W - LEFT - RIGHT) / span - 2), height: barH, class: "tl-bar" }));
  }

  // year ticks
  const step = span > 16 ? 5 : span > 8 ? 2 : 1;
  for (let year = Math.ceil(from / step) * step; year <= from + span; year += step) {
    root.append(svg("line", { x1: x(year), y1: axisY, x2: x(year), y2: axisY + 5, class: "tl-tick" }));
    const label = svg("text", { x: x(year), y: axisY + 20, class: "tl-year", "text-anchor": "middle" });
    label.textContent = String(year);
    root.append(label);
  }
  root.append(svg("line", { x1: LEFT, y1: axisY, x2: W - RIGHT, y2: axisY, class: "tl-axis" }));

  // lane labels
  for (const [key, text] of [["pdf", "Papers"], ["url", "Articles"]] as const) {
    const label = svg("text", { x: 4, y: LANES[key] + 4, class: "tl-lane" });
    label.textContent = text;
    root.append(label);
  }

  // dots, spread a little within a lane and year so that neighbours stay visible
  const slot = new Map<string, number>();
  for (const item of dated) {
    const year = Math.floor(fraction(item.date));
    const key = `${item.source}-${year}`;
    const n = slot.get(key) ?? 0;
    slot.set(key, n + 1);
    const cx = x(fraction(item.date));
    const cy = LANES[item.source] + ((n % 4) - 1.5) * 12;
    const dot = svg("g", { class: `tl-dot${item.listened ? " listened" : ""}`, tabindex: 0, role: "button", "aria-label": item.title });
    dot.append(svg("circle", { cx, cy, r: 6 }));
    const title = svg("title");
    title.textContent = `${item.title} · ${formatDate(item.date, item.precision)}`;
    dot.append(title);
    dot.addEventListener("click", () => onOpen(item));
    dot.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        onOpen(item);
      }
    });
    root.append(dot);
  }
  return root;
}

function list(items: TimelineItem[], onOpen: (item: TimelineItem) => void): HTMLElement {
  const openable = (s: TimelineItem["status"]) => s === "ready" || s === "processing";
  const entry = (item: TimelineItem) =>
    h(
      "li",
      { class: item.listened ? "listened" : "" },
      h("span", { class: "tl-date" }, item.date ? formatDate(item.date, item.precision) : "Undated"),
      openable(item.status)
        ? h("button", { type: "button", class: "tl-title", onclick: () => onOpen(item) }, item.title)
        : h("span", { class: "tl-title" }, item.title),
      h("span", { class: "chip" }, item.source === "pdf" ? "Paper" : "Article"),
    );
  return h("ol", { class: "timeline-list" }, ...items.map(entry));
}

/** The papers and articles on a topic or a keyword over time. `initial` opens one of them straight away. */
export function timelinePanel(onOpen: (item: TimelineItem) => void, initial: Selection | null = null): HTMLElement {
  let selected: Selection | null = initial;
  let topics: Topic[] = [];
  let data: Timeline | null = null;
  let token = 0;

  const chips = h("div", { class: "topic-chips", role: "group", "aria-label": "Topics" });
  const suggestions = h("datalist", { id: "timeline-terms" });
  const termInput = h("input", {
    type: "search",
    list: "timeline-terms",
    placeholder: "A keyword, e.g. sleep",
    "aria-label": "Keyword",
    onchange: (e: Event) => {
      const term = (e.target as HTMLInputElement).value.trim();
      if (term) select({ term });
    },
  });
  const body = h("div", { class: "timeline-body", "aria-live": "polite" });

  function isSelected(s: Selection): boolean {
    if (!selected) return false;
    return "topic" in s && "topic" in selected ? s.topic === selected.topic : "term" in s && "term" in selected && s.term === selected.term;
  }

  function renderChips(): void {
    chips.replaceChildren(
      ...(topics.length
        ? topics.map((t) =>
            h("button", {
              type: "button",
              class: "chip topic",
              "aria-pressed": String(isSelected({ topic: t.id })),
              title: t.terms.join(", "),
              onclick: () => select({ topic: t.id }),
            }, `${t.name} · ${t.documents}`),
          )
        : [h("p", { class: "quiet" }, "No topics yet. They are built from the keywords of papers that share at least two. Extract keywords in Settings, then rebuild topics.")]),
    );
  }

  async function loadTopics(): Promise<void> {
    try {
      topics = await api.topics();
      const facets = await api.facets(NO_FILTERS);
      const terms = new Set(Object.values(facets.terms).flat().map((t) => t.term));
      suggestions.replaceChildren(...[...terms].sort().map((t) => h("option", { value: t })));
      renderChips();
    } catch (err) {
      toast((err as Error).message, "error");
    }
  }

  async function select(next: Selection): Promise<void> {
    selected = next;
    renderChips();
    const current = ++token;
    body.replaceChildren(h("p", { class: "pane-message" }, "Loading…"));
    try {
      const result = await api.timeline(next);
      if (current !== token) return;
      data = result;
      renderBody();
    } catch (err) {
      toast((err as Error).message, "error");
    }
  }

  function renderBody(): void {
    if (!data) {
      body.replaceChildren(h("p", { class: "empty" }, "Choose a topic, or type a keyword, to see its papers and articles over time."));
      return;
    }
    if (!data.items.length) {
      body.replaceChildren(h("p", { class: "empty" }, `No papers or articles mention “${data.label}” yet.`));
      return;
    }
    const dated = data.items.filter((i) => i.date !== null);
    const undated = data.items.filter((i) => i.date === null);
    body.replaceChildren(
      h("h2", { class: "timeline-title" }, data.label, h("span", { class: "quiet" }, ` · ${data.items.length} ${data.items.length === 1 ? "item" : "items"}`)),
      ...(dated.length ? [chart(dated, onOpen)] : []),
      list([...dated, ...undated], onOpen),
    );
  }

  const el = h(
    "section",
    { class: "timeline" },
    h("div", { class: "timeline-controls" }, termInput, suggestions),
    chips,
    body,
  );
  renderBody();
  loadTopics().then(() => {
    if (initial) void select(initial);
  });
  return el;
}
