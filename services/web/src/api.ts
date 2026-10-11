// Typed client for the listen-vault api. All requests go through Traefik + Authelia; when the
// session has expired, Authelia answers with its login page instead of JSON, so we send the
// browser to the portal and come back afterwards.

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
  status: "queued" | "processing" | "ready" | "failed";
  error: string | null;
  duration_s: number | null;
  voice: string | null;
  listened: boolean;
  last_listened_at: string | null;
  created_at: string;
  job: Job | null;
  chapters_total: number;
  chapters_ready: number;
  playback: { chapter_idx: number; position_s: number } | null;
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

export interface DocDetail extends Doc {
  chapters: Chapter[];
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
  };
  voices: string[];
  engines: string[];
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

const WRITE_HEADERS = { "X-Requested-With": "listen-vault" };

export function toLogin(): never {
  window.location.href = `/auth/?rd=${encodeURIComponent(window.location.href)}`;
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
  documents: (q = "", sort = "added") =>
    request<Doc[]>("GET", `/api/documents?q=${encodeURIComponent(q)}&sort=${sort}`),
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
