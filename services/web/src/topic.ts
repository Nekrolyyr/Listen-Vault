// One topic: its papers (filterable), its timeline, the neighbouring topics in its area, and related topics.
import { api, type TopicPage } from "./api";
import { crumbs, link, openDoc } from "./components";
import { h, toast } from "./dom";
import { listing, type Listing } from "./listing";
import { readQuery, writeQuery } from "./query";
import { navigate, type View } from "./main";
import { timelinePanel } from "./timeline";

type Tab = "papers" | "timeline";

export function topicView(id: number): View {
  let destroyed = false;
  let tab: Tab = "papers";
  let info: TopicPage | null = null;
  let current: Listing | null = null;
  const body = h("div", { class: "topic-body" }, h("p", { class: "pane-message" }, "Loading…"));
  const panel = h("div", { class: "topic-panel" });

  /** Shows the papers or the timeline. The list is rebuilt, so it always starts from the address bar's filters. */
  function mountPanel(): void {
    current?.destroy();
    current = null;
    if (tab === "papers") {
      current = listing({ fixed: { topic: id }, hide: { area: true }, empty: "No papers on this topic match these filters." });
      panel.replaceChildren(current.el);
    } else {
      panel.replaceChildren(timelinePanel(openDoc, { topic: id }));
    }
  }

  function termRow(terms: string[]): HTMLElement {
    const selected = readQuery().terms;
    return h(
      "div",
      { class: "term-row", role: "group", "aria-label": "Keywords of this topic. Select to filter the papers." },
      ...terms.map((t) =>
        h(
          "button",
          {
            type: "button",
            class: "chip term",
            "aria-pressed": String(selected.includes(t)),
            onclick: () => {
              const now = readQuery().terms;
              writeQuery({ terms: now.includes(t) ? now.filter((x) => x !== t) : [...now, t] });
              mountPanel();
              body.querySelector(".term-row")?.replaceWith(termRow(terms));
            },
          },
          t,
        ),
      ),
    );
  }

  function neighbour(dir: "prev" | "next"): HTMLElement {
    const other = info?.[dir];
    const label = dir === "prev" ? "← " : "";
    const text = other ? `${label}${other.name}${dir === "next" ? " →" : ""}` : dir === "prev" ? "← First topic" : "Last topic →";
    if (!other) return h("span", { class: "btn small disabled", "aria-disabled": "true" }, text);
    return h("button", { type: "button", class: "btn small", title: dir === "prev" ? "Previous topic in this area" : "Next topic in this area", onclick: () => navigate(`/topics/${other.id}`) }, text);
  }

  function render(t: TopicPage): (HTMLElement | null)[] {
    info = t;
    const head = h(
      "div",
      { class: "page-head" },
      crumbs(["Library", "/"], ...(t.area && t.area_name ? [[t.area_name, `/areas/${t.area}`] as [string, string]] : []), [t.name]),
      h("h1", {}, t.name),
      h("p", { class: "quiet topic-meta" }, `${t.papers} ${t.papers === 1 ? "paper" : "papers"} · ${t.unread} unread`),
    );
    const nav = h("nav", { class: "topic-nav", "aria-label": "Topics in this area" }, neighbour("prev"), neighbour("next"));
    const tabs = h(
      "div",
      { class: "seg-group", role: "tablist", "aria-label": "Show" },
      ...(["papers", "timeline"] as Tab[]).map((k) =>
        h(
          "button",
          {
            type: "button",
            role: "tab",
            class: "seg",
            "aria-selected": String(tab === k),
            onclick: () => {
              tab = k;
              tabs.querySelectorAll("button").forEach((b, i) => b.setAttribute("aria-selected", String((i === 0) === (k === "papers"))));
              mountPanel();
            },
          },
          k === "papers" ? "Papers" : "Timeline",
        ),
      ),
    );
    const related = t.related.length
      ? h(
          "div",
          { class: "related" },
          h("span", { class: "quiet" }, "Related topics"),
          ...t.related.map((r) => link(`/topics/${r.id}`, `${r.name} · ${r.shared}`, "chip topic", `${r.shared} papers in common`)),
        )
      : null;
    return [
      head,
      nav,
      termRow(t.terms),
      related,
      h("div", { class: "topic-tools" }, tabs),
      panel,
    ];
  }

  async function load(): Promise<void> {
    try {
      const t = await api.topic(id);
      if (destroyed) return;
      body.replaceChildren(...render(t).filter((x): x is HTMLElement => x !== null));
      mountPanel();
    } catch (e) {
      if (!destroyed) body.replaceChildren(h("p", { class: "empty" }, (e as Error).message));
    }
  }

  /** The keys: [ and ] move to the previous and next topic of the area. */
  const onKey = (e: KeyboardEvent) => {
    const target = e.target as HTMLElement;
    if (target.closest("input, select, textarea, [contenteditable]") || e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === "[" && info?.prev) navigate(`/topics/${info.prev.id}`);
    else if (e.key === "]" && info?.next) navigate(`/topics/${info.next.id}`);
  };
  document.addEventListener("keydown", onKey);

  // swipe left and right on a phone: the next and previous topic
  let start: { x: number; y: number } | null = null;
  const onTouchStart = (e: TouchEvent) => {
    const t = e.changedTouches[0];
    start = { x: t.clientX, y: t.clientY };
  };
  const onTouchEnd = (e: TouchEvent) => {
    if (!start) return;
    const t = e.changedTouches[0];
    const dx = t.clientX - start.x;
    const dy = t.clientY - start.y;
    start = null;
    if (Math.abs(dx) < 90 || Math.abs(dy) > 50) return;
    if (dx < 0 && info?.next) navigate(`/topics/${info.next.id}`);
    else if (dx > 0 && info?.prev) navigate(`/topics/${info.prev.id}`);
  };

  void load().catch((e) => toast((e as Error).message, "error"));
  const el = h("section", { class: "library-page topic" }, body);
  el.addEventListener("touchstart", onTouchStart, { passive: true });
  el.addEventListener("touchend", onTouchEnd, { passive: true });
  return {
    el,
    title: "Topic",
    destroy: () => {
      destroyed = true;
      current?.destroy();
      document.removeEventListener("keydown", onKey);
    },
  };
}
