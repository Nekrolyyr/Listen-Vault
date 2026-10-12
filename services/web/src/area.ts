// One research area: its topics, and its papers grouped by topic.
import { api, type AreaPage, type Doc, NO_FILTERS } from "./api";
import { addToggle, bar, chip, crumbs, link, paperList } from "./components";
import { h } from "./dom";
import type { View } from "./main";

export function areaView(id: string): View {
  let destroyed = false;
  const body = h("div", { class: "area-body" }, h("p", { class: "pane-message" }, "Loading…"));

  async function load(): Promise<void> {
    try {
      const [area, docs] = await Promise.all([api.area(id), api.documents("", "added", { ...NO_FILTERS, area: id })]);
      if (!destroyed) body.replaceChildren(...render(area, docs));
    } catch (e) {
      if (!destroyed) body.replaceChildren(h("p", { class: "empty" }, (e as Error).message));
    }
  }

  function topicBlock(area: AreaPage): HTMLElement {
    if (!area.topics.length) {
      return h("p", { class: "quiet" }, "No topics in this area yet. Topics appear when papers share keywords or terms.");
    }
    return h(
      "ul",
      { class: "topic-rows" },
      ...area.topics.map((t) =>
        h(
          "li",
          { class: "topic-row" },
          link(`/topics/${t.id}`, t.name, "topic-name"),
          h("span", { class: "quiet topic-terms" }, t.terms.slice(0, 3).join(" · ")),
          h("span", { class: "topic-counts" }, chip(`${t.papers} ${t.papers === 1 ? "paper" : "papers"}`), t.unread ? chip(`${t.unread} unread`, "unread") : null),
        ),
      ),
    );
  }

  function render(area: AreaPage, docs: Doc[]): HTMLElement[] {
    const listened = area.papers ? area.listened / area.papers : null;
    const head = h(
      "div",
      { class: "page-head" },
      crumbs(["Library", "/"], [area.name]),
      h("h1", {}, area.name),
      addToggle(() => void load()),
    );
    const stats = h(
      "div",
      { class: "area-stats" },
      h("span", {}, `${area.papers} ${area.papers === 1 ? "paper" : "papers"}`),
      h("span", { class: "quiet" }, `${area.unread} unread · ${area.listened} listened`),
      listened !== null ? bar(listened, `${Math.round(listened * 100)}% listened`) : null,
      link(`/papers?area=${area.id}`, "Browse all papers in this area", "btn small"),
    );

    const topicIds = new Set(area.topics.map((t) => t.id));
    const groups = area.topics
      .map((t) => ({ topic: t, items: docs.filter((d) => (d.topics ?? []).some((x) => x.id === t.id)) }))
      .filter((g) => g.items.length);
    const loose = docs.filter((d) => !(d.topics ?? []).some((x) => topicIds.has(x.id)));
    const papers = docs.length
      ? [
          ...groups.map((g) =>
            h(
              "details",
              { class: "group", open: true },
              h("summary", {}, h("span", { class: "group-label" }, g.topic.name), chip(`${g.items.length}`)),
              paperList(g.items, () => void load(), ""),
            ),
          ),
          loose.length
            ? h(
                "details",
                { class: "group" },
                h("summary", {}, h("span", { class: "group-label" }, "Not on a topic yet"), chip(`${loose.length}`)),
                paperList(loose, () => void load(), ""),
              )
            : null,
        ]
      : [h("p", { class: "empty" }, "No papers in this area yet. Add one, or move a paper here from its ⋯ menu.")];

    return [
      head,
      stats,
      h("section", { class: "block" }, h("h2", {}, "Topics"), topicBlock(area)),
      h("section", { class: "block" }, h("h2", {}, "Papers"), ...papers),
    ];
  }

  void load();
  return {
    el: h("section", { class: "library-page area" }, body),
    title: "Area",
    destroy: () => {
      destroyed = true;
    },
  };
}
