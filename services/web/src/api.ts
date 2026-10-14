// Typed client for the listen-vault api. All requests go through the login proxy (Caddy in
// docker-compose.yml). When the login is missing or has expired, the proxy answers with a login
// prompt instead of JSON, so we reload the page and the browser asks again.

export interface Job {
  state: "queued" | "running" | "done" | "failed";
  stage: string | null;
  done: number;
  total: number;
  error: string | null;
  retry_at: string | null;
}

export interface Doc {
  id: string;
  title: string;
  authors: string[];
  published: string | null;
  source_type: "pdf" | "url";
  source_url: string | null;
  has_pdf: boolean;
  ocr_applied: boolean;
  status: "catalogued" | "queued" | "processing" | "ready" | "failed";
  from_zotero: boolean;
  error: string | null;
  duration_s: number | null;
  voice: string | null;
  listened: boolean;
  last_listened_at: string | null;
  created_at: string;
  job: Job | null;
  chapters_total: number;
  chapters_ready: number;
  playback: ListedPlayback | null;
  abstract: string | null;
  top_terms?: string[];
  /** Research areas: one primary area and up to two more (absent on the detail view of old responses). */
  areas?: DocAreas | null;
  topics?: { id: number; name: string }[];
}

export interface DocAreas {
  primary: string;
  also: string[];
  confidence: "high" | "medium" | "low";
  source: "llm" | "rules" | "manual";
  pinned: boolean;
}

/** Where a paper was left off, as the library list shows it. progress is the share of the whole paper. */
export interface ListedPlayback {
  chapter_idx: number;
  chapter_title: string | null;
  position_s: number;
  listened_s: number;
  progress: number | null;
}

/** Library filters. Terms must all match; the other filters narrow the same way. */
export interface Filters {
  term: string[];
  yearFrom: number | null;
  yearTo: number | null;
  source: "pdf" | "url" | "zotero" | null;
  notZotero: boolean;
  listened: boolean | null;
  status: Doc["status"] | null;
  area: string | null;
  topic: number | null;
}

export const NO_FILTERS: Filters = {
  term: [], yearFrom: null, yearTo: null, source: null, notZotero: false, listened: null, status: null,
  area: null, topic: null,
};

export interface Facets {
  total: number;
  audio_s: number;
  listened_audio_s: number;
  status: Record<string, number>;
  listened: Record<"true" | "false", number>;
  source: Record<"pdf" | "url" | "zotero", number>;
  years: { year: number; count: number }[];
  year_bounds: [number, number] | null; // the whole library, so the slider does not move with the filters
  terms: Record<KeywordKind, { term: string; count: number }[]>;
}

export interface GroupDoc {
  id: string;
  title: string;
  year: string | null;
  status: Doc["status"];
  listened: boolean;
  source: "pdf" | "url";
}

export interface GraphNode extends GroupDoc {
  group: number; // -1: no similar paper
}

export interface GraphLink {
  source: string;
  target: string;
  weight: number;
  terms: string[];
}

export interface Graph {
  nodes: GraphNode[];
  links: GraphLink[];
}

export interface Topic {
  id: number;
  name: string;
  terms: string[];
  area: string | null;
  documents: number;
}

/** One research area on the map. The top topics are the three largest of the area. */
export interface AreaTile {
  id: string;
  name: string;
  papers: number;
  unread: number;
  listened: number;
  progress: number | null;
  topics: { id: number; name: string; papers: number }[];
}

export interface Overview {
  papers: number;
  unread: number;
  not_processed: number;
  failed: number;
  audio_s: number;
  unplaced: number;
  areas: AreaTile[];
  attention: { id: string; title: string; status: Doc["status"]; reason: "failed" | "no area yet" | "low confidence" }[];
}

export interface AreaPage {
  id: string;
  name: string;
  papers: number;
  listened: number;
  unread: number;
  topics: { id: number; name: string; terms: string[]; papers: number; unread: number }[];
}

export interface TopicPage {
  id: number;
  name: string;
  terms: string[];
  area: string | null;
  area_name: string | null;
  papers: number;
  unread: number;
  related: { id: number; name: string; shared: number }[];
  prev: { id: number; name: string } | null;
  next: { id: number; name: string } | null;
}

export interface ClassificationIn {
  primary: string;
  also: string[];
  topics: Record<string, boolean>;
}

export interface TimelineItem {
  id: string;
  title: string;
  date: string | null; // YYYY-MM-DD, missing parts filled with 01
  precision: "day" | "month" | "year" | null;
  source: "pdf" | "url";
  status: Doc["status"];
  listened: boolean;
}

export interface Timeline {
  label: string;
  terms: string[];
  items: TimelineItem[];
}

export interface Chapter {
  idx: number;
  kind: "overview" | "chapter";
  title: string;
  intro: string | null;
  start_page: number | null;
  word_count: number;
  duration_s: number | null;
  ready: boolean;
}

export type KeywordKind = "task" | "method" | "dataset" | "topic" | "keyword";

export interface DocDetail extends Doc {
  chapters: Chapter[];
  keywords: Record<KeywordKind, string[]>;
}

export interface Sentence {
  i: number;
  text: string;
  page: number | null;
  start: number;
  end: number;
  /** [page, x, y, width, height] per line, PDF points from the top-left; empty for articles */
  rects: [number, number, number, number, number][];
}

export interface TextChapter {
  idx: number | null;
  title: string | null;
  intro: string | null;
  paragraphs: { text: string; kind: string }[];
}

export interface Citation {
  n: number;
  page: number | null;
  title: string;
  text: string;
}

export interface Question {
  id: number;
  question: string;
  answer: string | null;
  citations: Citation[];
  status: "queued" | "done" | "failed";
  error: string | null;
  created_at: string;
}

export interface Playback {
  chapter_idx: number;
  position_s: number;
  rate: number;
}

export interface Settings {
  values: {
    engine: "kokoro" | "piper";
    voice: string;
    speech_speed: number;
    citations: "remove" | "speak";
    reading_wpm: number;
  };
  voices: string[];
  engines: string[];
}

export interface ZoteroStatus {
  configured: boolean;
  last_synced_at: string | null;
  last_error: string | null;
  catalogued: number;
  syncing: boolean;
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

const WRITE_HEADERS = { "X-Requested-With": "listen-vault" };

/** Query string for the library filters (empty when nothing is set). */
export function filterQuery(f: Filters): string {
  const p = new URLSearchParams();
  for (const t of f.term) p.append("term", t);
  if (f.yearFrom !== null) p.set("year_from", String(f.yearFrom));
  if (f.yearTo !== null) p.set("year_to", String(f.yearTo));
  if (f.notZotero) p.set("not_zotero", "true");
  if (f.source) p.set("source", f.source);
  if (f.listened !== null) p.set("listened", String(f.listened));
  if (f.status) p.set("status", f.status);
  if (f.area) p.set("area", f.area);
  if (f.topic !== null) p.set("topic", String(f.topic));
  return p.toString();
}

export function toLogin(): never {
  window.location.reload();
  throw new ApiError(401, "Signing in…");
}

async function request<T>(method: string, path: string, body?: unknown, keepalive = false): Promise<T> {
  const init: RequestInit = { method, headers: {}, credentials: "same-origin", keepalive };
  if (method !== "GET") Object.assign(init.headers as Record<string, string>, WRITE_HEADERS);
  if (body !== undefined) {
    (init.headers as Record<string, string>)["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  const res = await fetch(path, init);
  const type = res.headers.get("content-type") ?? "";
  if (res.status === 401 || (res.ok && res.status !== 204 && !type.includes("json"))) toLogin();
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const data = await res.json();
      detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail);
    } catch {
      /* not json */
    }
    throw new ApiError(res.status, detail);
  }
  return res.status === 204 ? (undefined as T) : ((await res.json()) as T);
}

export const api = {
  documents: (q = "", sort = "added", f: Filters = NO_FILTERS) =>
    request<Doc[]>("GET", `/api/documents?q=${encodeURIComponent(q)}&sort=${sort}&${filterQuery(f)}`),
  facets: (f: Filters) => request<Facets>("GET", `/api/library/facets?${filterQuery(f)}`),
  overview: () => request<Overview>("GET", "/api/library/overview"),
  area: (id: string) => request<AreaPage>("GET", `/api/library/areas/${encodeURIComponent(id)}`),
  topic: (id: number) => request<TopicPage>("GET", `/api/library/topics/${id}`),
  classification: (id: string, body: ClassificationIn) =>
    request<Doc>("PATCH", `/api/documents/${id}/classification`, body),
  classify: (force: boolean) => request<{ queued: boolean }>("POST", `/api/library/classify?force=${force}`),
  graph: (f: Filters) => request<Graph>("GET", `/api/library/graph?${filterQuery(f)}`),
  topics: () => request<Topic[]>("GET", "/api/library/topics"),
  rebuildTopics: () => request<{ queued: boolean }>("POST", "/api/library/topics/rebuild"),
  timeline: (sel: { topic: number } | { term: string }) =>
    request<Timeline>("GET", `/api/library/timeline?${"topic" in sel ? `topic=${sel.topic}` : `term=${encodeURIComponent(sel.term)}`}`),
  document: (id: string) => request<DocDetail>("GET", `/api/documents/${id}`),
  addUrl: (url: string) => request<Doc>("POST", "/api/documents/url", { url }),
  patch: (id: string, changes: { title?: string; listened?: boolean }) =>
    request<Doc>("PATCH", `/api/documents/${id}`, changes),
  remove: (id: string) => request<void>("DELETE", `/api/documents/${id}`),
  rerun: (id: string, stage: "start" | "structure" | "chapters" | "synthesis") =>
    request<Doc>("POST", `/api/documents/${id}/rerun`, { stage }),
  sentences: (id: string, idx: number) => request<Sentence[]>("GET", `/api/documents/${id}/chapters/${idx}/sentences`),
  text: (id: string) => request<TextChapter[]>("GET", `/api/documents/${id}/text`),
  playback: (id: string) => request<Playback>("GET", `/api/documents/${id}/playback`),
  savePlayback: (id: string, p: Playback, keepalive = false) =>
    request<{ ok: boolean }>("PUT", `/api/documents/${id}/playback`, p, keepalive),
  backfillKeywords: () => request<{ queued: number }>("POST", "/api/library/keywords/backfill"),
  queue: (id: string) => request<Doc>("POST", `/api/documents/${id}/queue`),
  questions: (id: string) => request<Question[]>("GET", `/api/documents/${id}/questions`),
  ask: (id: string, question: string) => request<Question>("POST", `/api/documents/${id}/questions`, { question }),
  zotero: () => request<ZoteroStatus>("GET", "/api/zotero"),
  zoteroSync: () => request<{ queued: boolean }>("POST", "/api/zotero/sync"),
  zoteroQueueAll: () => request<{ queued: number }>("POST", "/api/zotero/queue-all"),
  settings: () => request<Settings>("GET", "/api/settings"),
  saveSettings: (changes: Partial<Settings["values"]>) => request<Settings>("PUT", "/api/settings", changes),
  me: () => request<{ user: string; logout_url: string }>("GET", "/api/me"),
  audioUrl: (id: string, idx: number) => `/api/documents/${id}/chapters/${idx}/audio`,
  pdfUrl: (id: string) => `/api/documents/${id}/pdf`,

  /** Upload a PDF with progress reporting (fetch has no upload progress). */
  upload(file: File, onProgress: (fraction: number) => void): Promise<Doc> {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/documents");
      xhr.setRequestHeader("X-Requested-With", "listen-vault");
      xhr.upload.onprogress = (e) => e.lengthComputable && onProgress(e.loaded / e.total);
      xhr.onload = () => {
        const type = xhr.getResponseHeader("content-type") ?? "";
        if (xhr.status === 401 || !type.includes("json")) toLogin();
        const data = JSON.parse(xhr.responseText || "{}");
        if (xhr.status >= 200 && xhr.status < 300) resolve(data as Doc);
        else reject(new ApiError(xhr.status, data.detail ?? xhr.statusText));
      };
      xhr.onerror = () => reject(new ApiError(0, "Upload failed — check your connection"));
      const form = new FormData();
      form.append("file", file);
      xhr.send(form);
    });
  },
};
