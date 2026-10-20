// The library's first screen: the map of research areas, with what needs attention and what to continue.
import { api, type Doc, type Overview, NO_FILTERS } from "./api";
import { openClassifier } from "./actions";
import { addToggle, bar, chip, dot, link, paperList } from "./components";
import { h, toast } from "./dom";
import { navigate, type View } from "./main";

const REASON_TONE: Record<string, "bad" | "wait" | "progress"> = { failed: "bad", "no area yet": "wait", "low confidence": "progress" };

/** A block that is one link: a stat or a tile. Its content is plain text and boxes, so it can be a single target. */
function cardLink(href: string, cls: string, ...children: (Node | null)[]): HTMLElement {
  const a = link(href, "", cls);
  a.append(...children.filter((c): c is Node => c !== null));
  return a;
}

export function homeView(): View {
  let destroyed = false;
  const body = h("div", { class: "home-body" }, h("p", { class: "pane-message" }, "Loading…"));

  async function load(): Promise<void> {
    try {
      const [overview, listened] = await Promise.all([api.overview(), api.documents("", "listened", NO_FILTERS)]);
      if (!destroyed) body.replaceChildren(...render(overview, listened));
    } catch (e) {
      toast((e as Error).message, "error");
    }
  }

  function stat(label: string, value: string, href: string, tone = ""): HTMLElement {
    return cardLink(href, `stat ${tone}`.trim(), h("span", { class: "stat-value" }, value), h("span", { class: "stat-label" }, label));
  }

  function render(o: Overview, listened: Doc[]): HTMLElement[] {
    const input = h("input", { type: "search", placeholder: "Search titles, authors, topics, keywords", "aria-label": "Search the library" }) as HTMLInputElement;
    const search = h(
      "form",
      {
        class: "search",
        role: "search",
        onsubmit: (e: Event) => {
          e.preventDefault();
          const q = input.value.trim();
          navigate(q ? `/papers?q=${encodeURIComponent(q)}` : "/papers");
        },
      },
      input,
      h("button", { type: "submit", class: "btn" }, "Search"),
    );
    const header = h(
      "div",
      { class: "page-head" },
      h("h1", {}, "Library"),
      search,
      addToggle(() => load()),
    );

    const hours = (o.audio_s / 3600).toFixed(1);
    const strip = h(
      "div",
      { class: "strip", "aria-label": "Library at a glance" },
      stat("Papers", String(o.papers), "/papers"),
      stat("Unread", String(o.unread), "/papers?status=unread", o.unread ? "unread" : ""),
      stat("Failed", String(o.failed), "/papers?status=failed", o.failed ? "bad" : ""),
      stat("Hours of audio", hours, "/papers"),
    );

    const continuing = listened.filter((d) => d.playback && !d.listened).slice(0, 4);
    const continueSection = continuing.length
      ? h("section", { class: "block" }, h("h2", {}, "Continue listening"), paperList(continuing, () => load(), ""))
      : null;

    const map = h(
      "section",
      { class: "block" },
      h("div", { class: "block-head" }, h("h2", {}, "Research areas"), h("span", { class: "quiet" }, "Each paper has one main area and up to two others")),
      h(
        "div",
        { class: "tiles" },
        ...o.areas.map((a) =>
          cardLink(
            `/areas/${a.id}`,
            `tile${a.papers ? "" : " empty"}`,
            h("span", { class: "tile-head" }, h("strong", {}, a.name), a.unread ? chip(`${a.unread} unread`, "unread") : null),
            h("span", { class: "tile-count quiet" }, a.papers ? `${a.papers} ${a.papers === 1 ? "paper" : "papers"}` : "No papers yet"),
            a.progress !== null ? bar(a.progress, `${Math.round(a.progress * 100)}% listened`) : null,
            h("span", { class: "tile-topics" }, ...a.topics.map((t) => chip(t.name, "topic-mini"))),
          ),
        ),
      ),
    );

    const attention = [];
    if (o.unplaced > 0) {
      attention.push(
        h(
          "div",
          { class: "notice" },
          h("span", {}, `${o.unplaced} ${o.unplaced === 1 ? "paper has" : "papers have"} no area yet.`),
          h("span", { class: "hint" }, "An administrator places them."),
        ),
      );
    }
    if (o.attention.length) {
      attention.push(
        h(
          "ul",
          { class: "attention-list" },
          ...o.attention.map((item) =>
            h(
              "li",
              { class: "attention-row" },
              dot(REASON_TONE[item.reason], item.reason),
              h("span", { class: "attention-title" }, item.title),
              chip(item.reason, REASON_TONE[item.reason] ?? ""),
              item.reason === "failed"
                ? h("button", { type: "button", class: "btn small", onclick: async () => {
                    try {
                      await api.rerun(item.id, "start");
                      toast("Processing starts over.");
                      load();
                    } catch (e) {
                      toast((e as Error).message, "error");
                    }
                  } }, "Retry")
                : h("button", { type: "button", class: "btn small", onclick: () => openClassifier(item.id, () => load()) }, item.reason === "no area yet" ? "Set area" : "Review"),
            ),
          ),
        ),
      );
    }
    const needs = attention.length
      ? h("section", { class: "block" }, h("div", { class: "block-head" }, h("h2", {}, "Needs attention")), ...attention)
      : null;

    return [header, strip, continueSection, map, needs].filter((x): x is HTMLElement => x !== null);
  }

  void load();
  return {
    el: h("section", { class: "library-page home" }, body),
    title: "Library",
    destroy: () => {
      destroyed = true;
    },
  };
}
