import { api, type DocDetail, type Sentence, type TextChapter } from "./api";
import { authors, clock, duration, h, icon, toast } from "./dom";
import type { View } from "./main";
import { PdfView } from "./pdfview";
import { Player } from "./player";

const RATES = [0.75, 0.9, 1, 1.1, 1.25, 1.5, 1.75, 2];

export function readerView(id: string): View {
  let doc: DocDetail | null = null;
  let player: Player | null = null;
  let pdf: PdfView | null = null;
  let text: TextChapter[] = [];
  let follow = true;
  let poll = 0;
  let destroyed = false;
  let currentSentence: Sentence | null = null;

  const head = h("div", { class: "reader-head" });
  const docPane = h("div", { class: "doc-pane" }, h("p", { class: "pane-message" }, "Loading…"));
  const nowReading = h("div", { class: "now-reading", "aria-live": "off" });
  const chapterList = h("ol", { class: "chapter-list" });
  const sidePane = h("aside", { class: "side-pane", "aria-label": "Chapters" }, nowReading, chapterList);
  const tabs = h(
    "div",
    { class: "pane-tabs", role: "tablist" },
    h("button", { type: "button", role: "tab", "aria-selected": "true", onclick: () => showTab("doc") }, "Document"),
    h("button", { type: "button", role: "tab", "aria-selected": "false", onclick: () => showTab("chapters") }, "Chapters"),
  );
  const body = h("div", { class: "reader-body tab-doc" }, docPane, sidePane);

  // ---------------------------------------------------------------- player bar
  const playBtn = h("button", { type: "button", class: "play", "aria-label": "Play", onclick: () => player?.toggle() }, icon("play"));
  const seek = h("input", { type: "range", min: 0, max: 1, step: 0.1, value: 0, "aria-label": "Position in chapter" }) as HTMLInputElement;
  const timeNow = h("span", { class: "time" }, "0:00");
  const timeEnd = h("span", { class: "time" }, "0:00");
  const chapterLabel = h("div", { class: "bar-title" }, "");
  const waiting = h("div", { class: "bar-waiting", hidden: true });
  const rateSelect = h(
    "select",
    { "aria-label": "Playback speed", onchange: (e: Event) => player?.setRate(Number((e.target as HTMLSelectElement).value)) },
    ...RATES.map((r) => h("option", { value: r, selected: r === 1 }, `${r}×`)),
  ) as HTMLSelectElement;
  const followBtn = h(
    "button",
    { type: "button", class: "icon-btn follow on", "aria-pressed": "true", title: "Follow playback in the document", onclick: () => setFollow(!follow) },
    icon("follow"),
    h("span", { class: "label" }, "Follow"),
  );
  let seeking = false;
  seek.addEventListener("input", () => {
    seeking = true;
    timeNow.textContent = clock(Number(seek.value));
  });
  seek.addEventListener("change", () => {
    player?.seekTo(Number(seek.value));
    seeking = false;
  });
  const bar = h(
    "div",
    { class: "player-bar" },
    h("div", { class: "bar-info" }, chapterLabel, waiting),
    h(
      "div",
      { class: "bar-controls" },
      h("button", { type: "button", class: "icon-btn", "aria-label": "Previous chapter", onclick: () => player?.prev() }, icon("prev")),
      h("button", { type: "button", class: "icon-btn", "aria-label": "Back 15 seconds", onclick: () => player?.skip(-15) }, icon("back15")),
      playBtn,
      h("button", { type: "button", class: "icon-btn", "aria-label": "Forward 15 seconds", onclick: () => player?.skip(15) }, icon("fwd15")),
      h("button", { type: "button", class: "icon-btn", "aria-label": "Next chapter", onclick: () => player?.next() }, icon("next")),
    ),
    h("div", { class: "bar-seek" }, timeNow, seek, timeEnd),
    h("div", { class: "bar-extra" }, rateSelect, followBtn),
  );

  function showTab(tab: "doc" | "chapters"): void {
    body.classList.toggle("tab-doc", tab === "doc");
    body.classList.toggle("tab-chapters", tab === "chapters");
    tabs.querySelectorAll("button").forEach((b, i) => b.setAttribute("aria-selected", String((i === 0) === (tab === "doc"))));
  }

  function setFollow(on: boolean): void {
    follow = on;
    followBtn.classList.toggle("on", on);
    followBtn.setAttribute("aria-pressed", String(on));
    if (on && currentSentence) followSentence(currentSentence);
  }

  function followSentence(s: Sentence): void {
    if (pdf && s.page) pdf.showPage(s.page);
    if (!pdf) {
      const el = docPane.querySelector(`[data-s="${player?.chapterIdx}-${s.i}"]`);
      el?.scrollIntoView({ block: "center", behavior: "smooth" });
    }
  }

  // ---------------------------------------------------------------- rendering
  function renderHead(): void {
    if (!doc) return;
    const status =
      doc.status === "ready"
        ? `${duration(doc.duration_s)} · ${doc.chapters.length} chapters`
        : doc.status === "failed"
          ? `Processing failed: ${doc.error ?? ""}`
          : `Generating audio · ${doc.chapters.filter((c) => c.ready).length} of ${doc.chapters.length || "…"} chapters ready`;
    head.replaceChildren(
      h("a", { href: "/", "data-link": true, class: "back", "aria-label": "Back to library" }, icon("arrowLeft")),
      h(
        "div",
        { class: "reader-title" },
        h("h1", {}, doc.title),
        h("p", {}, [authors(doc.authors), doc.published?.slice(0, 4), status].filter(Boolean).join(" · ")),
      ),
      ...(doc.source_url ? [h("a", { href: doc.source_url, target: "_blank", rel: "noopener noreferrer", class: "btn small" }, "Source")] : []),
    );
  }

  function renderChapters(): void {
    if (!doc) return;
    if (!doc.chapters.length) {
      chapterList.replaceChildren(h("li", { class: "pane-message" }, doc.status === "failed" ? "No chapters." : "Chapters appear here once the structure is analysed."));
      return;
    }
    chapterList.replaceChildren(
      ...doc.chapters.map((c) =>
        h(
          "li",
          { class: `chapter${c.idx === player?.chapterIdx ? " current" : ""}${c.ready ? "" : " pending"}` },
          h(
            "button",
            {
              type: "button",
              "aria-current": c.idx === player?.chapterIdx ? "true" : null,
              onclick: () => {
                player?.load(c.idx, 0, true);
                if (c.start_page && pdf) pdf.showPage(c.start_page);
                setFollow(true);
              },
            },
            h("span", { class: "ch-title" }, c.title),
            h("span", { class: "ch-meta" }, c.ready ? duration(c.duration_s) : "generating…", c.start_page ? ` · p. ${c.start_page}` : ""),
          ),
          c.intro ? h("p", { class: "ch-intro" }, c.intro) : null,
        ),
      ),
    );
  }

  function renderText(): void {
    if (pdf || !doc) return;
    if (!text.length) {
      docPane.replaceChildren(h("p", { class: "pane-message" }, "The article text appears here once it has been read."));
      return;
    }
    const article = h("article", { class: "article-text" });
    for (const ch of text) {
      if (ch.idx === 0) continue; // overview is spoken, shown in the chapter list
      const sec = h("section", { "data-ch": ch.idx ?? "" }, ch.title ? h("h2", {}, ch.title) : null);
      if (ch.idx !== null && ch.idx === player?.chapterIdx && player.sentences.length) {
        const p = h("p", {});
        for (const s of player.sentences.slice(1)) {
          p.append(h("span", { class: "s", "data-s": `${ch.idx}-${s.i}`, onclick: () => player?.seekToSentence(s) }, s.text), " ");
        }
        sec.append(p);
      } else {
        for (const para of ch.paragraphs) {
          if (para.kind === "heading") sec.append(h("h3", {}, para.text));
          else if (para.kind !== "equation") sec.append(h("p", { class: para.kind }, para.text));
        }
      }
      article.append(sec);
    }
    docPane.replaceChildren(article);
  }

  function renderState(): void {
    if (!player) return;
    const a = player.audio;
    playBtn.replaceChildren(icon(player.playing ? "pause" : "play"));
    playBtn.setAttribute("aria-label", player.playing ? "Pause" : "Play");
    if (!seeking) {
      seek.max = String(a.duration || 1);
      seek.value = String(a.currentTime || 0);
      timeNow.textContent = clock(a.currentTime);
    }
    timeEnd.textContent = clock(a.duration || 0);
  }

  // ---------------------------------------------------------------- load
  async function refresh(first = false): Promise<void> {
    try {
      const fresh = await api.document(id);
      const hadChapters = doc?.chapters.map((c) => `${c.idx}${c.ready}`).join() ?? "";
      doc = fresh;
      if (first) await setup();
      else player?.update(fresh);
      renderHead();
      if (hadChapters !== fresh.chapters.map((c) => `${c.idx}${c.ready}`).join()) {
        renderChapters();
        if (!pdf) {
          text = await api.text(id);
          renderText();
        }
      }
    } catch (e) {
      toast((e as Error).message, "error");
    }
    window.clearTimeout(poll);
    if (!destroyed && doc && doc.status !== "ready" && doc.status !== "failed") poll = window.setTimeout(refresh, 4000);
  }

  async function setup(): Promise<void> {
    if (!doc) return;
    if (doc.has_pdf) {
      pdf = new PdfView(api.pdfUrl(id), () => setFollow(false));
      docPane.replaceChildren(pdf.el);
    } else {
      text = await api.text(id);
    }
    player = new Player(doc, {
      onChapter: (idx) => {
        const c = doc?.chapters.find((x) => x.idx === idx);
        chapterLabel.textContent = c?.title ?? "";
        renderChapters();
        renderText();
        if (follow && c?.start_page && pdf) pdf.showPage(c.start_page);
      },
      onSentence: (s, i) => {
        currentSentence = s;
        nowReading.replaceChildren(
          s ? h("p", {}, h("span", { class: "label" }, "Now reading"), s.text) : h("p", { class: "quiet" }, "Press play to listen."),
        );
        docPane.querySelectorAll(".s.current").forEach((el) => el.classList.remove("current"));
        if (s) {
          docPane.querySelector(`[data-s="${player?.chapterIdx}-${s.i}"]`)?.classList.add("current");
          if (follow) followSentence(s);
        }
        void i;
      },
      onState: renderState,
      onWaiting: (idx) => {
        waiting.hidden = idx === null;
        waiting.textContent = idx === null ? "" : "This chapter is still being generated. It starts automatically when ready.";
      },
    });
    const pb = await api.playback(id).catch(() => null);
    if (pb) {
      player.rate = pb.rate;
      rateSelect.value = String(RATES.includes(pb.rate) ? pb.rate : 1);
      player.audio.playbackRate = pb.rate;
    }
    const start = pb && doc.chapters.some((c) => c.idx === pb.chapter_idx) ? pb : { chapter_idx: 0, position_s: 0 };
    await player.load(start.chapter_idx, start.position_s, false);
    nowReading.replaceChildren(h("p", { class: "quiet" }, pb && pb.position_s > 0 ? "Press play to resume where you left off." : "Press play to listen."));
    renderText();
  }

  const onKey = (e: KeyboardEvent) => {
    const t = e.target as HTMLElement;
    if (t.closest("input, select, textarea, [contenteditable]") || e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === " " || e.key === "k") {
      e.preventDefault();
      player?.toggle();
    } else if (e.key === "ArrowLeft" || e.key === "j") player?.skip(-15);
    else if (e.key === "ArrowRight" || e.key === "l") player?.skip(15);
  };
  document.addEventListener("keydown", onKey);

  const el = h("section", { class: "reader" }, head, tabs, body, bar);
  refresh(true);
  return {
    el,
    title: "Reader",
    destroy: () => {
      destroyed = true;
      window.clearTimeout(poll);
      document.removeEventListener("keydown", onKey);
      player?.destroy();
      pdf?.destroy();
    },
  };
}
