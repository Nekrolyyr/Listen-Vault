"""Text-to-speech engines: Kokoro-82M (ONNX, default) and Piper (fallback). Both run on CPU."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Protocol

import numpy as np

log = logging.getLogger(__name__)

KOKORO_MODEL = "kokoro/kokoro-v1.0.onnx"
KOKORO_VOICES = "kokoro/voices-v1.0.bin"
PIPER_VOICE = "piper/en_US-lessac-medium.onnx"
DEFAULT_VOICE = "af_heart"


class Engine(Protocol):
    name: str
    voice: str
    sample_rate: int

    def synth(self, text: str) -> np.ndarray: ...


class KokoroEngine:
    name = "kokoro"
    sample_rate = 24_000

    def __init__(self, models_dir: Path, voice: str, speed: float = 1.0, threads: int = 8):
        import onnxruntime as ort
        from kokoro_onnx import Kokoro

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = threads
        opts.inter_op_num_threads = 1
        session = ort.InferenceSession(str(models_dir / KOKORO_MODEL), opts, providers=["CPUExecutionProvider"])
        self.kokoro = Kokoro.from_session(session, str(models_dir / KOKORO_VOICES))
        voices = set(self.kokoro.get_voices())
        if voice not in voices:
            log.warning("unknown Kokoro voice %r, using %s", voice, DEFAULT_VOICE)
            voice = DEFAULT_VOICE
        self.voice = voice
        self.lang = "en-gb" if voice.startswith("b") else "en-us"
        self.speed = speed

    def synth(self, text: str) -> np.ndarray:
        try:
            samples, rate = self.kokoro.create(text, voice=self.voice, speed=self.speed, lang=self.lang)
        except ValueError as e:  # "Nothing to synthesize": text without any pronounceable phonemes
            log.warning("skipping unpronounceable text %r: %s", text[:80], e)
            return np.zeros(0, np.float32)
        assert rate == self.sample_rate
        return samples.astype(np.float32)


class PiperEngine:
    name = "piper"
    voice = "en_US-lessac-medium"

    def __init__(self, models_dir: Path, speed: float = 1.0):
        from piper import PiperVoice, SynthesisConfig

        self.piper = PiperVoice.load(str(models_dir / PIPER_VOICE))
        self.sample_rate = self.piper.config.sample_rate
        self.config = SynthesisConfig(length_scale=1.0 / speed)

    def synth(self, text: str) -> np.ndarray:
        chunks = [c.audio_float_array for c in self.piper.synthesize(text, syn_config=self.config)]
        return np.concatenate(chunks).astype(np.float32) if chunks else np.zeros(0, np.float32)


def kokoro_voices(models_dir: Path) -> list[str]:
    """Voice ids from the voices file without loading the model (english voices only)."""
    path = models_dir / KOKORO_VOICES
    if not path.is_file():
        return []
    with np.load(path) as data:
        return sorted(v for v in data.files if v[:2] in ("af", "am", "bf", "bm"))


def make_engine(engine: str, models_dir: Path, voice: str, speed: float, threads: int) -> Engine:
    if engine == "piper":
        return PiperEngine(models_dir, speed)
    return KokoroEngine(models_dir, voice, speed, threads)
