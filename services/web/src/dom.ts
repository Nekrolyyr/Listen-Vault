// Tiny DOM helpers and formatting.

type Child = Node | string | null | undefined | false;
type Attrs = Record<string, string | number | boolean | EventListener | null | undefined>;

export function h<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  attrs: Attrs = {},
  ...children: (Child | Child[])[]
): HTMLElementTagNameMap[K] {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key.startsWith("on") && typeof value === "function") {
      el.addEventListener(key.slice(2).toLowerCase(), value as EventListener);
    } else if (key === "class") {
      el.className = String(value);
    } else if (value === true) {
      el.setAttribute(key, "");
    } else {
      el.setAttribute(key, String(value));
    }
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(child));
  }
  return el;
}

/** Inline SVG icon from a small fixed set (stroke icons, currentColor). */
const ICONS: Record<string, string> = {
  play: '<path d="M8 5.5v13l11-6.5z" fill="currentColor" stroke="none"/>',
  pause: '<path d="M8 5h3v14H8zM13 5h3v14h-3z" fill="currentColor" stroke="none"/>',
  back15: '<path d="M4 12a8 8 0 1 0 2.3-5.6M4 4v4h4"/><text x="12" y="15.5" font-size="7" text-anchor="middle" fill="currentColor" stroke="none">15</text>',
  fwd15: '<path d="M20 12a8 8 0 1 1-2.3-5.6M20 4v4h-4"/><text x="12" y="15.5" font-size="7" text-anchor="middle" fill="currentColor" stroke="none">15</text>',
  prev: '<path d="M7 6v12M18 6l-8 6 8 6z"/>',
  next: '<path d="M17 6v12M6 6l8 6-8 6z"/>',
  upload: '<path d="M12 16V4M7 9l5-5 5 5M5 20h14"/>',
  link: '<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>',
  trash: '<path d="M5 7h14M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/>',
  refresh: '<path d="M20 11a8 8 0 0 0-14.3-4.5L4 8M4 4v4h4M4 13a8 8 0 0 0 14.3 4.5L20 16M20 20v-4h-4"/>',
  check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
  arrowLeft: '<path d="M19 12H5M11 6l-6 6 6 6"/>',
  doc: '<path d="M7 3h7l5 5v13H7zM14 3v5h5M10 13h6M10 17h6"/>',
  globe: '<path d="M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM3 12h18M12 3c2.5 2.7 3.6 5.7 3.6 9s-1.1 6.3-3.6 9c-2.5-2.7-3.6-5.7-3.6-9s1.1-6.3 3.6-9z"/>',
  more: '<circle cx="5" cy="12" r="1.6" fill="currentColor" stroke="none"/><circle cx="12" cy="12" r="1.6" fill="currentColor" stroke="none"/><circle cx="19" cy="12" r="1.6" fill="currentColor" stroke="none"/>',
  follow: '<path d="M12 5v10M7 10l5 5 5-5M5 20h14"/>',
  headphones: '<path d="M4 14v-2a8 8 0 0 1 16 0v2M4 14h3.5v5H4zM16.5 14H20v5h-3.5z"/>',
  book: '<path d="M12 6.5C9.5 4.8 6.5 4.4 3 5v13c3.5-.6 6.5-.2 9 1.5 2.5-1.7 5.5-2.1 9-1.5V5c-3.5-.6-6.5-.2-9 1.5zM12 6.5v13"/>',
  tag: '<path d="M3.5 12.3V4.5a1 1 0 0 1 1-1h7.8l8.2 8.2a1 1 0 0 1 0 1.4l-7.4 7.4a1 1 0 0 1-1.4 0z"/><circle cx="8" cy="8" r="1.4" fill="currentColor" stroke="none"/>',
  chevron: '<path d="M6 9.5l6 6 6-6"/>',
  x: '<path d="M6 6l12 12M18 6L6 18"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
};

export function icon(name: keyof typeof ICONS | string, label?: string): SVGSVGElement {
  const wrap = document.createElement("span");
  wrap.innerHTML =
    `<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.8" ` +
    `stroke-linecap="round" stroke-linejoin="round" ${label ? `role="img" aria-label="${label}"` : 'aria-hidden="true"'}>` +
    `${ICONS[name] ?? ""}</svg>`;
  return wrap.firstElementChild as SVGSVGElement;
}

export function duration(seconds: number | null | undefined): string {
  if (!seconds || seconds < 0) return "–";
  const total = Math.round(seconds);
  const hrs = Math.floor(total / 3600);
  const min = Math.floor((total % 3600) / 60);
  if (hrs) return `${hrs} h ${min} min`;
  return min ? `${min} min` : `${total} s`;
}

export function clock(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) seconds = 0;
  const s = Math.floor(seconds % 60).toString().padStart(2, "0");
  const m = Math.floor((seconds / 60) % 60);
  const hrs = Math.floor(seconds / 3600);
  return hrs ? `${hrs}:${m.toString().padStart(2, "0")}:${s}` : `${m}:${s}`;
}

export function date(iso: string | null): string {
  if (!iso) return "";
  return new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}

export function authors(list: string[]): string {
  if (list.length <= 2) return list.join(" and ");
  return `${list[0]}, ${list[1]} et al.`;
}

let toastTimer = 0;
export function toast(message: string, kind: "info" | "error" = "info"): void {
  let el = document.getElementById("toast");
  if (!el) {
    el = h("div", { id: "toast", role: "status", "aria-live": "polite" });
    document.body.append(el);
  }
  el.textContent = message;
  el.dataset.kind = kind;
  el.classList.add("show");
  window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => el!.classList.remove("show"), 4200);
}
