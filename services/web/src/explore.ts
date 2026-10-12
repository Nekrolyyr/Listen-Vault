// Explore: how the papers connect (the graph of shared keywords) and the timeline of a topic or keyword.
import { api, NO_FILTERS } from "./api";
import { crumbs, openDoc } from "./components";
import { h, toast } from "./dom";
import { graphView } from "./graph";
import type { View } from "./main";
import { timelinePanel } from "./timeline";

type Tab = "graph" | "timeline";

export function exploreView(): View {
  let tab: Tab = "graph";
  let destroyed = false;
  const panel = h("div", { class: "explore-panel" }, h("p", { class: "pane-message" }, "Loading…"));
  const buttons = (["graph", "timeline"] as Tab[]).map((k) =>
    h("button", { type: "button", role: "tab", class: "seg", "aria-selected": String(k === tab), onclick: () => show(k) }, k === "graph" ? "Graph" : "Timeline"),
  );

  async function show(next: Tab): Promise<void> {
    tab = next;
    buttons.forEach((b, i) => b.setAttribute("aria-selected", String((i === 0) === (next === "graph"))));
    if (next === "timeline") {
      panel.replaceChildren(timelinePanel(openDoc));
      return;
    }
    panel.replaceChildren(h("p", { class: "pane-message" }, "Drawing the graph…"));
    try {
      const graph = await api.graph(NO_FILTERS);
      if (destroyed || tab !== "graph") return;
      panel.replaceChildren(
        h("p", { class: "quiet" }, "Papers are linked when they share keywords. Only processed papers have keywords, so catalogued papers are not in the graph."),
        graphView(graph, openDoc),
      );
    } catch (e) {
      toast((e as Error).message, "error");
    }
  }

  void show("graph");
  return {
    el: h(
      "section",
      { class: "library-page explore" },
      h("div", { class: "page-head" }, crumbs(["Library", "/"], ["Explore"]), h("h1", {}, "Explore"), h("div", { class: "seg-group", role: "tablist", "aria-label": "Explore" }, ...buttons)),
      panel,
    ),
    title: "Explore",
    destroy: () => {
      destroyed = true;
    },
  };
}
