// Speed reading stage: one word at a time, driven by the thumb pad. The timing and the gesture rules live in
// speedread.ts; this file puts them on the screen and keeps the reader's position in step.
import type { Sentence } from "./api";
import { h } from "./dom";
import { HoldAction, HoldMachine, WPM_DEFAULT, clampWpm, splitWords, wordDelayMs } from "./speedread";

export interface SpeedHooks {
  /** The reading moved to another sentence: keep the saved position in step. */
  onIndex(index: number): void;
  /** Reading stopped for any reason: lift, a tap while locked, the screen turning off, the end of the paper. */
  onStop(): void;
  /** The chapter ran out. Return true when the next chapter has started loading; false stops the reading. */
  nextChapter(): boolean;
}

export class SpeedView {
  readonly el: HTMLElement;
  private sentences: Sentence[] = [];
  private index = 0;
  private wordIdx = 0;
  private title = "";
  private wpm = WPM_DEFAULT;
  private playing = false;
  private startedAt = 0;
  private timer = 0;
  private wake: WakeLockSentinel | null = null;
  private hold = new HoldMachine();
  private message: string | null = null;

  private readonly chapterEl = h("span", { class: "speed-chapter" });
  private readonly countEl = h("span", { class: "speed-count" });
  private readonly wordEl = h("div", { class: "speed-word", "aria-live": "off" });
  private readonly messageEl = h("p", { class: "speed-message", hidden: true });
  private readonly padHint = h("span", { class: "speed-hint" }, "Hold to read");
  private readonly padEl = h(
    "div",
    { class: "speed-pad", role: "button", tabindex: 0, "aria-label": "Hold to read. Slide up to keep reading. Lift to stop." },
    this.padHint,
  );

  constructor(private hooks: SpeedHooks) {
    this.el = h(
      "section",
      { class: "speed-stage" },
      h("div", { class: "speed-meta" }, this.chapterEl, this.countEl),
      h("div", { class: "speed-word-wrap" }, this.wordEl, this.messageEl),
      this.padEl,
    );
    this.padEl.addEventListener("pointerdown", (e) => {
      if (e.pointerType === "mouse" && e.button !== 0) return;
      this.padEl.setPointerCapture(e.pointerId);
      this.apply(this.hold.down(e.clientY));
    });
    this.padEl.addEventListener("pointermove", (e) => this.apply(this.hold.move(e.clientY)));
    this.padEl.addEventListener("pointerup", () => this.apply(this.hold.up()));
    this.padEl.addEventListener("pointercancel", () => this.apply(this.hold.cancel()));
    this.padEl.addEventListener("contextmenu", (e) => e.preventDefault());
    document.addEventListener("visibilitychange", this.onVisible);
    this.render();
  }

  /** The sentence being read, and how many there are in the chapter. */
  get position(): { index: number; count: number } {
    return { index: this.index, count: this.sentences.length };
  }

  setWpm(wpm: number): void {
    this.wpm = clampWpm(wpm);
  }

  /** A chapter's sentences arrived (or the chapter changed: pass an empty list while it loads). */
  setChapter(sentences: Sentence[], index: number, title: string): void {
    window.clearTimeout(this.timer);
    this.timer = 0;
    this.sentences = sentences;
    this.index = Math.min(Math.max(0, index), Math.max(0, sentences.length - 1));
    this.wordIdx = 0;
    this.title = title;
    this.render();
    if (this.playing && sentences.length) this.tick();
  }

  /** Jump to a sentence and keep the saved position there. */
  seek(index: number): void {
    this.preview(index);
    this.hooks.onIndex(this.index);
  }

  /** Show a sentence without changing the saved position (used while the progress slider is dragged). */
  preview(index: number): void {
    if (!this.sentences.length) return;
    this.index = Math.min(Math.max(0, index), this.sentences.length - 1);
    this.wordIdx = 0;
    this.render();
  }

  toggle(): void {
    if (this.playing) this.stop();
    else this.start();
  }

  start(): void {
    if (this.playing) return;
    this.playing = true;
    this.startedAt = performance.now(); // the start-up ramp begins again on every start
    this.padEl.classList.add("active");
    void this.acquireWake();
    if (this.sentences.length) this.tick();
    this.renderPad();
  }

  stop(): void {
    const was = this.playing;
    window.clearTimeout(this.timer);
    this.timer = 0;
    this.playing = false;
    this.hold.reset();
    this.padEl.classList.remove("active");
    void this.releaseWake();
    this.renderPad();
    if (was) this.hooks.onStop();
  }

  destroy(): void {
    this.stop();
    document.removeEventListener("visibilitychange", this.onVisible);
  }

  private apply(action: HoldAction): void {
    if (action === "start") this.start();
    else if (action === "stop") this.stop();
    else if (action === "lock") {
      navigator.vibrate?.(12);
      this.renderPad();
    }
  }

  private words(): string[] {
    return splitWords(this.sentences[this.index]?.text ?? "");
  }

  /** Shows the next word and schedules the one after it. */
  private tick(): void {
    this.timer = 0;
    if (!this.playing || !this.sentences.length) return;
    while (this.wordIdx >= this.words().length) {
      if (this.index + 1 >= this.sentences.length) {
        this.finish();
        return;
      }
      this.index += 1;
      this.wordIdx = 0;
      this.hooks.onIndex(this.index);
      this.renderMeta();
    }
    const word = this.words()[this.wordIdx];
    this.wordIdx += 1;
    this.wordEl.textContent = word;
    const delay = wordDelayMs(word, this.wpm, performance.now() - this.startedAt);
    this.timer = window.setTimeout(() => this.tick(), delay);
  }

  private finish(): void {
    // The chapter ran out. The reader either loads the next one (the view gets its sentences through setChapter)
    // or there is nothing left, which ends the reading.
    if (!this.hooks.nextChapter()) this.stop();
  }

  private readonly onVisible = () => {
    // The screen turned off or the tab went to the background: timers are throttled there, so stop and save.
    if (document.visibilityState === "hidden") this.stop();
  };

  private async acquireWake(): Promise<void> {
    try {
      const wake = (await navigator.wakeLock?.request("screen")) ?? null;
      if (this.playing) this.wake = wake;
      else await wake?.release(); // stopped while the request was pending
    } catch {
      /* not supported or refused: reading still works while the screen stays on */
    }
  }

  private async releaseWake(): Promise<void> {
    const wake = this.wake;
    this.wake = null;
    try {
      await wake?.release();
    } catch {
      /* already released */
    }
  }

  private render(): void {
    this.renderMeta();
    this.renderPad();
    const empty = !this.sentences.length;
    this.wordEl.hidden = empty;
    this.wordEl.textContent = empty ? "" : (this.words()[this.wordIdx] ?? this.words()[0] ?? "");
    // with nothing to read, the message says why; otherwise the word is all there is
    this.messageEl.hidden = !empty;
    this.messageEl.textContent = this.message ?? "Loading…";
  }

  /** Shown instead of the word when there is nothing to read yet, for example a chapter still being generated. */
  setMessage(text: string | null): void {
    this.message = text;
    this.render();
  }

  private renderMeta(): void {
    this.chapterEl.textContent = this.title;
    this.countEl.textContent = this.sentences.length ? `Sentence ${this.index + 1} of ${this.sentences.length}` : "";
  }

  private renderPad(): void {
    const locked = this.hold.locked;
    this.padEl.classList.toggle("locked", locked);
    this.padHint.textContent = locked ? "Locked · tap to stop" : this.playing ? "Lift to stop · slide up to lock" : "Hold to read";
    this.padEl.setAttribute("aria-pressed", String(this.playing));
  }
}
