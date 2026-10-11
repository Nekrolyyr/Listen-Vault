import { api, type Doc } from "./api";
import { authors, date, duration, h, icon, toast } from "./dom";
import type { View } from "./main";

const STAGES: Record<string, string> = {
  fetch: "Fetching",
  ocr: "Running OCR",
  structure: "Reading the structure",
  enrich: "Finding keywords",
  chapters: "Naming chapters",
  synthesis: "Generating audio",
};

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
  let query = "";
  let sort = localStore("sort") ?? "added";
  let timer = 0;
  let destroyed = false;
  const uploads = new Map<string, number>(); // file name -> progress

  const list = h("ul", { class: "doc-list", "aria-live": "polite" });
  const uploadList = h("ul", { class: "upload-list" });
  const fileInput = h("input", { type: "file", accept: "application/pdf,.pdf", multiple: true, hidden: true }) as HTMLInputElement;
  const urlInput = h("input", {
    type: "url", name: "url", placeholder: "https://arxiv.org/abs/… or any article URL", required: true,
    "aria-label": "Article or paper URL", autocomplete: "off",
  }) as HTMLInputElement;

  async function load(): Promise<void> {
    try {
      docs = await api.documents(query, sort);
      renderList();
    } catch (e) {
      toast((e as Error).message, "error");
    }
    window.clearTimeout(timer);
    if (!destroyed) {
      const busy = docs.some((d) => d.status === "queued" || d.status === "processing");
      timer = window.setTimeout(load, busy ? 3000 : 20000);
    }
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
      } else if (action === "rerun-chapters") {
        await api.rerun(doc.id, "chapters");
        toast("Chapters and summaries will be rebuilt.");
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
        h("button", { type: "button", role: "menuitem", onclick: () => act(doc, "rerun-chapters"), disabled: notProcessed(doc) }, icon("refresh"), "Rebuild chapters"),
        h("button", { type: "button", role: "menuitem", onclick: () => act(doc, "rerun-start"), disabled: notProcessed(doc) }, icon("refresh"), "Process from scratch"),
        h("button", { type: "button", role: "menuitem", class: "danger", onclick: () => act(doc, "delete") }, icon("trash"), "Delete"),
      ),
    );
    const title = openable
      ? h("a", { href: `/doc/${doc.id}`, "data-link": true, class: "doc-title" }, doc.title)
      : h("span", { class: "doc-title" }, doc.title);
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
        s.progress !== null ? h("progress", { max: 1, value: s.progress, "aria-label": s.label }) : null,
        s.detail ? h("p", { class: `doc-detail ${s.tone}` }, s.detail) : null,
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
        menu,
      ),
    );
  }

  function renderList(): void {
    if (!docs.length) {
      list.replaceChildren(
        h("li", { class: "empty" }, query ? `Nothing matches “${query}”.` : "Your library is empty. Add a PDF or paste a link above to get started."),
      );
      return;
    }
    list.replaceChildren(...docs.map(card));
  }

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
    type: "search", placeholder: "Search title or author", "aria-label": "Search the library",
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

  const el = h(
    "section",
    { class: "library" },
    h("h1", { class: "visually-hidden" }, "Library"),
    h("div", { class: "add-panel" }, drop, urlForm, uploadList),
    h("div", { class: "toolbar" }, search, sortSelect),
    list,
  );
  load();
  return {
    el,
    title: "Library",
    destroy: () => {
      destroyed = true;
      window.clearTimeout(timer);
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
