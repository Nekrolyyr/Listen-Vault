"""Client for the llama.cpp server running BLOOMZ-7b1 (raw completion; BLOOMZ has no chat template)."""
from __future__ import annotations

import logging
import time

import httpx

log = logging.getLogger(__name__)


class LlmError(Exception):
    pass


class LlmClient:
    def __init__(self, base_url: str, timeout_s: float = 900):
        self.base_url = base_url.rstrip("/")
        self.http = httpx.Client(timeout=timeout_s)

    def wait_ready(self, timeout_s: float = 300) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                if self.http.get(f"{self.base_url}/health", timeout=5).status_code == 200:
                    return True
            except httpx.HTTPError:
                pass
            time.sleep(5)
        return False

    def _post(self, path: str, payload: dict) -> dict:
        try:
            resp = self.http.post(f"{self.base_url}{path}", json=payload)
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, ValueError) as e:
            raise LlmError(f"LLM request {path} failed: {e}") from e

    def tokenize(self, text: str) -> list[int]:
        return self._post("/tokenize", {"content": text})["tokens"]

    def detokenize(self, tokens: list[int]) -> str:
        return self._post("/detokenize", {"tokens": tokens})["content"]

    def count(self, text: str) -> int:
        return len(self.tokenize(text))

    def truncate(self, text: str, max_tokens: int) -> str:
        tokens = self.tokenize(text)
        return text if len(tokens) <= max_tokens else self.detokenize(tokens[:max_tokens])

    def complete(self, prompt: str, n_predict: int = 160, temperature: float = 0.2) -> str:
        t0 = time.perf_counter()
        out = self._post(
            "/completion",
            {
                "prompt": prompt,
                "n_predict": n_predict,
                "temperature": temperature,
                "top_p": 0.9,
                "repeat_penalty": 1.15,
                "repeat_last_n": 128,
                "cache_prompt": False,
                "stop": ["</s>", "\n\n\n"],
            },
        )
        log.debug("llm %.1fs, %s prompt tokens", time.perf_counter() - t0, out.get("tokens_evaluated"))
        return (out.get("content") or "").strip()
