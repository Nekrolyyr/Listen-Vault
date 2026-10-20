import { api, type DocDetail, type Question, type Sentence, type TextChapter } from "./api";
import { stopAbstract } from "./abstract";
import { areaName } from "./areas";
import { authors, clock, duration, h, icon, toast } from "./dom";
import type { View } from "./main";
import { PdfView } from "./pdfview";
import { Player } from "./player";
import { WPM_DEFAULT, WPM_MAX, WPM_MIN, clampWpm, sentenceIndexAt } from "./speedread";
import { SpeedView } from "./speedview";

const RATES = [0.75, 0.9, 1, 1.1, 1.25, 1.5, 1.75, 2];
const MODE_KEY = "lv.readMode";
const HEADER_KEY = "lv.headerOpen";
// the content is three columns from this width of the reader on; keep in step with the container query in style.css
const WIDE_MIN = 1180;
const SAVE_EVERY_MS = 10_000;

export function readerView(id: string): View {
  stopAbstract(); // the reader has its own player
  let doc: DocDetail | null = null;
  let player: Player | null = null;
  let pdf: PdfView | null = null;
  let text: TextChapter[] = [];
  let follow = true;
  let poll = 0;
  let destroyed = false;
  let currentSentence: Sentence | null = null;
  // "read" is remembered per viewer; the mode is only "read" while the reading stage is on screen
  let mode: "listen" | "read" = storedValue(MODE_KEY) === "read" ? "read" : "listen";
  // where in the chapter speed reading starts, once that chapter's sentences have arrived
  let pendingTime: number | null = null;
  let wpm = WPM_DEFAULT;
  let lastSave = 0;
  let sentenceDragging = false;

  // ---------------------------------------------------------------- header: a bar, and the paper's details when open
  // Phones start with the bar only; wide screens show the details. The choice is remembered per viewer.
  const storedHeader = storedValue(HEADER_KEY);
  let headerOpen = storedHeader === null ? window.innerWidth > 900 : storedHeader === "open";
  const detailsId = "reader-details";
  const titleHeading = h("h1", {});
  const statusDot = h("span", { class: "status-dot", "aria-hidden": "true" });
  const headerToggle = h(
    "button",
    { type: "button", class: "icon-btn header-toggle", "aria-controls": detailsId, onclick: () => setHeaderOpen(!headerOpen, true) },
    icon("chevron"),
  );
  const headerBar = h(
    "div",
    { class: "reader-head" },
    h("a", { href: "/", "data-link": true, class: "back", "aria-label": "Back to library" }, icon("arrowLeft")),
    h("div", { class: "reader-title" }, titleHeading),
    statusDot,
    headerToggle,
  );
  const metaLine = h("p", { class: "reader-meta" });
  const sourceLink = h("a", { target: "_blank", rel: "noopener noreferrer", class: "btn small" }, "Source");
  const makeAudioBtn = h("button", { type: "button", class: "btn small primary", onclick: makeAudio }, "Make audio");
  const markReadLabel = h("span", { class: "label" }, "Mark as read");
  const markReadBtn = h("button", { type: "button", class: "btn small", onclick: toggleRead }, icon("check"), markReadLabel);
  const actions = h("div", { class: "reader-actions" }, markReadBtn, sourceLink, makeAudioBtn);
  const tagList = h("ul", { class: "tag-list", "aria-label": "Your tags" });
  const tagInput = h("input", { type: "text", maxlength: 32, placeholder: "Tag name", "aria-label": "Tag name" }) as HTMLInputElement;
  const tagForm = h(
    "form",
    { class: "tag-form", hidden: true, onsubmit: addTag },
    tagInput,
    h("button", { type: "submit", class: "btn small primary" }, "Add"),
    h("button", { type: "button", class: "btn small", onclick: closeTagForm }, "Cancel"),
  );
  const addTagBtn = h("button", { type: "button", class: "btn small", onclick: openTagForm }, icon("plus"), "Add tag");
  tagInput.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeTagForm();
  });
  const keywordChips = h("div", { class: "keyword-chips" });
  const keywordsSection = h("details", { class: "header-section" }, h("summary", {}, "Keywords"), keywordChips);
  const areasLine = h("p", { class: "header-text" });
  const areasSection = h("details", { class: "header-section" }, h("summary", {}, "Areas and topics"), areasLine);
  const details = h(
    "div",
    { class: "reader-details", id: detailsId },
    metaLine,
    actions,
    h("section", { class: "header-tags" }, h("h2", {}, "Tags"), tagList, addTagBtn, tagForm),
    keywordsSection,
    areasSection,
  );
  const head = h("header", { class: "reader-header" }, headerBar, details);

  const docPane = h("div", { class: "doc-pane" }, h("p", { class: "pane-message" }, "Loading…"));
  const nowReading = h("div", { class: "now-reading", "aria-live": "off" });
  const chapterList = h("ol", { class: "chapter-list" });

  // ---------------------------------------------------------------- questions about the paper
  let questions: Question[] = [];
  let qaTimer = 0;
  const qaList = h("ol", { class: "qa-list", "aria-live": "polite" });
  const qaText = h("textarea", { rows: 2, maxlength: 500, placeholder: "Ask about this paper…", "aria-label": "Your question" }) as HTMLTextAreaElement;
  const qaSubmit = h("button", { type: "submit", class: "btn small primary" }, "Ask");
  const qaForm = h(
    "form",
    {
      class: "qa-form",
      onsubmit: async (e: Event) => {
        e.preventDefault();
        const question = qaText.value.trim();
        if (!question) return;
        try {
          await api.ask(id, question);
          qaText.value = "";
          loadQuestions();
        } catch (err) {
          toast((err as Error).message, "error");
        }
      },
    },
    qaText,
    qaSubmit,
  );
  const relatedList = h("ul", { class: "related-list" }, h("li", { class: "quiet" }, "Looking for related papers…"));
  const relatedBox = h("section", { class: "related-papers" }, h("h2", {}, "Related papers"), relatedList);
  const qaBox = h("section", { class: "qa" }, h("h2", {}, "Ask about this paper"), qaForm, qaList, relatedBox);
  // Three columns when the reader is wide (document, chapters, questions); narrower, one pane at a time behind tabs.
  // The splitters between the columns only show in the wide layout.
  const splitA = splitter("Resize the chapters column");
  const splitB = splitter("Resize the questions column");
  const chaptersPane = h("aside", { class: "chapters-pane", "aria-label": "Chapters" }, nowReading, chapterList);
  const qaPane = h("aside", { class: "qa-pane", "aria-label": "Questions" }, qaBox);
  const tabs = h(
    "div",
    { class: "pane-tabs reader-tabs", role: "tablist" },
    h("button", { type: "button", role: "tab", "aria-selected": "true", onclick: () => showTab("doc") }, "Document"),
    h("button", { type: "button", role: "tab", "aria-selected": "false", onclick: () => showTab("chapters") }, "Chapters"),
    h("button", { type: "button", role: "tab", "aria-selected": "false", onclick: () => showTab("qa") }, "Questions"),
  );
  const body = h("div", { class: "reader-body tab-doc" }, docPane, splitA, chaptersPane, splitB, qaPane);
  const tabButtons = tabs.querySelectorAll("button");
  const main = h("div", { class: "reader-main" }, tabs, body);

  // ---------------------------------------------------------------- player bar
  const playBtn = h("button", { type: "button", class: "play", "aria-label": "Play", onclick: () => player?.toggle() }, icon("play"));
  const seek = h("input", { type: "range", min: 0, max: 1, step: 0.1, value: 0, "aria-label": "Position in chapter" }) as HTMLInputElement;
  const timeNow = h("span", { class: "time" }, "0:00");
  const timeEnd = h("span", { class: "time" }, "0:00");
  const chapterLabel = h("div", { class: "bar-title" }, "");
  const waiting = h("div", { class: "bar-waiting", hidden: true });
  const rateSelect = h(
    "select",
    { class: "rate-select", "aria-label": "Playback speed", onchange: (e: Event) => player?.setRate(Number((e.target as HTMLSelectElement).value)) },
    ...RATES.map((r) => h("option", { value: r, selected: r === 1 }, `${r}×`)),
  ) as HTMLSelectElement;
  const followBtn = h(
    "button",
    { type: "button", class: "icon-btn follow on", "aria-pressed": "true", title: "Follow playback in the document", onclick: () => setFollow(!follow) },
    icon("follow"),
    h("span", { class: "label" }, "Follow"),
  );
  // Listen | Read: the same position, read aloud or one word at a time (the read controls replace the audio controls)
  const listenBtn = h("button", { type: "button", "aria-pressed": "true", onclick: () => setMode("listen") }, icon("headphones"), h("span", {}, "Listen"));
  const readBtn = h("button", { type: "button", "aria-pressed": "false", onclick: () => setMode("read") }, icon("book"), h("span", {}, "Read"));
  const modeSwitch = h("div", { class: "mode-switch", role: "group", "aria-label": "Listen or speed read" }, listenBtn, readBtn);
  const sentenceSeek = h("input", { type: "range", min: 0, max: 1, step: 1, value: 0, "aria-label": "Sentence" }) as HTMLInputElement;
  const sentenceNow = h("span", { class: "time" }, "0 / 0");
  const wpmSeek = h("input", { type: "range", min: WPM_MIN, max: WPM_MAX, step: 10, value: WPM_DEFAULT, "aria-label": "Words per minute" }) as HTMLInputElement;
  const wpmNow = h("span", { class: "time" }, `${WPM_DEFAULT} wpm`);
  // the read controls take the places of the seek bar and the transport row, so the bar keeps its height in both modes
  const readSeek = h("div", { class: "read-seek" }, sentenceNow, sentenceSeek);
  const readWpm = h("div", { class: "read-wpm" }, wpmNow, wpmSeek);
  let seeking = false;
  seek.addEventListener("input", () => {
    seeking = true;
    timeNow.textContent = clock(Number(seek.value));
    paint(seek);
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
      rateSelect,
      followBtn,
    ),
    h("div", { class: "bar-seek" }, timeNow, seek, timeEnd),
    readSeek,
    readWpm,
    h("div", { class: "bar-mode" }, modeSwitch),
  );

  sentenceSeek.addEventListener("input", () => {
    sentenceDragging = true;
    paint(sentenceSeek);
    speed.preview(Number(sentenceSeek.value));
  });
  sentenceSeek.addEventListener("change", () => {
    sentenceDragging = false;
    jumpTo(Number(sentenceSeek.value));
  });
  wpmSeek.addEventListener("input", () => setWpm(Number(wpmSeek.value), false));
  wpmSeek.addEventListener("change", () => setWpm(Number(wpmSeek.value), true));

  // Speed reading: one word at a time. It keeps the audio position in step with the sentence it shows.
  const speed = new SpeedView({
    onIndex: (i) => {
      const s = player?.sentences[i];
      if (!s) return;
      player?.seekTo(s.start);
      if (!sentenceDragging) syncSentenceBar();
      nowReading.replaceChildren(h("p", {}, h("span", { class: "label" }, "Now reading"), s.text));
      if (Date.now() - lastSave > SAVE_EVERY_MS) saveNow();
    },
    onStop: () => {
      syncSentenceBar();
      saveNow();
    },
    nextChapter: () => {
      const cur = player?.chapterIdx ?? -1;
      const next = doc?.chapters.filter((c) => c.idx > cur).sort((a, b) => a.idx - b.idx)[0];
      if (!player || !next) return false;
      if (!next.ready) {
        toast("The next chapter is still being generated.");
        return false;
      }
      void player.load(next.idx, 0, false);
      return true;
    },
  });

  function showTab(tab: "doc" | "chapters" | "qa"): void {
    // the reading stage is hidden on the other tabs, so reading pauses there
    if (tab !== "doc") speed.stop();
    body.classList.toggle("tab-doc", tab === "doc");
    body.classList.toggle("tab-chapters", tab === "chapters");
    body.classList.toggle("tab-qa", tab === "qa");
    const order = ["doc", "chapters", "qa"];
    tabButtons.forEach((b, i) => b.setAttribute("aria-selected", String(order[i] === tab)));
  }

  function setHeaderOpen(open: boolean, save = false): void {
    headerOpen = open;
    head.classList.toggle("open", open);
    details.hidden = !open;
    headerToggle.setAttribute("aria-expanded", String(open));
    headerToggle.setAttribute("aria-label", open ? "Hide details" : "Show details");
    headerToggle.title = open ? "Hide details" : "Show details";
    if (save) storeValue(HEADER_KEY, open ? "open" : "closed");
  }

  function saveNow(): void {
    lastSave = Date.now();
    player?.save();
  }

  function chapterTitle(): string {
    return doc?.chapters.find((c) => c.idx === player?.chapterIdx)?.title ?? "";
  }

  function syncSentenceBar(): void {
    const { index, count } = speed.position;
    sentenceSeek.max = String(Math.max(0, count - 1));
    sentenceSeek.value = String(index);
    paint(sentenceSeek);
    sentenceNow.textContent = count ? `${index + 1} / ${count}` : "0 / 0";
  }

  /** A jump is saved at once, like a seek in the audio. Reading saves every few seconds instead. */
  function jumpTo(index: number): void {
    speed.seek(index);
    saveNow();
  }

  function stepSentence(delta: number): void {
    const { index, count } = speed.position;
    if (count) jumpTo(Math.min(count - 1, Math.max(0, index + delta)));
  }

  function setWpm(value: number, persist: boolean): void {
    wpm = clampWpm(value);
    speed.setWpm(wpm);
    wpmSeek.value = String(wpm);
    paint(wpmSeek);
    wpmNow.textContent = `${wpm} wpm`;
    if (persist) api.saveSettings({ reading_wpm: wpm }).catch((err) => toast((err as Error).message, "error"));
  }

  function setMode(next: "listen" | "read"): void {
    if (next === mode || !player) return;
    if (next === "read") {
      if (!enterRead(player.time)) return;
    } else exitRead();
    storeValue(MODE_KEY, mode);
  }

  /** Starts speed reading at `time` (seconds into the chapter). False when the chapter is not ready yet. */
  function enterRead(time: number, quiet = false): boolean {
    const ready = doc?.chapters.find((c) => c.idx === player?.chapterIdx)?.ready;
    if (!player || !ready) {
      if (!quiet) toast("This chapter is still being generated. Speed reading starts when it is ready.");
      return false;
    }
    player.setReading(true);
    mode = "read";
    pendingTime = time;
    showMode();
    if (player.sentences.length) syncSpeed();
    else speed.setChapter([], 0, chapterTitle());
    return true;
  }

  function exitRead(): void {
    const s = player?.sentences[speed.position.index];
    if (player && s) player.seekTo(s.start); // the audio resumes at the sentence being read
    speed.stop();
    mode = "listen";
    player?.setReading(false);
    saveNow();
    showMode();
  }

  /** Gives the reading stage the sentences of the current chapter, starting where the listener was. */
  function syncSpeed(): void {
    const list = player?.sentences ?? [];
    const index = Math.max(0, sentenceIndexAt(list, pendingTime ?? 0));
    pendingTime = null;
    speed.setMessage(null);
    speed.setChapter(list, index, chapterTitle());
    syncSentenceBar();
  }

  function showMode(): void {
    const reading = mode === "read";
    bar.classList.toggle("reading", reading);
    listenBtn.setAttribute("aria-pressed", String(!reading));
    readBtn.setAttribute("aria-pressed", String(reading));
    if (reading) docPane.replaceChildren(speed.el);
    else if (pdf) docPane.replaceChildren(pdf.el);
    else renderText();
    syncSentenceBar();
  }

  function setFollow(on: boolean): void {
    follow = on;
    followBtn.classList.toggle("on", on);
    followBtn.setAttribute("aria-pressed", String(on));
    // "Follow" resumes following; while following, the label says so
    followBtn.querySelector(".label")!.textContent = on ? "Following" : "Follow";
    followBtn.title = on ? "Following playback. Scroll the document to browse freely." : "Resume following playback";
    if (on && currentSentence) followSentence(currentSentence);
  }

  function followSentence(s: Sentence): void {
    if (pdf) {
      pdf.showSentence(s.page, s.rects ?? []);
      return;
    }
    const el = docPane.querySelector(`[data-s="${player?.chapterIdx}-${s.i}"]`);
    el?.scrollIntoView({ block: "center", behavior: "smooth" });
  }

  // ---------------------------------------------------------------- rendering
  function renderHead(): void {
    if (!doc) return;
    // audio is the signed-in user's own: a paper can be ready and still have no audio for this user yet
    const noAudio = doc.status === "ready" && (doc.audio_state === null || doc.audio_state === "failed");
    const status =
      doc.status === "ready" && doc.audio_state === "ready"
        ? `${duration(doc.duration_s)} · ${doc.chapters.length} chapters`
        : doc.status === "failed"
          ? `Processing failed: ${doc.error ?? ""}`
          : noAudio
            ? doc.audio_state === "failed" ? `Audio failed: ${doc.job?.error ?? "try again"}` : "No audio yet"
            : doc.status === "ready"
              ? `Generating audio · ${doc.chapters.filter((c) => c.ready).length} of ${doc.chapters.length || "…"} chapters ready`
              : `Processing · ${doc.job?.stage ?? "queued"}`;
    const tone = doc.status === "failed" ? "bad" : doc.status === "ready" && doc.audio_state === "ready" ? "ok" : "busy";
    titleHeading.textContent = doc.title;
    statusDot.dataset.tone = tone;
    statusDot.title = status;
    metaLine.textContent = [authors(doc.authors), doc.published?.slice(0, 4), status].filter(Boolean).join(" · ");
    sourceLink.hidden = !doc.source_url;
    if (doc.source_url) sourceLink.href = doc.source_url;
    makeAudioBtn.hidden = !noAudio;
    markReadLabel.textContent = doc.listened ? "Marked as read" : "Mark as read";
    markReadBtn.setAttribute("aria-pressed", String(doc.listened));
    qaSubmit.disabled = doc.status !== "ready";
    qaText.disabled = doc.status !== "ready";
    const terms = [...doc.keywords.task, ...doc.keywords.method, ...doc.keywords.topic, ...doc.keywords.keyword];
    keywordChips.replaceChildren(...terms.map((t) => h("span", { class: "chip" }, t)));
    keywordsSection.hidden = terms.length === 0;
    const areas = doc.areas ? [doc.areas.primary, ...doc.areas.also].map(areaName) : [];
    areasLine.textContent = [...areas, ...(doc.topics ?? []).map((t) => t.name)].join(" · ");
    areasSection.hidden = areasLine.textContent === "";
    renderTags();
  }

  function renderTags(): void {
    const tags = doc?.tags ?? [];
    tagList.replaceChildren(
      ...(tags.length
        ? tags.map((t) =>
            h(
              "li",
              { class: "tag-chip" },
              h("span", {}, t),
              h("button", {
                type: "button",
                class: "tag-remove",
                title: `Remove ${t}`,
                "aria-label": `Remove tag ${t}`,
                onclick: () => void saveTags(tags.filter((x) => x !== t)),
              }, icon("x")),
            ),
          )
        : [h("li", { class: "quiet" }, "No tags yet.")]),
    );
  }

  /** Stores the tags the paper should have. False, with the reason in a toast, when the server refuses them. */
  async function saveTags(next: string[]): Promise<boolean> {
    if (!doc) return false;
    try {
      doc.tags = (await api.setTags(id, next)).tags;
      renderTags();
      return true;
    } catch (e) {
      toast((e as Error).message, "error");
      return false;
    }
  }

  async function addTag(e: Event): Promise<void> {
    e.preventDefault();
    const tag = tagInput.value.trim();
    if (!tag || !doc) return;
    if (await saveTags([...doc.tags, tag])) closeTagForm();
  }

  function openTagForm(): void {
    tagForm.hidden = false;
    addTagBtn.hidden = true;
    tagInput.focus();
  }

  function closeTagForm(): void {
    tagForm.hidden = true;
    addTagBtn.hidden = false;
    tagInput.value = "";
    addTagBtn.focus();
  }

  async function toggleRead(): Promise<void> {
    if (!doc) return;
    try {
      doc.listened = (await api.patch(id, { listened: !doc.listened })).listened;
      renderHead();
    } catch (e) {
      toast((e as Error).message, "error");
    }
  }

  async function makeAudio(): Promise<void> {
    try {
      await api.makeAudio(id);
      toast("Making your audio. It plays as each chapter is ready.");
      void refresh();
    } catch (e) {
      toast((e as Error).message, "error");
    }
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
    if (pdf || !doc || mode === "read") return;
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

  async function loadRelated(): Promise<void> {
    try {
      const { papers } = await api.related(id);
      if (destroyed) return;
      relatedList.replaceChildren(...(papers.length
        ? papers.map((p) => h("li", {},
            h("a", { href: `/doc/${p.id}`, "data-link": true, class: "related-item", title: p.title },
              h("span", { class: "related-title" }, p.title),
              h("span", { class: "quiet" }, `${Math.round(p.similarity * 100)}%`))))
        : [h("li", { class: "quiet" }, "No related papers yet. They appear once the paper has been processed.")]));
    } catch {
      relatedList.replaceChildren(h("li", { class: "quiet" }, "Related papers are not available."));
    }
  }

  async function loadQuestions(): Promise<void> {
    window.clearTimeout(qaTimer);
    try {
      questions = await api.questions(id);
    } catch (e) {
      toast((e as Error).message, "error");
      return;
    }
    renderQuestions();
    if (!destroyed && questions.some((q) => q.status === "queued")) qaTimer = window.setTimeout(loadQuestions, 2500);
  }

  function renderQuestions(): void {
    if (!questions.length) {
      qaList.replaceChildren(h("li", { class: "quiet" }, doc?.status === "ready" ? "Ask anything the paper covers. Answers quote the passages they come from." : "Questions can be asked once the paper has been processed."));
      return;
    }
    qaList.replaceChildren(
      ...questions.map((q) => {
        const answer =
          q.status === "queued"
            ? h("p", { class: "quiet" }, "Reading the paper… this takes about 30 seconds.")
            : q.status === "failed"
              ? h("p", { class: "quiet" }, q.error ?? "No answer.")
              : h("p", { class: "a" }, q.answer ?? "");
        const cites = q.status === "done"
          ? h(
              "div",
              { class: "cites" },
              ...q.citations.filter((c) => c.page).map((c) =>
                h("button", {
                  type: "button",
                  class: "cite",
                  title: c.text,
                  onclick: () => pdf?.showPage(c.page!),
                  disabled: !pdf,
                }, `p. ${c.page}`),
              ),
            )
          : null;
        return h("li", {}, h("p", { class: "q" }, q.question), answer, cites);
      }),
    );
  }

  function renderState(): void {
    if (!player) return;
    const a = player.audio;
    playBtn.replaceChildren(icon(player.playing ? "pause" : "play"));
    playBtn.setAttribute("aria-label", player.playing ? "Pause" : "Play");
    if (!seeking) {
      seek.max = String(a.duration || 1);
      seek.value = String(a.currentTime || 0);
      paint(seek);
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
      if (first) {
        await setup();
        loadQuestions();
        loadRelated();
      }
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
        if (mode === "read") {
          // a new chapter: reading carries on from its first sentence once the sentences arrive
          pendingTime = null;
          speed.setMessage(c?.ready ? null : "This chapter is still being generated.");
          speed.setChapter([], 0, c?.title ?? "");
        }
        if (follow && c?.start_page && pdf) pdf.showPage(c.start_page);
      },
      onSentencesLoaded: () => {
        renderText();
        if (mode === "read") syncSpeed();
      },
      onSentence: (s, i) => {
        if (mode === "read") return; // the reading stage follows its own position
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
    const settingsNow = await api.settings().catch(() => null);
    if (settingsNow) setWpm(settingsNow.values.reading_wpm, false);
    const start = pb && doc.chapters.some((c) => c.idx === pb.chapter_idx) ? pb : { chapter_idx: 0, position_s: 0 };
    await player.load(start.chapter_idx, start.position_s, false);
    nowReading.replaceChildren(h("p", { class: "quiet" }, pb && pb.position_s > 0 ? "Press play to resume where you left off." : "Press play to listen or read."));
    if (mode === "read" && !enterRead(start.position_s, true)) mode = "listen";
    showMode();
  }

  const onKey = (e: KeyboardEvent) => {
    const t = e.target as HTMLElement;
    if (t.closest("input, select, textarea, [contenteditable]") || e.metaKey || e.ctrlKey || e.altKey) return;
    if (mode === "read") {
      if (e.key === " " || e.key === "k") {
        e.preventDefault();
        speed.toggle();
      } else if (e.key === "ArrowLeft" || e.key === "j") {
        e.preventDefault();
        stepSentence(-1);
      } else if (e.key === "ArrowRight" || e.key === "l") {
        e.preventDefault();
        stepSentence(1);
      }
      return;
    }
    if (e.key === " " || e.key === "k") {
      e.preventDefault();
      player?.toggle();
    } else if (e.key === "ArrowLeft" || e.key === "j") player?.skip(-15);
    else if (e.key === "ArrowRight" || e.key === "l") player?.skip(15);
  };
  document.addEventListener("keydown", onKey);

  // ---------------------------------------------------------------- column widths (wide layout)
  const CHAPTERS_KEY = "lv.chaptersWidth";
  const QA_KEY = "lv.qaWidth";
  const CHAPTERS_DEFAULT = 320;
  const QA_DEFAULT = 340;
  const CHAPTERS_MIN = 240;
  const QA_MIN = 260;
  const DOC_MIN = 360;
  const SPLITTER_PX = 6;
  let chaptersW = storedNumber(CHAPTERS_KEY) ?? CHAPTERS_DEFAULT;
  let qaW = storedNumber(QA_KEY) ?? QA_DEFAULT;

  /** The width a column can take while the document keeps DOC_MIN and the other column keeps `other`. */
  function fit(px: number, min: number, other: number): number {
    // not laid out yet (set at creation, before insertion): no room limit to apply
    const room = body.clientWidth ? body.clientWidth - DOC_MIN - 2 * SPLITTER_PX - other : Infinity;
    return Math.round(Math.min(Math.max(px, min), Math.max(min, room)));
  }

  function setChaptersWidth(px: number, save = true): void {
    chaptersW = fit(px, CHAPTERS_MIN, qaW);
    body.style.setProperty("--chapters-w", `${chaptersW}px`);
    if (save) storeNumber(CHAPTERS_KEY, chaptersW);
  }

  function setQaWidth(px: number, save = true): void {
    qaW = fit(px, QA_MIN, chaptersW);
    body.style.setProperty("--qa-w", `${qaW}px`);
    if (save) storeNumber(QA_KEY, qaW);
  }

  /** Pointer drag on a splitter; the callback gets the pointer position. */
  function dragging(handle: HTMLElement, onMove: (e: PointerEvent) => void): void {
    handle.addEventListener("pointerdown", (e) => {
      if (e.button !== 0) return;
      e.preventDefault();
      handle.setPointerCapture(e.pointerId);
      handle.classList.add("dragging");
      document.body.classList.add("is-dragging");
      const move = (ev: PointerEvent) => onMove(ev);
      const up = () => {
        handle.classList.remove("dragging");
        document.body.classList.remove("is-dragging");
        handle.removeEventListener("pointermove", move);
        handle.removeEventListener("pointerup", up);
        handle.removeEventListener("pointercancel", up);
      };
      handle.addEventListener("pointermove", move);
      handle.addEventListener("pointerup", up);
      handle.addEventListener("pointercancel", up);
    });
  }

  dragging(splitA, (e) => setChaptersWidth(chaptersPane.getBoundingClientRect().right - e.clientX));
  dragging(splitB, (e) => setQaWidth(body.getBoundingClientRect().right - e.clientX));
  // keyboard: arrows move a splitter, Home or double-click resets it
  splitA.addEventListener("keydown", (e) => {
    if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
      e.preventDefault();
      setChaptersWidth(chaptersW + (e.key === "ArrowLeft" ? 24 : -24));
    } else if (e.key === "Home") setChaptersWidth(CHAPTERS_DEFAULT);
  });
  splitB.addEventListener("keydown", (e) => {
    if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
      e.preventDefault();
      setQaWidth(qaW + (e.key === "ArrowLeft" ? 24 : -24));
    } else if (e.key === "Home") setQaWidth(QA_DEFAULT);
  });
  splitA.addEventListener("dblclick", () => setChaptersWidth(CHAPTERS_DEFAULT));
  splitB.addEventListener("dblclick", () => setQaWidth(QA_DEFAULT));
  setChaptersWidth(chaptersW, false);
  setQaWidth(qaW, false);
  // The layout follows the width of the reader. The columns only show when it is wide, so only then do they refit.
  const isWide = () => main.clientWidth >= WIDE_MIN;
  const resizes = new ResizeObserver(() => {
    if (!isWide()) return;
    setChaptersWidth(chaptersW, false);
    setQaWidth(qaW, false);
  });
  resizes.observe(main);

  // Background timers are throttled: catch up on chapter readiness as soon as the page is shown again.
  const onVisible = () => {
    if (document.visibilityState === "visible" && doc && doc.status !== "ready" && doc.status !== "failed") refresh();
  };
  document.addEventListener("visibilitychange", onVisible);

  setHeaderOpen(headerOpen);
  const el = h("section", { class: "reader" }, head, main, bar);
  refresh(true);
  return {
    el,
    title: "Reader",
    destroy: () => {
      destroyed = true;
      resizes.disconnect();
      window.clearTimeout(poll);
      window.clearTimeout(qaTimer);
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("visibilitychange", onVisible);
      speed.destroy();
      player?.destroy();
      pdf?.destroy();
    },
  };
}

function splitter(label: string): HTMLElement {
  return h("div", {
    class: "splitter v",
    role: "separator",
    "aria-orientation": "vertical",
    "aria-label": label,
    title: "Drag to resize. Double-click to reset.",
    tabindex: 0,
  });
}

/** Shows how far a range input is along, for the track's filled part (style.css). */
function paint(input: HTMLInputElement): void {
  const min = Number(input.min);
  const span = Number(input.max) - min;
  const pct = span > 0 ? ((Number(input.value) - min) / span) * 100 : 0;
  input.style.setProperty("--fill", `${Math.min(100, Math.max(0, pct))}%`);
}

function storedNumber(key: string): number | null {
  try {
    const v = Number(localStorage.getItem(key));
    return v > 0 ? v : null;
  } catch {
    return null;
  }
}

function storedValue(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function storeValue(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* ignore */
  }
}

function storeNumber(key: string, value: number): void {
  try {
    localStorage.setItem(key, String(value));
  } catch {
    /* ignore */
  }
}
