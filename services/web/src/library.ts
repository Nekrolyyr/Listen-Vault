import { api, type Doc, type Facets, type Filters, type Graph, type GroupDoc, type Groups, NO_FILTERS, type TimelineItem } from "./api";
import { authors, date, duration, h, icon, toast } from "./dom";
import { graphView } from "./graph";
import { facetColumn } from "./facets";
import { timelinePanel } from "./timeline";
import { navigate, type View } from "./main";

const STAGES: Record<string, string> = {
  fetch: "Fetching",
  ocr: "Running OCR",
  structure: "Reading the structure",
  enrich: "Finding keywords",
  chapters: "Naming chapters",
  synthesis: "Generating audio",
};

type Mode = "list" | "groups" | "graph" | "timeline";
const MODES: [Mode, string][] = [
  ["list", "List"],
  ["groups", "Groups"],
  ["graph", "Graph"],
  ["timeline", "Timeline"],
];

/** Actions that re-run processing make no sense while it is running or before the item was queued. */
function notProcessed(doc: Doc): boolean {
  return doc.status === "queued" || doc.status === "processing" || doc.status === "catalogued";
}

function statusOf(doc: Doc): { label: string; tone: string; progress: number | null; detail?: string } {
  const job = doc.job;
  if (doc.status === "catalogued") return { label: "In Zotero · not queued", tone: "wait", progress: null };
  if (doc.status === "failed") return { label: "Failed", tone: "bad", progress: null, detail: doc.error ?? undefined };
  if (doc.status === "queued") {
    const retry = job?.error ? `Retrying soon: ${job.error}` : undefined;
    return { label: "Queued", tone: "wait", progress: null, detail: retry };
  }
  if (doc.status === "processing") {
    const stage = STAGES[job?.stage ?? ""] ?? "Starting";
    let progress: number | null = null;
    let label = stage;
    if (job && job.total > 0) {
      progress = job.done / job.total;
      label = job.stage === "synthesis" ? `${stage} · chapter ${Math.min(job.done + 1, job.total)} of ${job.total}` : stage;
    }
    return { label, tone: "busy", progress };
  }
  return { label: `Ready · ${duration(doc.duration_s)}`, tone: "ok", progress: null };
}

export function libraryView(): View {
  let docs: Doc[] = [];
  let facets: Facets | null = null;
  let groups: Groups | null = null;
  let graph: Graph | null = null;
  let filters: Filters = { ...NO_FILTERS };
  let query = "";
  let sort = localStore("sort") ?? "added";
  const saved = localStore("view");
  let mode: Mode = saved === "groups" || saved === "graph" || saved === "timeline" ? saved : "list";
  let timer = 0;
  let destroyed = false;
  let lastKey = "";
  const uploads = new Map<string, number>(); // file name -> progress

  // ---------------------------------------------------------------- abstract listening
  // The overview track is the abstract (see worker/pipeline.py), so it plays from the library.
  const audio = new Audio();
  const barTitle = h("span", { class: "bar-title" });
  const barPlay = h("button", { type: "button", class: "icon-btn", "aria-label": "Play", onclick: () => (audio.paused ? audio.play() : audio.pause()) }, icon("play"));
  const abstractBar = h(
    "div",
    { class: "abstract-bar", hidden: true, role: "region", "aria-label": "Abstract player" },
    barPlay,
    barTitle,
    h("button", { type: "button", class: "icon-btn", "aria-label": "Close", onclick: () => { audio.pause(); abstractBar.hidden = true; } }, "×"),
  );
  audio.addEventListener("play", () => barPlay.replaceChildren(icon("pause")));
  audio.addEventListener("pause", () => barPlay.replaceChildren(icon("play")));
  audio.addEventListener("ended", () => barPlay.replaceChildren(icon("play")));

  function listenAbstract(doc: Doc): void {
    audio.src = api.audioUrl(doc.id, 0);
    barTitle.textContent = `Abstract · ${doc.title}`;
    abstractBar.hidden = false;
    audio.play().catch(() => toast("Press play to listen.", "info"));
  }

  // ---------------------------------------------------------------- loading
  async function load(): Promise<void> {
    try {
      const [list, stats] = await Promise.all([api.documents(query, sort, filters), api.facets(filters)]);
      docs = list;
      facets = stats;
      if (mode === "groups") groups = await api.groups(filters);
      if (mode === "graph") graph = await api.graph(filters);
      const key = JSON.stringify([docs, facets, groups, graph, filters, mode]);
      if (key !== lastKey) {
        lastKey = key;
        render();
      }
    } catch (e) {
      toast((e as Error).message, "error");
    }
    window.clearTimeout(timer);
    if (!destroyed) {
      const busy = docs.some((d) => d.status === "queued" || d.status === "processing");
      timer = window.setTimeout(load, busy ? 3000 : 20000);
    }
  }

  function setFilters(next: Filters): void {
    filters = next;
    groups = null;
    graph = null;
    load();
  }

  async function addFiles(files: Iterable<File>): Promise<void> {
    for (const file of files) {
      if (!/\.pdf$/i.test(file.name) && file.type !== "application/pdf") {
        toast(`${file.name} is not a PDF`, "error");
        continue;
      }
      uploads.set(file.name, 0);
      renderUploads();
      try {
        await api.upload(file, (p) => {
          uploads.set(file.name, p);
          renderUploads();
        });
        toast(`Added ${file.name}. Processing has started.`);
      } catch (e) {
        toast(`${file.name}: ${(e as Error).message}`, "error");
      }
      uploads.delete(file.name);
      renderUploads();
      load();
    }
  }

  function renderUploads(): void {
    uploadList.replaceChildren(
      ...[...uploads].map(([name, p]) =>
        h("li", {}, h("span", {}, `Uploading ${name}`), h("progress", { max: 1, value: p, "aria-label": `Uploading ${name}` })),
      ),
    );
  }

  async function act(doc: Doc, action: string): Promise<void> {
    try {
      if (action === "delete") {
        if (!confirm(`Delete “${doc.title}” with its PDF and audio?`)) return;
        await api.remove(doc.id);
        toast("Deleted");
      } else if (action === "listened") {
        await api.patch(doc.id, { listened: !doc.listened });
      } else if (action === "rerun-audio") {
        await api.rerun(doc.id, "synthesis");
        toast("Audio will be generated again with the current voice settings.");
      } else if (action === "rerun-start") {
        await api.rerun(doc.id, "start");
        toast("Processing starts over.");
      } else if (action === "queue") {
        await api.queue(doc.id);
        toast("Queued for processing.");
      }
    } catch (e) {
      toast((e as Error).message, "error");
    }
    load();
  }

  function addTerm(term: string): void {
    if (!filters.term.includes(term)) setFilters({ ...filters, term: [...filters.term, term] });
  }

  // ---------------------------------------------------------------- cards
  function card(doc: Doc): HTMLElement {
    const s = statusOf(doc);
    const openable = doc.chapters_ready > 0 || doc.has_pdf || doc.status === "ready";
    const meta = [authors(doc.authors), doc.published?.slice(0, 4), `added ${date(doc.created_at)}`].filter(Boolean).join(" · ");
    const menu = h(
      "details",
      { class: "menu" },
      h("summary", { "aria-label": "More actions", title: "More actions" }, icon("more")),
      h(
        "div",
        { class: "menu-items", role: "menu" },
        h("button", { type: "button", role: "menuitem", onclick: () => act(doc, "listened") }, icon("check"), doc.listened ? "Mark as not listened" : "Mark as listened"),
        h("button", { type: "button", role: "menuitem", onclick: () => act(doc, "rerun-audio"), disabled: notProcessed(doc) }, icon("refresh"), "Regenerate audio"),
        h("button", { type: "button", role: "menuitem", onclick: () => act(doc, "rerun-start"), disabled: notProcessed(doc) }, icon("refresh"), "Process from scratch"),
        h("button", { type: "button", role: "menuitem", class: "danger", onclick: () => act(doc, "delete") }, icon("trash"), "Delete"),
      ),
    );
    const title = openable
      ? h("a", { href: `/doc/${doc.id}`, "data-link": true, class: "doc-title" }, doc.title)
      : h("span", { class: "doc-title" }, doc.title);
    const newTerms = (doc.top_terms ?? []).filter((t) => !filters.term.includes(t));
    return h(
      "li",
      { class: `doc-card${doc.listened ? " listened" : ""}` },
      h("div", { class: "doc-icon", "aria-hidden": "true" }, icon(doc.has_pdf ? "doc" : "globe")),
      h(
        "div",
        { class: "doc-main" },
        title,
        h("div", { class: "doc-meta" }, meta),
        h(
          "div",
          { class: "doc-status" },
          h("span", { class: `chip ${s.tone}` }, s.label),
          doc.ocr_applied ? h("span", { class: "chip" }, "OCR") : null,
          doc.listened ? h("span", { class: "chip" }, "Listened") : null,
          doc.from_zotero ? h("span", { class: "chip" }, "Zotero") : null,
          doc.status === "processing" && doc.chapters_ready > 0
            ? h("a", { href: `/doc/${doc.id}`, "data-link": true, class: "chip link" }, `${doc.chapters_ready} chapters playable`)
            : null,
        ),
        newTerms.length
          ? h(
              "div",
              { class: "doc-terms" },
              ...newTerms.map((t) => h("button", { type: "button", class: "chip term", title: "Filter by this term", onclick: () => addTerm(t) }, t)),
            )
          : null,
        s.progress !== null ? h("progress", { max: 1, value: s.progress, "aria-label": s.label }) : null,
        s.detail ? h("p", { class: `doc-detail ${s.tone}` }, s.detail) : null,
        doc.abstract ? h("details", { class: "abstract-toggle" }, h("summary", {}, "Abstract"), h("p", {}, doc.abstract)) : null,
      ),
      h(
        "div",
        { class: "doc-actions" },
        doc.status === "catalogued"
          ? h("button", { type: "button", class: "btn small primary", onclick: () => act(doc, "queue") }, "Queue")
          : doc.status === "failed"
            ? h("button", { type: "button", class: "btn small", onclick: () => act(doc, "rerun-start") }, "Retry")
            : openable
              ? h("a", { href: `/doc/${doc.id}`, "data-link": true, class: "btn small primary" }, doc.playback ? "Resume" : "Open")
              : null,
        doc.chapters_ready > 0
          ? h("button", { type: "button", class: "btn small", title: "Listen to the abstract", onclick: () => listenAbstract(doc) }, icon("play"), "Abstract")
          : null,
        menu,
      ),
    );
  }

  function renderList(): HTMLElement {
    if (!docs.length) {
      const hasFilter = query || filters.term.length || filters.year || filters.source || filters.listened !== null || filters.status;
      return h("ul", { class: "doc-list" }, h("li", { class: "empty" }, hasFilter ? "Nothing matches these filters." : "Your library is empty. Add a PDF or paste a link above to get started."));
    }
    return h("ul", { class: "doc-list", "aria-live": "polite" }, ...docs.map(card));
  }

  // ---------------------------------------------------------------- groups
  function groupItem(d: GroupDoc): HTMLElement {
    const meta = [d.year, d.listened ? "listened" : null].filter(Boolean).join(" · ");
    const link = d.status === "ready" || d.status === "processing"
      ? h("a", { href: `/doc/${d.id}`, "data-link": true }, d.title)
      : h("span", {}, d.title);
    return h("li", {}, link, meta ? h("span", { class: "quiet" }, ` · ${meta}`) : null);
  }

  function renderGroups(): HTMLElement {
    if (!groups) return h("p", { class: "pane-message" }, "Grouping papers…");
    if (!groups.groups.length && !groups.ungrouped.length) return h("p", { class: "empty" }, "No papers match these filters.");
    return h(
      "div",
      { class: "groups" },
      ...groups.groups.map((g) =>
        h(
          "details",
          { class: "group", open: true },
          h("summary", {}, h("span", { class: "group-label" }, g.label.join(" · ")), h("span", { class: "chip" }, `${g.documents.length} papers`)),
          h("ul", { class: "group-docs" }, ...g.documents.map(groupItem)),
        ),
      ),
      groups.ungrouped.length
        ? h(
            "details",
            { class: "group" },
            h("summary", {}, h("span", { class: "group-label" }, "Not similar to other papers"), h("span", { class: "chip" }, String(groups.ungrouped.length))),
            h("ul", { class: "group-docs" }, ...groups.ungrouped.map(groupItem)),
          )
        : null,
    );
  }

  // ---------------------------------------------------------------- graph
  function openDoc(d: { id: string; status: string }): void {
    if (d.status === "ready" || d.status === "processing") navigate(`/doc/${d.id}`);
    else toast("This paper is not processed yet, so there is nothing to open.", "info");
  }

  // ---------------------------------------------------------------- timeline
  // Kept across refreshes, so the chosen topic or keyword stays selected while the library updates.
  const timeline = timelinePanel((item: TimelineItem) => openDoc(item));

  function renderGraph(): HTMLElement {
    if (!graph) return h("p", { class: "pane-message" }, "Drawing the graph…");
    return graphView(graph, openDoc);
  }

  // ---------------------------------------------------------------- dashboard
  function dashboard(): HTMLElement {
    if (!facets) return h("div", { class: "dash" });
    const f = facets;
    const hours = (s: number) => `${(s / 3600).toFixed(1)} h`;
    const tile = (label: string, value: string, onclick?: () => void) =>
      h(
        onclick ? "button" : "div",
        { type: onclick ? "button" : undefined, class: "tile", onclick: onclick ?? null },
        h("span", { class: "tile-value" }, value),
        h("span", { class: "tile-label" }, label),
      );
    const maxYear = Math.max(1, ...f.years.map((y) => y.count));
    return h(
      "div",
      { class: "dash", "aria-label": "Library at a glance" },
      tile("Papers and articles", String(f.total), () => setFilters({ ...NO_FILTERS })),
      tile("Papers", String(f.source.pdf), () => setFilters({ ...NO_FILTERS, source: "pdf" })),
      tile("Articles", String(f.source.url), () => setFilters({ ...NO_FILTERS, source: "url" })),
      tile("Listened", String(f.listened.true ?? 0), () => setFilters({ ...NO_FILTERS, listened: true })),
      tile("Listened audio", hours(f.listened_audio_s)),
      tile("Audio in library", hours(f.audio_s)),
      f.source.zotero ? tile("From Zotero", String(f.source.zotero), () => setFilters({ ...NO_FILTERS, source: "zotero" })) : null,
      f.status.catalogued ? tile("Not queued", String(f.status.catalogued), () => setFilters({ ...NO_FILTERS, status: "catalogued" })) : null,
      f.years.length
        ? h(
            "div",
            { class: "years", "aria-label": "Papers per year" },
            ...f.years.map((y) =>
              h("button", {
                type: "button",
                class: `year-bar${filters.year === String(y.year) ? " on" : ""}`,
                title: `${y.year}: ${y.count}`,
                "aria-label": `${y.year}: ${y.count} papers`,
                style: `height:${Math.max(8, (y.count / maxYear) * 100)}%`,
                onclick: () => setFilters({ ...filters, year: filters.year === String(y.year) ? null : String(y.year) }),
              }),
            ),
            h("span", { class: "years-range" }, `${f.years[0].year}–${f.years[f.years.length - 1].year}`),
          )
        : null,
    );
  }

  // ---------------------------------------------------------------- layout
  function resultsHead(): HTMLElement {
    const tabs = h(
      "div",
      { class: "pane-tabs", role: "tablist" },
      ...MODES.map(([m, label]) =>
        h("button", {
          type: "button",
          role: "tab",
          "aria-selected": String(m === mode),
          onclick: () => {
            mode = m;
            localStore("view", m);
            groups = null;
            graph = null;
            load();
          },
        }, label),
      ),
    );
    const count = facets ? `${facets.total} ${facets.total === 1 ? "item" : "items"}` : "";
    return h("div", { class: "results-head" }, h("span", { class: "quiet" }, count), tabs);
  }

  function render(): void {
    dashSlot.replaceChildren(dashboard());
    facetSlot.replaceChildren(facetColumn(facets, filters, setFilters));
    headSlot.replaceChildren(resultsHead());
    const body = { groups: renderGroups, graph: renderGraph, timeline: () => timeline, list: renderList }[mode];
    bodySlot.replaceChildren(body());
  }

  // ---------------------------------------------------------------- inputs
  const uploadList = h("ul", { class: "upload-list" });
  const fileInput = h("input", { type: "file", accept: "application/pdf,.pdf", multiple: true, hidden: true }) as HTMLInputElement;
  const urlInput = h("input", {
    type: "url", name: "url", placeholder: "https://arxiv.org/abs/… or any article URL", required: true,
    "aria-label": "Article or paper URL", autocomplete: "off",
  }) as HTMLInputElement;

  const drop = h(
    "label",
    {
      class: "dropzone",
      ondragover: (e: Event) => {
        e.preventDefault();
        drop.classList.add("over");
      },
      ondragleave: () => drop.classList.remove("over"),
      ondrop: (e: Event) => {
        e.preventDefault();
        drop.classList.remove("over");
        const files = (e as DragEvent).dataTransfer?.files;
        if (files) addFiles(files);
      },
    },
    icon("upload"),
    h("span", {}, h("strong", {}, "Drop PDFs here"), " or ", h("u", {}, "choose files")),
    fileInput,
  );
  fileInput.addEventListener("change", () => {
    if (fileInput.files) addFiles([...fileInput.files]);
    fileInput.value = "";
  });

  const urlForm = h(
    "form",
    {
      class: "url-form",
      onsubmit: async (e: Event) => {
        e.preventDefault();
        const url = urlInput.value.trim();
        if (!url) return;
        try {
          await api.addUrl(url);
          urlInput.value = "";
          toast("Link added. Processing has started.");
          load();
        } catch (err) {
          toast((err as Error).message, "error");
        }
      },
    },
    icon("link"),
    urlInput,
    h("button", { type: "submit", class: "btn primary" }, "Add link"),
  );

  let searchTimer = 0;
  const search = h("input", {
    type: "search", placeholder: "Search title or author", "aria-label": "Search the library", value: "",
    oninput: (e: Event) => {
      query = (e.target as HTMLInputElement).value;
      window.clearTimeout(searchTimer);
      searchTimer = window.setTimeout(load, 250);
    },
  });
  const sortSelect = h(
    "select",
    {
      "aria-label": "Sort",
      onchange: (e: Event) => {
        sort = (e.target as HTMLSelectElement).value;
        localStore("sort", sort);
        load();
      },
    },
    ...[["added", "Recently added"], ["listened", "Recently listened"], ["title", "Title"]].map(([v, l]) =>
      h("option", { value: v, selected: v === sort }, l),
    ),
  );

  const dashSlot = h("div");
  const facetSlot = h("div", { class: "facet-slot" });
  const headSlot = h("div");
  const bodySlot = h("div", { class: "results-body" });
  const el = h(
    "section",
    { class: "library" },
    h("h1", { class: "visually-hidden" }, "Library"),
    h("div", { class: "add-panel" }, drop, urlForm, uploadList),
    dashSlot,
    h(
      "div",
      { class: "library-body" },
      facetSlot,
      h("div", { class: "results" }, h("div", { class: "toolbar" }, search, sortSelect), headSlot, bodySlot),
    ),
    abstractBar,
  );
  load();
  return {
    el,
    title: "Library",
    destroy: () => {
      destroyed = true;
      window.clearTimeout(timer);
      window.clearTimeout(searchTimer);
      audio.pause();
      audio.removeAttribute("src");
    },
  };
}

function localStore(key: string, value?: string): string | null {
  try {
    if (value !== undefined) localStorage.setItem(`lv.${key}`, value);
    return localStorage.getItem(`lv.${key}`);
  } catch {
    return null;
  }
}
