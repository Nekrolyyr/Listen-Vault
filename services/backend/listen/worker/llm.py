"""Client for the llama.cpp server.

Two prompt formats (LLM_FORMAT):
- "chatml" (default): Qwen3, a chat model. Prompts use the ChatML template with thinking switched off, so the answer
  starts at once. `chat` and `chat_json` build the template.
- "raw": BLOOMZ-7b1, a completion model with no template. `complete` and `complete_json` send the text as it is; `chat`
  and `chat_json` fall back to them, so the same feature code runs on both.
"""
from __future__ import annotations

import json
import logging
import re
import time

import httpx

log = logging.getLogger(__name__)

FORMATS = ("chatml", "raw")
CHATML_STOP = ["<|im_end|>", "<|endoftext|>"]
RAW_STOP = ["</s>", "\n\n\n"]

# Input budgets in tokens, per prompt format. The chatml budgets fit a 4,096-token window with the answer and the
# template, and keep the titles and figure summaries short: prompt processing on the GTX 1660 SUPER is about 15 ms per
# token, so a 2,500-token prompt costs about 40 s. The raw budgets fit BLOOMZ's 2,048.
BUDGETS = {
    "chatml": {"excerpt": 2500, "title": 1200, "body": 2000, "element": 1200, "context": 3000, "answer": 300},
    "raw": {"excerpt": 900, "title": 600, "body": 700, "element": 700, "context": 1000, "answer": 200},
}

_THINK = re.compile(r"<think>.*?</think>\s*|<think>.*\Z", re.S)


class LlmError(Exception):
    pass


def chatml(user: str, system: str | None = None) -> str:
    """A ChatML prompt for Qwen3 with thinking off: the empty think block tells the model to answer directly."""
    head = f"<|im_start|>system\n{system}<|im_end|>\n" if system else ""
    return f"{head}<|im_start|>user\n{user}<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"


def strip_thinking(text: str) -> str:
    """Remove reasoning blocks, closed or cut off, in case a model writes one anyway."""
    return _THINK.sub("", text).strip()


def budget(llm, name: str) -> int:
    """The input budget of one prompt for this client's format. Clients without a format are raw."""
    return BUDGETS[getattr(llm, "chat_format", "raw")][name]


class LlmClient:
    def __init__(self, base_url: str, timeout_s: float = 900, chat_format: str = "chatml"):
        if chat_format not in FORMATS:
            raise ValueError(f"unknown LLM format {chat_format!r}")
        self.base_url = base_url.rstrip("/")
        self.chat_format = chat_format
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

    def complete(self, prompt: str, n_predict: int = 160, temperature: float = 0.2, *,
                 stop: list[str] | None = None, cache_prompt: bool = False) -> str:
        """`cache_prompt` reuses the KV cache of a prompt that starts with the same text as the previous one."""
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
                "cache_prompt": cache_prompt,
                "stop": stop or RAW_STOP,
            },
        )
        log.debug("llm %.1fs, %s prompt tokens", time.perf_counter() - t0, out.get("tokens_evaluated"))
        return (out.get("content") or "").strip()

    def complete_json(self, prompt: str, schema: dict, n_predict: int = 300, *, cache_prompt: bool = False) -> dict:
        """Completion constrained by a JSON schema (llama.cpp grammar), so the output always parses."""
        out = self._post(
            "/completion",
            {
                "prompt": prompt,
                "n_predict": n_predict,
                "temperature": 0.2,
                "top_p": 0.9,
                "cache_prompt": cache_prompt,
                "stop": ["</s>"],
                "json_schema": schema,
            },
        )
        try:
            data = json.loads(out.get("content") or "")
        except ValueError as e:
            raise LlmError(f"LLM returned no valid JSON: {e}") from e
        if not isinstance(data, dict):
            raise LlmError("LLM returned JSON that is not an object")
        return data

    # ------------------------------------------------------------ chat (feature code uses these)
    def _prompt(self, user: str, system: str | None) -> str:
        if self.chat_format == "chatml":
            return chatml(user, system)
        return f"{system}\n\n{user}" if system else user

    def chat(self, user: str, *, system: str | None = None, n_predict: int = 160, temperature: float = 0.2,
             stop: list[str] | None = None, cache_prompt: bool = False, feature: str = "") -> str:
        """One answer to a user message, with the thinking part removed."""
        prompt = self._prompt(user, system)
        if self.chat_format == "raw":
            text = self.complete(prompt, n_predict, temperature, stop=stop, cache_prompt=cache_prompt)
        else:
            t0 = time.perf_counter()
            out = self._post("/completion", {
                "prompt": prompt, "n_predict": n_predict, "temperature": temperature, "top_p": 0.9,
                "repeat_penalty": 1.15, "repeat_last_n": 128, "cache_prompt": cache_prompt,
                "stop": stop or CHATML_STOP,
            })
            text = (out.get("content") or "").strip()
            log.debug("llm[%s] %.1fs, %s prompt tokens", feature or "chat", time.perf_counter() - t0,
                      out.get("tokens_evaluated"))
        return strip_thinking(text)

    def chat_json(self, user: str, schema: dict, *, system: str | None = None, n_predict: int = 300,
                  cache_prompt: bool = False, feature: str = "") -> dict:
        """A chat answer constrained by a JSON schema. Raises LlmError when the answer is not an object."""
        prompt = self._prompt(user, system)
        if self.chat_format == "raw":
            return self.complete_json(prompt, schema, n_predict, cache_prompt=cache_prompt)
        t0 = time.perf_counter()
        out = self._post("/completion", {
            "prompt": prompt, "n_predict": n_predict, "temperature": 0.2, "top_p": 0.9,
            "cache_prompt": cache_prompt, "stop": CHATML_STOP, "json_schema": schema,
        })
        log.debug("llm[%s] %.1fs, %s prompt tokens", feature or "json", time.perf_counter() - t0,
                  out.get("tokens_evaluated"))
        try:
            data = json.loads(strip_thinking(out.get("content") or ""))
        except ValueError as e:
            raise LlmError(f"LLM returned no valid JSON: {e}") from e
        if not isinstance(data, dict):
            raise LlmError("LLM returned JSON that is not an object")
        return data
