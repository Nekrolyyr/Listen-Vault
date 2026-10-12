// The state of a paper list lives in the address bar, so a filtered view can be bookmarked, shared and restored
// with the back button. Every list page reads and writes the same parameter names.
import { type Filters, NO_FILTERS } from "./api";

export const STATUSES = [
  ["", "All"],
  ["unread", "Unread"],
  ["listened", "Listened"],
  ["not_processed", "Not processed"],
  ["failed", "Failed"],
] as const;

export const SORTS = [
  ["added", "Recently added"],
  ["listened", "Recently listened"],
  ["title", "Title"],
] as const;

export interface ListQuery {
  q: string;
  status: string;
  source: string;
  area: string;
  topic: string;
  terms: string[];
  from: string;
  to: string;
  sort: string;
}

export function readQuery(): ListQuery {
  const p = new URLSearchParams(location.search);
  return {
    q: p.get("q") ?? "",
    status: p.get("status") ?? "",
    source: p.get("source") ?? "",
    area: p.get("area") ?? "",
    topic: p.get("topic") ?? "",
    terms: p.getAll("term"),
    from: p.get("from") ?? "",
    to: p.get("to") ?? "",
    sort: p.get("sort") ?? "added",
  };
}

/** Changes some parameters and keeps the rest. Replaces the history entry, so the back button is not flooded. */
export function writeQuery(patch: Partial<ListQuery>): void {
  const next = { ...readQuery(), ...patch };
  const p = new URLSearchParams();
  if (next.q.trim()) p.set("q", next.q.trim());
  if (next.status) p.set("status", next.status);
  if (next.source) p.set("source", next.source);
  if (next.area) p.set("area", next.area);
  if (next.topic) p.set("topic", next.topic);
  for (const t of next.terms) p.append("term", t);
  if (next.from) p.set("from", next.from);
  if (next.to) p.set("to", next.to);
  if (next.sort && next.sort !== "added") p.set("sort", next.sort);
  const qs = p.toString();
  history.replaceState(null, "", `${location.pathname}${qs ? `?${qs}` : ""}`);
}

/** The api filters for a list query. `fixed` holds what the page itself decides (for example its topic). */
export function toFilters(q: ListQuery, fixed: Partial<Filters> = {}): Filters {
  const f: Filters = { ...NO_FILTERS, term: [...q.terms] };
  if (q.status === "unread") f.listened = false;
  else if (q.status === "listened") f.listened = true;
  else if (q.status === "not_processed") f.status = "catalogued";
  else if (q.status === "failed") f.status = "failed";
  if (q.source === "pdf" || q.source === "url" || q.source === "zotero") f.source = q.source;
  if (q.area) f.area = q.area;
  if (q.topic) f.topic = Number(q.topic);
  if (q.from) f.yearFrom = Number(q.from);
  if (q.to) f.yearTo = Number(q.to);
  return { ...f, ...fixed };
}
