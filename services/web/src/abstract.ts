// The abstract track: the overview of a paper is its abstract, so it can be played from any library page.
// One player for the whole app; the shell shows its bar while something plays.
import { api, type Doc } from "./api";
import { h, icon, toast } from "./dom";

const audio = new Audio();
const title = h("span", { class: "bar-title" });
const playBtn = h("button", { type: "button", class: "icon-btn", "aria-label": "Play", onclick: () => (audio.paused ? void audio.play() : audio.pause()) }, icon("play"));

export const abstractBar = h(
  "div",
  { class: "abstract-bar", hidden: true, role: "region", "aria-label": "Abstract player" },
  playBtn,
  title,
  h("button", { type: "button", class: "btn small", onclick: () => stopAbstract() }, "Close"),
);

audio.addEventListener("play", () => playBtn.replaceChildren(icon("pause")));
audio.addEventListener("pause", () => playBtn.replaceChildren(icon("play")));
audio.addEventListener("ended", () => playBtn.replaceChildren(icon("play")));

export function listenAbstract(doc: Doc): void {
  audio.src = api.audioUrl(doc.id, 0);
  title.textContent = `Abstract · ${doc.title}`;
  abstractBar.hidden = false;
  audio.play().catch(() => toast("Press play to listen.", "info"));
}

export function stopAbstract(): void {
  audio.pause();
  audio.removeAttribute("src");
  abstractBar.hidden = true;
}
