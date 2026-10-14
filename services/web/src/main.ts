import "./style.css";
import { abstractBar } from "./abstract";
import { h } from "./dom";
import { areaView } from "./area";
import { exploreView } from "./explore";
import { homeView } from "./home";
import { papersView } from "./papers";
import { readerView } from "./reader";
import { settingsView } from "./settings";
import { topicView } from "./topic";

export interface View {
  el: HTMLElement;
  destroy?: () => void;
  title?: string;
}

const app = document.getElementById("app")!;
let current: View | null = null;

/** Which top-level section a page belongs to, so its navigation item stays marked. */
type Section = "library" | "papers" | "explore" | "settings" | "reader";

const ROUTES: [RegExp, (m: RegExpMatchArray) => View, Section][] = [
  [/^\/$/, () => homeView(), "library"],
  [/^\/areas\/([a-z_]{2,20})$/, (m) => areaView(m[1]), "library"],
  [/^\/topics\/(\d+)$/, (m) => topicView(Number(m[1])), "library"],
  [/^\/papers$/, () => papersView(), "papers"],
  [/^\/explore$/, () => exploreView(), "explore"],
  [/^\/settings$/, () => settingsView(), "settings"],
  [/^\/doc\/([0-9a-f-]{36})$/, (m) => readerView(m[1]), "reader"],
];

function shell(view: View, active: Section): HTMLElement {
  const link = (href: string, label: string, section: Section) => {
    const on = active === section;
    return h("a", { href, "data-link": true, class: on ? "active" : "", "aria-current": on ? "page" : null }, label);
  };
  return h(
    "div",
    { class: "shell" },
    h(
      "header",
      { class: "topbar" },
      h("a", { href: "/", "data-link": true, class: "brand" }, h("img", { src: "/favicon.svg", alt: "", width: 24, height: 24 }), h("span", { class: "brand-name" }, "Listen Vault")),
      h("nav", { "aria-label": "Main" }, link("/", "Library", "library"), link("/papers", "Papers", "papers"), link("/explore", "Explore", "explore"), link("/settings", "Settings", "settings")),
    ),
    h("main", { class: "content" }, view.el),
    abstractBar,
  );
}

export function navigate(path: string, replace = false): void {
  if (replace) history.replaceState(null, "", path);
  else history.pushState(null, "", path);
  render();
}

function render(): void {
  current?.destroy?.();
  const path = location.pathname;
  let view: View | null = null;
  let section: Section = "library";
  for (const [pattern, make, sec] of ROUTES) {
    const m = path.match(pattern);
    if (m) {
      view = make(m);
      section = sec;
      break;
    }
  }
  if (!view) {
    view = homeView();
    section = "library";
  }
  current = view;
  app.replaceChildren(shell(view, section));
  document.title = view.title ? `${view.title} · Listen Vault` : "Listen Vault";
  window.scrollTo(0, 0);
}

document.addEventListener("click", (e) => {
  const a = (e.target as Element).closest("a[data-link]") as HTMLAnchorElement | null;
  if (!a || e.metaKey || e.ctrlKey || e.shiftKey || a.target) return;
  e.preventDefault();
  if (a.pathname !== location.pathname || a.search !== location.search) navigate(a.pathname + a.search);
});
window.addEventListener("popstate", render);
render();
