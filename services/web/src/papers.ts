// All papers, with the filters in the address bar.
import { crumbs, addToggle } from "./components";
import { listing, type Listing } from "./listing";
import { h } from "./dom";
import type { View } from "./main";

export function papersView(): View {
  let current: Listing | null = listing();
  return {
    el: h(
      "section",
      { class: "library-page papers" },
      h("div", { class: "page-head" }, crumbs(["Library", "/"], ["Papers"]), h("h1", {}, "Papers"), addToggle(() => current?.el.dispatchEvent(new Event("reload")))),
      current.el,
    ),
    title: "Papers",
    destroy: () => {
      current?.destroy();
      current = null;
    },
  };
}
