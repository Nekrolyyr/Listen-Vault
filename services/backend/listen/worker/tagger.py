"""Keyword tagger: ml6team/keyphrase-extraction-kbir-inspec, a token classifier that marks the keyphrases written in a text.

Its spans are taken from the text itself, so they are grounded by construction; the LLM's keywords can still be
paraphrases. It is one more source beside the LLM (KEYWORD_TAGGER=on, see enrich.extract). The model was trained on
short abstracts, so it is used on the abstract and the first parts of the body only.
"""
from __future__ import annotations

from pathlib import Path

from .enrich import clean_terms

CHUNK_WORDS = 150   # about 250 tokens per call, under the model's 512
MAX_CHUNKS = 12     # the abstract and the start of the body


def spans_to_terms(entities: list[dict]) -> list[str]:
    """The keyphrases of the tagger's output (token classification with aggregation "simple" gives entity_group "KEY")."""
    words = [e.get("word", "") for e in entities if e.get("entity_group") in ("KEY", "B-KEY", "I-KEY")]
    return clean_terms(words, 30)


def chunks(text: str) -> list[str]:
    words = text.split()
    return [" ".join(words[i:i + CHUNK_WORDS]) for i in range(0, len(words), CHUNK_WORDS)][:MAX_CHUNKS]


class Tagger:
    def __init__(self, path: Path):
        self.path = path
        self._pipe = None

    def _load(self):
        if self._pipe is None:
            if not (self.path / "config.json").exists():
                from .embed import EmbedError
                raise EmbedError(f"{self.path} is missing; run scripts/fetch-models.sh")
            from transformers import pipeline
            self._pipe = pipeline("token-classification", model=str(self.path), aggregation_strategy="simple", device=-1)
        return self._pipe

    def __call__(self, text: str) -> list[str]:
        pipe = self._load()
        terms: list[str] = []
        for chunk in chunks(text):
            for term in spans_to_terms(pipe(chunk)):
                if term not in terms:
                    terms.append(term)
        return terms
