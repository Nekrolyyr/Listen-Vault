// Pieces every library page is built from, so that a paper, a status and an action look and behave the same
// everywhere: the status model and its colours, the paper row, chips, progress bars and breadcrumbs.
import type { Doc } from "./api";
import { authors, h, icon, toast } from "./dom";
import { areaName } from "./areas";
import { listenAbstract } from "./abstract";
import { act, addPanel, notProcessed, openClassifier } from "./actions";
import { navigate } from "./main";

/** The colour of a status: unread (accent), in progress (amber), listened (green), waiting, busy, failed, idle. */
export type Tone = "unread" | "progress" | "done" | "wait" | "busy" | "bad" | "idle";

export interface Status {
  label: string;
  tone: Tone;
  progress: number | null;
  detail?: string;
}

const STAGES: Record<string, string> = {
  fetch: "Fetching",
  ocr: "Running OCR",
  structure: "Reading the structure",
  enrich: "Finding keywords",
  chapters: "Naming chapters",
  synthesis: "Generating audio",
};

/** Papers that can be opened in the reader. */
export function openable(doc: Doc): boolean {
  return doc.chapters_ready > 0 || doc.has_pdf || doc.status === "ready";
}

export function statusOf(doc: Doc): Status {
  const job = doc.job;
  if (doc.status === "catalogued") return { label: "Not processed", tone: "idle", progress: null };
  if (doc.status === "failed") return { label: "Failed", tone: "bad", progress: null, detail: doc.error ?? undefined };
  if (doc.status === "queued") {
    return { label: "Queued", tone: "wait", progress: null, detail: job?.error ? `Retrying soon: ${job.error}` : undefined };
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
  if (doc.listened) return { label: "Listened", tone: "done", progress: null };
  const share = doc.playback?.progress ?? null;
  if (doc.playback && (doc.playback.position_s > 0 || (share ?? 0) > 0)) {
    const pct = share === null ? "" : ` · ${Math.round(share * 100)}%`;
    return { label: `In progress${pct}`, tone: "progress", progress: share };
  }
  return { label: "Unread", tone: "unread", progress: null };
}

export function dot(tone: Tone, label: string): HTMLElement {
  return h("span", { class: `dot tone-${tone}`, role: "img", "aria-label": label, title: label });
}

export function chip(text: string, cls = "", attrs: Record<string, string> = {}): HTMLElement {
  return h("span", { class: `chip ${cls}`.trim(), ...attrs }, text);
}

export function link(href: string, text: string, cls = "", title?: string): HTMLAnchorElement {
  return h("a", { href, "data-link": true, class: cls || null, title: title ?? null }, text) as HTMLAnchorElement;
}

/** A thin bar for a share between 0 and 1. */
export function bar(value: number, label: string): HTMLElement {
  return h("progress", { class: "bar", max: 1, value: Math.min(1, Math.max(0, value)), "aria-label": label });
}

/** Opens the reader, or says why not. */
export function openDoc(doc: { id: string; status: string; chapters_ready?: number; has_pdf?: boolean }): void {
  if (doc.status === "ready" || doc.status === "processing" || (doc.chapters_ready ?? 0) > 0 || doc.has_pdf) {
    navigate(`/doc/${doc.id}`);
  } else toast("This paper is not processed yet, so there is nothing to open.", "info");
}

/** The breadcrumb: Library › … The last item is the current page and is not a link. */
export function crumbs(...items: [label: string, href?: string][]): HTMLElement {
  return h(
    "nav",
    { class: "crumbs", "aria-label": "Breadcrumb" },
    ...items.flatMap(([label, href], i) => [
      i > 0 ? h("span", { class: "crumb-sep", "aria-hidden": "true" }, "›") : null,
      href ? link(href, label) : h("span", { "aria-current": "page" }, label),
    ]),
  );
}

/** One paper, in the same layout everywhere: status, title, meta, areas and topics, and its actions. */
export function paperRow(doc: Doc, reload: () => void): HTMLElement {
  const s = statusOf(doc);
  const canOpen = openable(doc);
  const title = canOpen
    ? link(`/doc/${doc.id}`, doc.title, "paper-title")
    : h("span", { class: "paper-title" }, doc.title);
  const meta = [authors(doc.authors), doc.published?.slice(0, 4), doc.has_pdf ? "PDF" : doc.source_type === "url" ? "Article" : null]
    .filter(Boolean)
    .join(" · ");
  const areaTag = doc.areas
    ? link(`/papers?area=${doc.areas.primary}`, areaName(doc.areas.primary), "chip area", "Papers in this area")
    : h("button", { type: "button", class: "chip area empty", onclick: () => openClassifier(doc.id, reload) }, "Set area");
  const topics = doc.topics ?? [];
  const topicTags = topics.slice(0, 2).map((t) => link(`/topics/${t.id}`, t.name, "chip topic", "Papers on this topic"));
  const more = topics.length > 2
    ? h("span", { class: "chip quiet", title: topics.slice(2).map((t) => t.name).join(", ") }, `+${topics.length - 2}`)
    : null;

  let primary: HTMLElement | null = null;
  if (s.tone === "idle" && doc.status === "catalogued") {
    primary = h("button", { type: "button", class: "btn small primary", onclick: () => act(doc, "queue", reload) }, "Queue");
  } else if (doc.status === "failed") {
    primary = h("button", { type: "button", class: "btn small", onclick: () => act(doc, "rerun-start", reload) }, "Retry");
  } else if (canOpen) {
    const resume = !!doc.playback && !doc.listened;
    primary = link(`/doc/${doc.id}`, resume ? "Resume" : "Open", "btn small primary");
  }

  const abstract = doc.chapters_ready > 0
    ? h("button", { type: "button", class: "btn small icon-only", title: "Listen to the abstract", "aria-label": "Listen to the abstract", onclick: () => listenAbstract(doc) }, icon("play"))
    : null;

  const menu = h(
    "details",
    { class: "menu" },
    h("summary", { "aria-label": "More actions", title: "More actions" }, icon("more")),
    h(
      "div",
      { class: "menu-items", role: "menu" },
      h("button", { type: "button", role: "menuitem", onclick: () => act(doc, "listened", reload) }, icon("check"), doc.listened ? "Mark as not listened" : "Mark as listened"),
      h("button", { type: "button", role: "menuitem", onclick: () => openClassifier(doc.id, reload) }, "Areas and topics…"),
      h("button", { type: "button", role: "menuitem", onclick: () => act(doc, "rerun-audio", reload), disabled: notProcessed(doc) }, icon("refresh"), "Regenerate audio"),
      h("button", { type: "button", role: "menuitem", onclick: () => act(doc, "rerun-start", reload), disabled: notProcessed(doc) }, icon("refresh"), "Process from scratch"),
      h("button", { type: "button", role: "menuitem", class: "danger", onclick: () => act(doc, "delete", reload) }, icon("trash"), "Delete"),
    ),
  );

  return h(
    "li",
    { class: `paper tone-${s.tone}${doc.listened ? " listened" : ""}` },
    h("div", { class: "paper-status" }, dot(s.tone, s.label)),
    h(
      "div",
      { class: "paper-main" },
      title,
      h("div", { class: "paper-meta" }, meta || null, meta ? h("span", { class: "sep" }, " · ") : null, h("span", { class: `status-label tone-${s.tone}` }, s.label)),
      h("div", { class: "paper-tags" }, areaTag, ...topicTags, more),
      s.progress !== null ? bar(s.progress, s.label) : null,
      s.detail ? h("p", { class: "paper-detail" }, s.detail) : null,
    ),
    h("div", { class: "paper-actions" }, primary, abstract, menu),
  );
}

/** Renders a page's list, or the message when nothing matches. */
export function paperList(docs: Doc[], reload: () => void, empty: string): HTMLElement {
  if (!docs.length) return h("ul", { class: "paper-list" }, h("li", { class: "empty" }, empty));
  return h("ul", { class: "paper-list", "aria-live": "polite" }, ...docs.map((d) => paperRow(d, reload)));
}

/** The "Add paper" control of every library page: the drop zone, the link form and the upload progress. */
export function addToggle(added: () => void): HTMLElement {
  return h("details", { class: "add-toggle" }, h("summary", { class: "btn primary" }, "Add paper"), addPanel(added));
}
