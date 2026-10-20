// A list of papers with its filter bar. Used by the Papers page and by a topic page (which fixes the topic).
// The filters are read from and written to the address bar (see query.ts).
import { api, type Doc, type Filters } from "./api";
import { areaList } from "./areas";
import { paperList } from "./components";
import { h, toast } from "./dom";
import { SORTS, STATUSES, readQuery, toFilters, writeQuery, type ListQuery } from "./query";

export interface ListingOptions {
  /** Filters the page decides itself (a topic page fixes its topic). */
  fixed?: Partial<Filters>;
  /** Controls that the page does not need. */
  hide?: { area?: boolean; source?: boolean };
  /** Shown when nothing matches. */
  empty?: string;
}

export interface Listing {
  el: HTMLElement;
  destroy(): void;
}

const SOURCES: [string, string][] = [["", "All sources"], ["pdf", "PDF"], ["url", "Article or link"]];

export function listing(opts: ListingOptions = {}): Listing {
  let destroyed = false;
  let timer = 0;
  let searchTimer = 0;
  let docs: Doc[] = [];
  const areaNames = new Map<string, string>();
  const count = h("p", { class: "list-count quiet", "aria-live": "polite" });
  const activeRow = h("div", { class: "active-filters" });
  const results = h("div", { class: "results" }, h("p", { class: "pane-message" }, "Loading…"));

  const search = h("input", {
    type: "search",
    placeholder: "Search titles, authors, topics, keywords, abstracts",
    "aria-label": "Search papers",
    value: readQuery().q,
    oninput: (e: Event) => {
      const value = (e.target as HTMLInputElement).value;
      window.clearTimeout(searchTimer);
      searchTimer = window.setTimeout(() => change({ q: value }), 250);
    },
  }) as HTMLInputElement;

  // one segmented control for the status, the most used filter
  const statusButtons = STATUSES.map(([value, label]) =>
    h("button", { type: "button", class: "seg", "aria-pressed": "false", "data-value": value, onclick: () => change({ status: value }) }, label),
  );

  const source = h("select", { "aria-label": "Source", onchange: (e: Event) => change({ source: (e.target as HTMLSelectElement).value }) },
    ...SOURCES.map(([v, l]) => h("option", { value: v }, l)));
  const area = h("select", { "aria-label": "Area", onchange: (e: Event) => change({ area: (e.target as HTMLSelectElement).value }) });
  const sort = h("select", { "aria-label": "Sort", onchange: (e: Event) => change({ sort: (e.target as HTMLSelectElement).value }) },
    ...SORTS.map(([v, l]) => h("option", { value: v }, l)));
  const from = h("input", { type: "number", min: 1000, max: 2999, placeholder: "From", "aria-label": "Published from year", class: "year-input",
    onchange: (e: Event) => change({ from: (e.target as HTMLInputElement).value.trim() }) }) as HTMLInputElement;
  const to = h("input", { type: "number", min: 1000, max: 2999, placeholder: "To", "aria-label": "Published to year", class: "year-input",
    onchange: (e: Event) => change({ to: (e.target as HTMLInputElement).value.trim() }) }) as HTMLInputElement;
  const more = h(
    "details",
    { class: "filter-more" },
    h("summary", {}, "Filters"),
    h("div", { class: "filter-grid" },
      h("label", { class: "field-inline" }, h("span", {}, "Source"), source),
      opts.hide?.area ? null : h("label", { class: "field-inline" }, h("span", {}, "Area"), area),
      h("label", { class: "field-inline" }, h("span", {}, "Published"), from, h("span", { class: "quiet" }, "to"), to),
      h("label", { class: "field-inline" }, h("span", {}, "Sort"), sort),
    ),
  );
  if (opts.hide?.source) source.closest("label")?.remove();

  const el = h(
    "div",
    { class: "listing" },
    h("div", { class: "filter-bar" },
      h("div", { class: "filter-top" }, search, h("div", { class: "seg-group", role: "group", "aria-label": "Status" }, ...statusButtons)),
      more,
      activeRow,
    ),
    count,
    results,
  );

  /** Writes the new state to the address bar, and reloads. */
  function change(patch: Partial<ListQuery>): void {
    writeQuery(patch);
    void reload();
  }

  function sync(): ListQuery {
    const q = readQuery();
    search.value = q.q;
    statusButtons.forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.value === q.status)));
    source.value = q.source;
    sort.value = q.sort;
    from.value = q.from;
    to.value = q.to;
    if (!opts.hide?.area) area.value = q.area;
    activeRow.replaceChildren(...activeChips(q));
    return q;
  }

  function activeChips(q: ListQuery): HTMLElement[] {
    const chips: [string, () => void][] = [];
    const areaName = (id: string) => areaNames.get(id) ?? id;
    if (q.q.trim()) chips.push([`“${q.q.trim()}”`, () => change({ q: "" })]);
    if (q.status) chips.push([STATUSES.find(([v]) => v === q.status)?.[1] ?? q.status, () => change({ status: "" })]);
    if (q.source) chips.push([SOURCES.find(([v]) => v === q.source)?.[1] ?? q.source, () => change({ source: "" })]);
    if (q.area && !opts.hide?.area) chips.push([`Area: ${areaName(q.area)}`, () => change({ area: "" })]);
    for (const t of q.terms) chips.push([`Keyword: ${t}`, () => change({ terms: q.terms.filter((x) => x !== t) })]);
    if (q.from || q.to) chips.push([`Published ${q.from || "…"}–${q.to || "…"}`, () => change({ from: "", to: "" })]);
    if (!chips.length) return [];
    return [
      ...chips.map(([label, remove]) =>
        h("button", { type: "button", class: "chip removable", title: "Remove this filter", onclick: remove }, label, h("span", { "aria-hidden": "true" }, " ×"))),
      h("button", { type: "button", class: "btn small ghost", onclick: () => change({ q: "", status: "", source: "", area: "", terms: [], from: "", to: "" }) }, "Clear all"),
    ];
  }

  void areaList().then((list) => {
    for (const a of list) {
      areaNames.set(a.id, a.name);
      area.append(h("option", { value: a.id }, a.name));
    }
    sync();
  });

  async function reload(): Promise<void> {
    window.clearTimeout(timer);
    const q = sync();
    try {
      const filters = toFilters(q, opts.fixed);
      docs = await api.documents(q.q.trim(), q.sort, filters);
      count.textContent = `${docs.length} ${docs.length === 1 ? "paper" : "papers"}`;
      results.replaceChildren(paperList(docs, () => void reload(), opts.empty ?? (filtered(q) ? "Nothing matches these filters." : "No papers yet. Add a PDF or a link to start.")));
    } catch (e) {
      toast((e as Error).message, "error");
    }
    if (!destroyed) {
      const busy = docs.some((d) => d.status === "queued" || d.status === "processing");
      timer = window.setTimeout(() => void reload(), busy ? 6000 : 30000);
    }
  }

  function filtered(q: ListQuery): boolean {
    return !!(q.q.trim() || q.status || q.source || q.area || q.terms.length || q.from || q.to);
  }

  // a paper was added outside the list (the add panel): show it
  el.addEventListener("reload", () => void reload());
  sync();
  void reload();
  return {
    el,
    destroy: () => {
      destroyed = true;
      window.clearTimeout(timer);
      window.clearTimeout(searchTimer);
    },
  };
}
