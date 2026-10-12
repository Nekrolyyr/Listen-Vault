// Pure logic for speed reading: pacing, the start-up ramp, word splitting, the thumb gesture and the
// sentence lookup. No DOM here, so it can be tested directly (see test/speedread.test.ts).

export const WPM_MIN = 150;
export const WPM_MAX = 800;
export const WPM_DEFAULT = 300;

/** The reading speed starts at this share of the target and reaches the full target after RAMP_MS of reading. */
export const RAMP_START = 0.5;
export const RAMP_MS = 15_000;
/** How far the thumb must move up (in px) before a hold becomes a lock. */
export const LOCK_DISTANCE = 60;

export function clampWpm(value: number): number {
  return Math.min(WPM_MAX, Math.max(WPM_MIN, Math.round(value)));
}

/** Speed factor for the time since reading started: eases from RAMP_START to 1 over RAMP_MS. */
export function rampFactor(elapsedMs: number): number {
  const t = Math.min(1, Math.max(0, elapsedMs / RAMP_MS));
  const eased = 1 - (1 - t) * (1 - t); // ease-out, so most of the change comes early
  return RAMP_START + (1 - RAMP_START) * eased;
}

/** Longer pauses after the punctuation that ends a clause or a sentence. */
export function pauseMultiplier(word: string): number {
  if (/[.!?…][\])"'”’]*$/.test(word)) return 1.5;
  if (/[,;:—][\])"'”’]*$/.test(word)) return 1.2;
  return 1;
}

/** Milliseconds to show `word` at the target speed, after `elapsedMs` of reading in this session. */
export function wordDelayMs(word: string, wpm: number, elapsedMs: number): number {
  const perMs = (wpm * rampFactor(elapsedMs)) / 60_000;
  return pauseMultiplier(word) / perMs;
}

/** Words of a sentence, split on whitespace. */
export function splitWords(text: string): string[] {
  return text.split(/\s+/).filter((w) => w.length > 0);
}

/** Index of the last sentence that starts at or before `t` (seconds), or -1 before the first one. */
export function sentenceIndexAt(sentences: readonly { start: number }[], t: number): number {
  let lo = 0;
  let hi = sentences.length - 1;
  let found = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (sentences[mid].start <= t + 0.05) {
      found = mid;
      lo = mid + 1;
    } else hi = mid - 1;
  }
  return found;
}

export type HoldAction = "start" | "stop" | "lock" | "none";

/**
 * The thumb gesture. Press to read, lift to stop. Sliding up past LOCK_DISTANCE keeps reading after the
 * thumb is lifted; the next press stops it. The pointerup that ends the unlocking press is ignored.
 */
export class HoldMachine {
  private state: "idle" | "holding" | "locked" = "idle";
  private startY = 0;
  private swallowUp = false;

  get locked(): boolean {
    return this.state === "locked";
  }

  down(y: number): HoldAction {
    if (this.state === "locked") {
      this.state = "idle";
      this.swallowUp = true;
      return "stop";
    }
    this.state = "holding";
    this.startY = y;
    return "start";
  }

  move(y: number): HoldAction {
    if (this.state !== "holding") return "none";
    if (this.startY - y >= LOCK_DISTANCE) {
      this.state = "locked";
      return "lock";
    }
    return "none";
  }

  up(): HoldAction {
    if (this.swallowUp) {
      this.swallowUp = false;
      return "none";
    }
    if (this.state !== "holding") return "none";
    this.state = "idle";
    return "stop";
  }

  /** The browser took the pointer (scroll, system gesture). Treated as a lift. */
  cancel(): HoldAction {
    return this.up();
  }

  reset(): void {
    this.state = "idle";
    this.swallowUp = false;
  }
}
