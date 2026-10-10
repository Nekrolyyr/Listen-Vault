// Chapter-by-chapter audio player with sentence tracking, resume and lock-screen controls.
import { api, type Chapter, type DocDetail, type Sentence } from "./api";

export interface PlayerEvents {
  onChapter(idx: number): void;
  onSentence(sentence: Sentence | null, idx: number): void;
  onState(): void;
  onWaiting(idx: number | null): void; // next chapter not generated yet
}

const SAVE_EVERY_MS = 10_000;

export class Player {
  readonly audio = new Audio();
  chapterIdx = -1;
  sentences: Sentence[] = [];
  private sentenceIdx = -1;
  private lastSave = 0;
  private waitingFor: number | null = null;
  rate = 1;

  constructor(public doc: DocDetail, private events: PlayerEvents) {
    this.audio.preload = "auto";
    this.audio.addEventListener("timeupdate", () => this.tick());
    this.audio.addEventListener("play", () => this.events.onState());
    this.audio.addEventListener("pause", () => {
      this.events.onState();
      this.save();
    });
    this.audio.addEventListener("ended", () => this.next(true));
    this.audio.addEventListener("loadedmetadata", () => this.events.onState());
    document.addEventListener("visibilitychange", this.onHide);
    window.addEventListener("pagehide", this.onHide);
    this.mediaSession();
  }

  get playing(): boolean {
    return !this.audio.paused;
  }

  get chapter(): Chapter | undefined {
    return this.doc.chapters.find((c) => c.idx === this.chapterIdx);
  }

  /** Called when the document refreshes (chapters becoming ready while we listen). */
  update(doc: DocDetail): void {
    this.doc = doc;
    if (this.waitingFor !== null && doc.chapters.find((c) => c.idx === this.waitingFor)?.ready) {
      const idx = this.waitingFor;
      this.waitingFor = null;
      this.events.onWaiting(null);
      this.load(idx, 0, true);
    }
  }

  async load(idx: number, position = 0, autoplay = false): Promise<void> {
    const chapter = this.doc.chapters.find((c) => c.idx === idx);
    if (!chapter) return;
    if (!chapter.ready) {
      this.waitingFor = idx;
      this.chapterIdx = idx;
      this.events.onChapter(idx);
      this.events.onWaiting(idx);
      return;
    }
    this.waitingFor = null;
    this.events.onWaiting(null);
    if (idx !== this.chapterIdx || !this.audio.src) {
      this.chapterIdx = idx;
      this.sentenceIdx = -1;
      this.sentences = [];
      this.audio.src = api.audioUrl(this.doc.id, idx);
      this.audio.playbackRate = this.rate;
      this.events.onChapter(idx);
      api.sentences(this.doc.id, idx).then((s) => {
        if (this.chapterIdx === idx) {
          this.sentences = s;
          this.tick(true);
        }
      });
    }
    if (position > 0) {
      const seek = () => (this.audio.currentTime = position);
      if (this.audio.readyState >= 1) seek();
      else this.audio.addEventListener("loadedmetadata", seek, { once: true });
    }
    if (autoplay) await this.play();
    this.updateMetadata();
  }

  async play(): Promise<void> {
    if (this.chapterIdx < 0) {
      await this.load(this.doc.chapters[0]?.idx ?? 0, 0, true);
      return;
    }
    try {
      await this.audio.play();
    } catch {
      /* autoplay blocked: the user presses play */
    }
  }

  toggle(): void {
    if (this.playing) this.audio.pause();
    else this.play();
  }

  skip(seconds: number): void {
    const t = this.audio.currentTime + seconds;
    if (t < 0 && seconds < 0 && this.audio.currentTime < 2) {
      this.prev();
      return;
    }
    this.audio.currentTime = Math.max(0, Math.min(t, (this.audio.duration || t) - 0.1));
  }

  seekTo(seconds: number): void {
    this.audio.currentTime = seconds;
  }

  seekToSentence(s: Sentence): void {
    this.audio.currentTime = s.start + 0.01;
    this.play();
  }

  next(autoplay = this.playing): void {
    const later = this.doc.chapters.filter((c) => c.idx > this.chapterIdx).sort((a, b) => a.idx - b.idx);
    if (later.length) this.load(later[0].idx, 0, autoplay);
    else this.save();
  }

  prev(): void {
    const earlier = this.doc.chapters.filter((c) => c.idx < this.chapterIdx).sort((a, b) => b.idx - a.idx);
    if (this.audio.currentTime > 3 || !earlier.length) this.audio.currentTime = 0;
    else this.load(earlier[0].idx, 0, this.playing);
  }

  setRate(rate: number): void {
    this.rate = rate;
    this.audio.playbackRate = rate;
    this.save();
  }

  private tick(force = false): void {
    const t = this.audio.currentTime;
    const list = this.sentences;
    let i = this.sentenceIdx;
    if (force || i < 0 || i >= list.length || t < list[i].start || (i + 1 < list.length && t >= list[i + 1].start)) {
      // binary search for the last sentence starting at or before t
      let lo = 0;
      let hi = list.length - 1;
      i = -1;
      while (lo <= hi) {
        const mid = (lo + hi) >> 1;
        if (list[mid].start <= t + 0.05) {
          i = mid;
          lo = mid + 1;
        } else hi = mid - 1;
      }
      if (i !== this.sentenceIdx || force) {
        this.sentenceIdx = i;
        this.events.onSentence(i >= 0 ? list[i] : null, i);
      }
    }
    this.events.onState();
    if (this.playing && Date.now() - this.lastSave > SAVE_EVERY_MS) this.save();
    if ("setPositionState" in navigator.mediaSession && this.audio.duration) {
      try {
        navigator.mediaSession.setPositionState({ duration: this.audio.duration, position: t, playbackRate: this.rate });
      } catch {
        /* ignore */
      }
    }
  }

  save(keepalive = false): void {
    if (this.chapterIdx < 0) return;
    this.lastSave = Date.now();
    api
      .savePlayback(this.doc.id, { chapter_idx: this.chapterIdx, position_s: this.audio.currentTime || 0, rate: this.rate }, keepalive)
      .catch(() => {
        /* best effort */
      });
  }

  private onHide = () => {
    if (document.visibilityState === "hidden") this.save(true);
  };

  private mediaSession(): void {
    if (!("mediaSession" in navigator)) return;
    const ms = navigator.mediaSession;
    ms.setActionHandler("play", () => this.play());
    ms.setActionHandler("pause", () => this.audio.pause());
    ms.setActionHandler("seekbackward", () => this.skip(-15));
    ms.setActionHandler("seekforward", () => this.skip(15));
    ms.setActionHandler("previoustrack", () => this.prev());
    ms.setActionHandler("nexttrack", () => this.next());
    ms.setActionHandler("seekto", (d) => d.seekTime !== undefined && this.seekTo(d.seekTime));
  }

  private updateMetadata(): void {
    if (!("mediaSession" in navigator)) return;
    navigator.mediaSession.metadata = new MediaMetadata({
      title: this.chapter?.title ?? this.doc.title,
      artist: this.doc.authors.slice(0, 3).join(", "),
      album: this.doc.title,
      artwork: [{ src: "/favicon.svg", sizes: "any", type: "image/svg+xml" }],
    });
  }

  destroy(): void {
    this.save(true);
    this.audio.pause();
    this.audio.removeAttribute("src");
    this.audio.load();
    document.removeEventListener("visibilitychange", this.onHide);
    window.removeEventListener("pagehide", this.onHide);
    if ("mediaSession" in navigator) navigator.mediaSession.metadata = null;
  }
}
