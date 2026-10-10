"""Build one chapter's audio: synthesize sentence by sentence, join with pauses, encode to AAC (.m4a).

AAC in MP4 plays in every current browser, including Safari on iOS (Ogg Opus does not everywhere).
Sentence start/end times are exact because they come from the sample counts.
"""
from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .tts import Engine

PAUSES = {"sentence": 0.25, "paragraph": 0.6, "heading": 0.9}
BITRATE = "64k"


@dataclass
class Segment:
    display: str          # text shown in the reader
    spoken: str           # normalized text sent to TTS
    page: int | None
    pause: str = "sentence"


@dataclass
class Timed:
    text: str
    page: int | None
    start_s: float
    end_s: float


def render(engine: Engine, segments: list[Segment], out: Path,
           progress: Callable[[], bool] = lambda: True) -> tuple[float, list[Timed]]:
    """Synthesize segments into `out`. Returns (duration, timings). Stops early if progress() is False."""
    rate = engine.sample_rate
    pieces: list[np.ndarray] = []
    timed: list[Timed] = []
    cursor = 0
    for seg in segments:
        if not progress():
            raise InterruptedError("job cancelled")
        audio = engine.synth(seg.spoken) if seg.spoken.strip() else np.zeros(0, np.float32)
        start = cursor
        pieces.append(audio)
        cursor += len(audio)
        timed.append(Timed(seg.display, seg.page, start / rate, cursor / rate))
        silence = np.zeros(int(PAUSES[seg.pause] * rate), np.float32)
        pieces.append(silence)
        cursor += len(silence)
    samples = np.concatenate(pieces) if pieces else np.zeros(rate, np.float32)
    encode(samples, rate, out)
    return cursor / rate, timed


def encode(samples: np.ndarray, rate: int, out: Path) -> None:
    tmp = out.with_suffix(".tmp.m4a")
    pcm = np.clip(samples, -1.0, 1.0).astype("<f4").tobytes()
    cmd = [
        "ffmpeg", "-loglevel", "error", "-y", "-f", "f32le", "-ar", str(rate), "-ac", "1", "-i", "pipe:0",
        "-c:a", "aac", "-b:a", BITRATE, "-movflags", "+faststart", str(tmp),
    ]
    proc = subprocess.run(cmd, input=pcm, capture_output=True, timeout=1800)
    if proc.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg failed: {proc.stderr.decode(errors='replace')[-400:]}")
    tmp.replace(out)
