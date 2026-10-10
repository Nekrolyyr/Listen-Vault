import "./style.css";
import { h } from "./dom";
import { libraryView } from "./library";
import { readerView } from "./reader";
import { settingsView } from "./settings";

export interface View {
  el: HTMLElement;
  destroy?: () => void;
  title?: string;
}

const app = document.getElementById("app")!;
let current: View | null = null;

function shell(view: View, active: string): HTMLElement {
  const link = (href: string, label: string) =>
    h("a", { href, "data-link": true, class: active === href ? "active" : "", "aria-current": active === href ? "page" : null }, label);
  return h(
    "div",
    { class: "shell" },
    h(
      "header",
      { class: "topbar" },
      h("a", { href: "/", "data-link": true, class: "brand" }, h("img", { src: "/favicon.svg", alt: "", width: 24, height: 24 }), h("span", { class: "brand-name" }, "Listen Vault")),
      h("nav", {}, link("/", "Library"), link("/settings", "Settings"), h("a", { href: "/auth/logout", class: "quiet" }, "Sign out")),
    ),
    h("main", { class: "content" }, view.el),
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
  let view: View;
  let active = "/";
  const doc = path.match(/^\/doc\/([0-9a-f-]{36})$/);
  if (doc) view = readerView(doc[1]);
  else if (path === "/settings") {
    view = settingsView();
    active = "/settings";
  } else view = libraryView();
  current = view;
  app.replaceChildren(shell(view, active));
  document.title = view.title ? `${view.title} · Listen Vault` : "Listen Vault";
  window.scrollTo(0, 0);
}

document.addEventListener("click", (e) => {
  const a = (e.target as Element).closest("a[data-link]") as HTMLAnchorElement | null;
  if (!a || e.metaKey || e.ctrlKey || e.shiftKey || a.target) return;
  e.preventDefault();
  if (a.pathname !== location.pathname) navigate(a.pathname);
});
window.addEventListener("popstate", render);
render();
