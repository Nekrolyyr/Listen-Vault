// Filter column for the library, in the style of a model-hub search: counts next to each value, a search
// box per term list, and "clear" for each group. Terms combine with AND; each single-choice facet has one value.
import { type Facets, type Filters, type KeywordKind, NO_FILTERS } from "./api";
import { h } from "./dom";

const TERM_KINDS: [KeywordKind, string][] = [
  ["task", "Task"],
  ["method", "Methods"],
  ["dataset", "Datasets"],
  ["topic", "Topics"],
  ["keyword", "Keywords"],
];
const STATUS_LABELS: Record<string, string> = {
  catalogued: "In Zotero, not queued",
  queued: "Queued",
  processing: "Processing",
  ready: "Ready",
  failed: "Failed",
};
const TERMS_SHOWN = 8;

type Choice<T> = { value: T; label: string; count?: number };

function radioGroup<T extends string | boolean | null>(
  name: string,
  title: string,
  choices: Choice<T>[],
  selected: T,
  onPick: (value: T) => void,
): HTMLElement {
  return h(
    "fieldset",
    { class: "facet" },
    h("legend", {}, title),
    ...choices.map((c) =>
      h(
        "label",
        { class: "facet-row" },
        h("input", {
          type: "radio",
          name,
          checked: c.value === selected,
          onchange: () => onPick(c.value),
        }),
        h("span", { class: "facet-label" }, c.label),
        c.count !== undefined ? h("span", { class: "facet-count" }, String(c.count)) : null,
      ),
    ),
  );
}

function termFacet(title: string, values: { term: string; count: number }[], f: Filters,
                   onToggle: (term: string) => void): HTMLElement {
  let expanded = false;
  let query = "";
  const list = h("ul", { class: "facet-terms" });
  const search = h("input", {
    type: "search", class: "facet-search", placeholder: `Search ${title.toLowerCase()}`, "aria-label": `Search ${title.toLowerCase()}`,
    oninput: (e: Event) => {
      query = (e.target as HTMLInputElement).value.toLowerCase();
      render();
    },
  });
  const more = h("button", { type: "button", class: "facet-more", onclick: () => { expanded = !expanded; render(); } });

  function render(): void {
    // selected terms stay visible even when they fall outside the shown counts
    const selected = values.filter((v) => f.term.includes(v.term));
    const rest = values.filter((v) => !f.term.includes(v.term) && (!query || v.term.includes(query)));
    const shown = expanded || query ? rest : rest.slice(0, Math.max(0, TERMS_SHOWN - selected.length));
    list.replaceChildren(
      ...[...selected, ...shown].map((v) =>
        h(
          "li",
          {},
          h(
            "label",
            { class: "facet-row" },
            h("input", { type: "checkbox", checked: f.term.includes(v.term), onchange: () => onToggle(v.term) }),
            h("span", { class: "facet-label" }, v.term),
            h("span", { class: "facet-count" }, String(v.count)),
          ),
        ),
      ),
    );
    if (!query && rest.length > shown.length) {
      more.textContent = `Show ${rest.length - shown.length} more`;
      more.hidden = false;
    } else if (expanded && rest.length > TERMS_SHOWN) {
      more.textContent = "Show fewer";
      more.hidden = false;
    } else {
      more.hidden = true;
    }
    if (!values.length) list.replaceChildren(h("li", { class: "facet-empty" }, "None yet"));
  }
  render();
  return h("fieldset", { class: "facet" }, h("legend", {}, title), search, list, more);
}

/** The filter column. On phones it is a disclosure, closed unless `open`; on wide screens it is always open. */
/** Exclude papers imported from Zotero (independent of the Source choice above). */
function zoteroToggle(f: Filters, set: (patch: Partial<Filters>) => void): HTMLElement {
  return h(
    "fieldset",
    { class: "facet" },
    h("legend", {}, "Zotero"),
    h(
      "label",
      { class: "facet-row" },
      h("input", {
        type: "checkbox",
        checked: f.notZotero,
        onchange: (e: Event) => set({ notZotero: (e.target as HTMLInputElement).checked }),
      }),
      h("span", { class: "facet-label" }, "Exclude Zotero items"),
    ),
  );
}

/** Publication years as a range: two handles on one track. The filter changes when a handle is released; the
 *  whole library range means no filter. */
function yearSlider(
  bounds: [number, number] | null,
  f: Filters,
  onChange: (from: number | null, to: number | null) => void,
): HTMLElement | null {
  if (!bounds || bounds[0] === bounds[1]) return null;
  const [lo, hi] = bounds;
  const value = h("span", { class: "year-value", "aria-live": "polite" });
  const low = h("input", { type: "range", min: lo, max: hi, step: 1, value: f.yearFrom ?? lo, "aria-label": "First year" }) as HTMLInputElement;
  const high = h("input", { type: "range", min: lo, max: hi, step: 1, value: f.yearTo ?? hi, "aria-label": "Last year" }) as HTMLInputElement;
  const range = (): [number, number] => {
    const a = Number(low.value);
    const b = Number(high.value);
    return [Math.min(a, b), Math.max(a, b)];
  };
  const show = () => {
    const [a, b] = range();
    value.textContent = a === b ? String(a) : `${a}–${b}`;
  };
  const commit = () => {
    const [a, b] = range();
    const whole = a <= lo && b >= hi;
    onChange(whole ? null : a, whole ? null : b);
  };
  low.addEventListener("input", show);
  high.addEventListener("input", show);
  low.addEventListener("change", commit);
  high.addEventListener("change", commit);
  show();
  const yearActive = f.yearFrom !== null || f.yearTo !== null;
  return h(
    "fieldset",
    { class: "facet year-facet" },
    h("legend", {}, "Year"),
    h(
      "div",
      { class: "year-head" },
      value,
      yearActive ? h("button", { type: "button", class: "facet-clear", onclick: () => onChange(null, null) }, "Any year") : null,
    ),
    h("div", { class: "year-slider" }, low, high),
  );
}

export function facetColumn(
  facets: Facets | null,
  f: Filters,
  onChange: (next: Filters) => void,
  open: boolean,
  onToggle: (open: boolean) => void,
): HTMLElement {
  const set = (patch: Partial<Filters>) => onChange({ ...f, ...patch });
  const yearActive = f.yearFrom !== null || f.yearTo !== null;
  const active = f.term.length > 0 || yearActive || f.source !== null || f.notZotero || f.listened !== null || f.status !== null;
  const activeCount = f.term.length + [yearActive, f.source, f.notZotero, f.listened, f.status].filter(Boolean).length;
  const wide = window.matchMedia("(min-width: 861px)").matches;
  const children: (HTMLElement | null)[] = [
    h(
      "summary",
      {},
      "Filters",
      activeCount ? h("span", { class: "chip ok" }, `${activeCount} active`) : null,
    ),
    h(
      "div",
      { class: "facets-head" },
      h("strong", {}, "Filter"),
      active ? h("button", { type: "button", class: "facet-clear", onclick: () => onChange(NO_FILTERS) }, "Clear all") : null,
    ),
  ];
  const root = (...rest: (HTMLElement | null)[]) =>
    h(
      "details",
      { class: "facets", open: wide || open, "aria-label": "Filter the library", ontoggle: (e: Event) => onToggle((e.target as HTMLDetailsElement).open) },
      ...children,
      ...rest,
    );
  if (!facets) {
    return root(h("p", { class: "pane-message" }, "Loading…"));
  }
  const s = facets.source;
  const sourceGroup = radioGroup<Filters["source"]>("facet-source", "Source", [
      { value: null, label: "All", count: facets.total },
      { value: "pdf", label: "Papers", count: s.pdf },
      { value: "url", label: "Articles", count: s.url },
      { value: "zotero", label: "From Zotero", count: s.zotero },
    ], f.source, (v) => set({ source: v }));
  children.push(
    sourceGroup,
    zoteroToggle(f, set),
    radioGroup<Filters["listened"]>("facet-listened", "Listened", [
      { value: null, label: "All" },
      { value: true, label: "Listened", count: facets.listened.true ?? 0 },
      { value: false, label: "Not listened", count: facets.listened.false ?? 0 },
    ], f.listened, (v) => set({ listened: v })),
    radioGroup<Filters["status"]>(
      "facet-status",
      "Status",
      [{ value: null, label: "Any" }, ...Object.entries(facets.status).map(([k, n]) => ({
        value: k as Filters["status"], label: STATUS_LABELS[k] ?? k, count: n,
      }))],
      f.status,
      (v) => set({ status: v }),
    ),
  );
  const slider = yearSlider(facets.year_bounds, f, (from, to) => set({ yearFrom: from, yearTo: to }));
  if (slider) children.push(slider);
  for (const [kind, title] of TERM_KINDS) {
    const values = facets.terms[kind] ?? [];
    if (values.length) {
      children.push(termFacet(title, values, f, (term) =>
        set({ term: f.term.includes(term) ? f.term.filter((t) => t !== term) : [...f.term, term] })));
    }
  }
  return root();
}
