import "./style.css";
import { abstractBar } from "./abstract";
import { accountView, adminView, loginView, registerView, termsView, verifyView } from "./accounts";
import { areaView } from "./area";
import { h } from "./dom";
import { exploreView } from "./explore";
import { homeView } from "./home";
import { papersView } from "./papers";
import { readerView } from "./reader";
import { session, loadSession } from "./session";
import { onboardingView } from "./onboarding";
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
type Section = "library" | "papers" | "explore" | "settings" | "reader" | "account" | "admin";

const ROUTES: [RegExp, (m: RegExpMatchArray) => View, Section][] = [
  [/^\/$/, () => homeView(), "library"],
  [/^\/areas\/([a-z_]{2,20})$/, (m) => areaView(m[1]), "library"],
  [/^\/topics\/(\d+)$/, (m) => topicView(Number(m[1])), "library"],
  [/^\/papers$/, () => papersView(), "papers"],
  [/^\/explore$/, () => exploreView(), "explore"],
  [/^\/settings$/, () => settingsView(), "settings"],
  [/^\/doc\/([0-9a-f-]{36})$/, (m) => readerView(m[1]), "reader"],
  [/^\/account$/, () => accountView(session!), "account"],
  [/^\/admin$/, () => adminView(), "admin"],
];

/** Pages that work without a session: they are shown without the navigation. */
const PUBLIC_VIEWS: Record<string, () => View> = {
  "/login": loginView,
  "/register": registerView,
  "/verify": verifyView,
  "/terms": termsView,
};

function shell(view: View, active: Section): HTMLElement {
  const link = (href: string, label: string, section: Section) => {
    const on = active === section;
    return h("a", { href, "data-link": true, class: on ? "active" : "", "aria-current": on ? "page" : null }, label);
  };
  const notices: HTMLElement[] = [];
  if (session && session.terms.current && !session.terms.accepted) {
    notices.push(h("div", { class: "notice terms-notice" },
      h("span", {}, "The terms of service have changed. You need to accept them before you add papers. "),
      h("a", { href: "/terms", "data-link": true }, "Read and accept")));
  }
  return h(
    "div",
    { class: "shell" },
    h(
      "header",
      { class: "topbar" },
      h("a", { href: "/", "data-link": true, class: "brand" }, h("img", { src: "/favicon.svg", alt: "", width: 24, height: 24 }), h("span", { class: "brand-name" }, "Listen Vault")),
      h("nav", { "aria-label": "Main" },
        link("/", "Library", "library"), link("/papers", "Papers", "papers"), link("/explore", "Explore", "explore"),
        link("/settings", "Settings", "settings"), link("/account", "Account", "account"),
        session?.is_admin ? link("/admin", "Admin", "admin") : null),
    ),
    ...notices,
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
  const publicView = PUBLIC_VIEWS[path];
  if (publicView) {
    if (session && (path === "/login" || path === "/register")) return navigate("/", true);
    current = publicView();
    app.replaceChildren(h("main", { class: "auth-page" }, current.el));
    document.title = current.title ? `${current.title} · Listen Vault` : "Listen Vault";
    window.scrollTo(0, 0);
    return;
  }
  if (!session) return navigate("/login", true);
  if (path === "/welcome") {
    current = onboardingView();
    app.replaceChildren(h("main", { class: "auth-page" }, current.el));
    document.title = "Welcome · Listen Vault";
    window.scrollTo(0, 0);
    return;
  }
  // the tour comes first; the terms and the account page stay open so nobody is stuck
  if (!session.onboarded && path !== "/terms" && path !== "/account") return navigate("/welcome", true);
  if (path === "/admin" && !session.is_admin) return navigate("/", true);
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
void loadSession().then(render);
