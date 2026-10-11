// Chapter-by-chapter audio player with sentence tracking, resume and lock-screen controls.
import { api, type Chapter, type DocDetail, type Sentence } from "./api";

export interface PlayerEvents {
  onChapter(idx: number): void;
  onSentencesLoaded(idx: number): void; // timings for the current chapter arrived
  onSentence(sentence: Sentence | null, idx: number): void;
  onState(): void;
  onWaiting(idx: number | null): void; // next chapter not generated yet
}

const SAVE_EVERY_MS = 10_000;

/** One second of 8-bit silence as a WAV file, looped while a chapter is still being generated. */
let silenceUrl: string | null = null;
function silence(): string {
  if (!silenceUrl) {
    const rate = 8000;
    const buf = new ArrayBuffer(44 + rate);
    const v = new DataView(buf);
    const ascii = (offset: number, s: string) => [...s].forEach((c, i) => v.setUint8(offset + i, c.charCodeAt(0)));
    ascii(0, "RIFF");
    v.setUint32(4, 36 + rate, true);
    ascii(8, "WAVE");
    ascii(12, "fmt ");
    v.setUint32(16, 16, true);
    v.setUint16(20, 1, true); // PCM
    v.setUint16(22, 1, true); // mono
    v.setUint32(24, rate, true);
    v.setUint32(28, rate, true);
    v.setUint16(32, 1, true);
    v.setUint16(34, 8, true);
    ascii(36, "data");
    v.setUint32(40, rate, true);
    new Uint8Array(buf, 44).fill(128); // 8-bit unsigned silence
    silenceUrl = URL.createObjectURL(new Blob([buf], { type: "audio/wav" }));
  }
  return silenceUrl;
}

export class Player {
  readonly audio = new Audio();
  chapterIdx = -1;
  sentences: Sentence[] = [];
  private sentenceIdx = -1;
  // The chapter whose audio is loaded in `audio`. chapterIdx can point ahead of it while a chapter is generated.
  private srcIdx = -1;
  private lastSave = 0;
  private waitingFor: number | null = null;
  // The listener's intent. audio.paused also flips at the end of a chapter and on system pauses (audio focus,
  // the media notification), so auto-advance and the play button follow this instead.
  private wantPlaying = false;
  private keepAlive: HTMLAudioElement | null = null;
  rate = 1;

  constructor(public doc: DocDetail, private events: PlayerEvents) {
    this.audio.preload = "auto";
    this.audio.addEventListener("timeupdate", () => this.tick());
    this.audio.addEventListener("play", () => {
      this.syncPlaybackState();
      this.events.onState();
    });
    this.audio.addEventListener("pause", () => {
      this.syncPlaybackState();
      this.events.onState();
      // at the end of a chapter there is nothing to resume from, so keep the last real position
      if (!this.audio.ended && this.waitingFor === null) this.save();
    });
    this.audio.addEventListener("ended", () => this.next(true));
    this.audio.addEventListener("loadedmetadata", () => this.events.onState());
    document.addEventListener("visibilitychange", this.onHide);
    window.addEventListener("pagehide", this.onHide);
    this.mediaSession();
  }

  get playing(): boolean {
    return this.waitingFor !== null ? this.wantPlaying : !this.audio.paused;
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
      this.load(idx, 0, this.wantPlaying);
    }
  }

  async load(idx: number, position = 0, autoplay = false): Promise<void> {
    const chapter = this.doc.chapters.find((c) => c.idx === idx);
    if (!chapter) return;
    if (autoplay) this.wantPlaying = true;
    if (!chapter.ready) {
      this.waitingFor = idx;
      this.audio.pause(); // the previous chapter must not keep playing under the new title
      this.chapterIdx = idx;
      this.sentenceIdx = -1;
      this.sentences = [];
      this.events.onChapter(idx);
      this.events.onWaiting(idx);
      if (this.wantPlaying) this.keepAliveStart();
      else this.keepAliveStop();
      // the audio was already paused at the end of the previous chapter, so no pause event will update the controls
      this.syncPlaybackState();
      this.events.onState();
      this.updateMetadata();
      return;
    }
    this.waitingFor = null;
    this.events.onWaiting(null);
    this.keepAliveStop();
    const changed = idx !== this.chapterIdx;
    this.chapterIdx = idx;
    if (idx !== this.srcIdx || !this.audio.src) {
      this.srcIdx = idx;
      this.sentenceIdx = -1;
      this.sentences = [];
      this.audio.src = api.audioUrl(this.doc.id, idx);
      this.audio.playbackRate = this.rate;
      api.sentences(this.doc.id, idx).then((s) => {
        if (this.chapterIdx === idx && this.srcIdx === idx) {
          this.sentences = s;
          this.events.onSentencesLoaded(idx);
          this.tick(true);
        }
      });
    }
    if (changed) this.events.onChapter(idx);
    if (position > 0) {
      const seek = () => (this.audio.currentTime = position);
      if (this.audio.readyState >= 1) seek();
      else this.audio.addEventListener("loadedmetadata", seek, { once: true });
    }
    if (autoplay) await this.play();
    this.syncPlaybackState();
    this.events.onState();
    this.updateMetadata();
  }

  async play(): Promise<void> {
    if (this.chapterIdx < 0) {
      await this.load(this.doc.chapters[0]?.idx ?? 0, 0, true);
      return;
    }
    this.wantPlaying = true;
    if (this.waitingFor !== null) {
      this.keepAliveStart(); // the chapter starts when it is ready
      this.events.onState();
      return;
    }
    try {
      await this.audio.play();
    } catch (err) {
      // blocked (for example a resume in the background without a tap): show paused, so the next tap plays
      if ((err as Error).name === "NotAllowedError") this.wantPlaying = false;
      this.syncPlaybackState();
      this.events.onState();
    }
  }

  pause(): void {
    this.wantPlaying = false;
    this.keepAliveStop();
    this.audio.pause();
    // no pause event if the audio was already paused (e.g. while a chapter is generating)
    this.syncPlaybackState();
    this.events.onState();
  }

  toggle(): void {
    if (this.playing) this.pause();
    else void this.play();
  }

  skip(seconds: number): void {
    if (this.waitingFor !== null) return;
    const t = this.audio.currentTime + seconds;
    if (t < 0 && seconds < 0 && this.audio.currentTime < 2) {
      this.prev();
      return;
    }
    this.audio.currentTime = Math.max(0, Math.min(t, (this.audio.duration || t) - 0.1));
  }

  seekTo(seconds: number): void {
    if (this.waitingFor !== null) return;
    this.audio.currentTime = seconds;
  }

  seekToSentence(s: Sentence): void {
    this.audio.currentTime = s.start + 0.01;
    void this.play();
  }

  next(autoplay = this.wantPlaying): void {
    const later = this.doc.chapters.filter((c) => c.idx > this.chapterIdx).sort((a, b) => a.idx - b.idx);
    if (later.length) {
      this.load(later[0].idx, 0, autoplay);
    } else {
      this.wantPlaying = false;
      this.save();
    }
  }

  prev(): void {
    const earlier = this.doc.chapters.filter((c) => c.idx < this.chapterIdx).sort((a, b) => b.idx - a.idx);
    if (this.audio.currentTime > 3 || !earlier.length) this.audio.currentTime = 0;
    else this.load(earlier[0].idx, 0, this.wantPlaying);
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
    if (this.chapterIdx < 0 || this.waitingFor !== null) return;
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

  /** Keeps the page audible while it waits for a chapter. A tab that is playing audio is not throttled or frozen
   *  in the background, so the readiness poll keeps running with the screen off. */
  private keepAliveStart(): void {
    if (!this.keepAlive) {
      this.keepAlive = new Audio(silence());
      this.keepAlive.loop = true;
    }
    this.keepAlive.play().catch(() => {
      /* blocked: the poll still runs while the page is in the foreground */
    });
  }

  private keepAliveStop(): void {
    this.keepAlive?.pause();
  }

  private mediaSession(): void {
    if (!("mediaSession" in navigator)) return;
    const ms = navigator.mediaSession;
    ms.setActionHandler("play", () => this.play());
    ms.setActionHandler("pause", () => this.pause());
    ms.setActionHandler("seekbackward", () => this.skip(-15));
    ms.setActionHandler("seekforward", () => this.skip(15));
    ms.setActionHandler("previoustrack", () => this.prev());
    ms.setActionHandler("nexttrack", () => this.next());
    ms.setActionHandler("seekto", (d) => d.seekTime !== undefined && this.seekTo(d.seekTime));
  }

  /** The notification's play/pause button follows this; Android hides the controls when it is unset. */
  private syncPlaybackState(): void {
    if ("mediaSession" in navigator) navigator.mediaSession.playbackState = this.playing ? "playing" : "paused";
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
    this.keepAliveStop();
    this.keepAlive = null;
    this.audio.removeAttribute("src");
    this.audio.load();
    document.removeEventListener("visibilitychange", this.onHide);
    window.removeEventListener("pagehide", this.onHide);
    if ("mediaSession" in navigator) navigator.mediaSession.metadata = null;
  }
}
