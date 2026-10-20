"""Sentence vectors on the CPU, from files in MODELS_DIR/embed (scripts/fetch-models.sh downloads them).

- SPECTER2 with its proximity adapter: one vector per paper, from its title and abstract. Papers that cite each other
  or share a topic get close vectors. Used for related papers, the areas of similar papers and the topic clusters.
- bge-small-en-v1.5: one vector per passage and per question, for the hybrid search of the ask function.

torch and transformers are imported on first use, so the api and the tests do not need them. Nothing is downloaded
at run time: a missing model raises EmbedError, and the callers carry on without it.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

from ..vectors import normalize

log = logging.getLogger(__name__)

SPECTER_BASE = "specter2_base"
SPECTER_ADAPTER = "specter2_adapter"
PASSAGE_MODEL = "bge-small"
PASSAGE_QUERY = "Represent this sentence for searching relevant passages: "
BATCH = 8
MAX_TOKENS = 512

_cache: dict[str, object] = {}
_lock = threading.Lock()


class EmbedError(Exception):
    pass


def _dir(models_dir: Path, name: str) -> Path:
    path = models_dir / "embed" / name
    if not (path / "config.json").exists():
        raise EmbedError(f"{path} is missing; run scripts/fetch-models.sh")
    return path


def available(models_dir: Path, name: str) -> bool:
    return (models_dir / "embed" / name / "config.json").exists()


class Specter:
    """SPECTER2 base model with the proximity adapter. The paper vector is the [CLS] state, normalized."""

    def __init__(self, models_dir: Path):
        import torch
        from adapters import AutoAdapterModel
        from transformers import AutoTokenizer

        base = _dir(models_dir, SPECTER_BASE)
        adapter = models_dir / "embed" / SPECTER_ADAPTER
        self._torch = torch
        self.tokenizer = AutoTokenizer.from_pretrained(base)
        self.model = AutoAdapterModel.from_pretrained(base)
        self.model.load_adapter(str(adapter), source=None, load_as="proximity", set_active=True)
        self.model.eval()

    def paper_vectors(self, papers: list[tuple[str, str]]) -> list[list[float]]:
        """One vector per (title, abstract). The model reads title, [SEP] and abstract, as in SPECTER."""
        out: list[list[float]] = []
        for start in range(0, len(papers), BATCH):
            texts = [f"{title or ''}{self.tokenizer.sep_token}{abstract or ''}"
                     for title, abstract in papers[start:start + BATCH]]
            enc = self.tokenizer(texts, padding=True, truncation=True, max_length=MAX_TOKENS, return_tensors="pt")
            with self._torch.no_grad():
                cls = self.model(**enc).last_hidden_state[:, 0, :]
            out += [normalize(row) for row in cls.tolist()]
        return out


class Passages:
    """bge-small: [CLS] pooling, normalized. Questions get the model's retrieval prefix."""

    def __init__(self, models_dir: Path):
        import torch
        from transformers import AutoModel, AutoTokenizer

        path = _dir(models_dir, PASSAGE_MODEL)
        self._torch = torch
        self.tokenizer = AutoTokenizer.from_pretrained(path)
        self.model = AutoModel.from_pretrained(path)
        self.model.eval()

    def _encode(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for start in range(0, len(texts), BATCH):
            enc = self.tokenizer(texts[start:start + BATCH], padding=True, truncation=True, max_length=MAX_TOKENS,
                                 return_tensors="pt")
            with self._torch.no_grad():
                cls = self.model(**enc).last_hidden_state[:, 0, :]
            out += [normalize(row) for row in cls.tolist()]
        return out

    def passage_vectors(self, texts: list[str]) -> list[list[float]]:
        return self._encode(texts)

    def query_vector(self, question: str) -> list[float]:
        return self._encode([PASSAGE_QUERY + question])[0]


def _get(models_dir: Path, key: str, factory):
    with _lock:
        if key not in _cache:
            _cache[key] = factory(models_dir)
        return _cache[key]


def specter(models_dir: Path) -> Specter:
    return _get(models_dir, "specter", Specter)


def passages(models_dir: Path) -> Passages:
    return _get(models_dir, "passages", Passages)


def passage_encoder(models_dir: Path) -> Passages | None:
    """The passage model, or None when it is not installed (the ask function then uses BM25 alone)."""
    if not available(models_dir, PASSAGE_MODEL):
        log.warning("passage model missing in %s; questions use keyword search only", models_dir / "embed")
        return None
    try:
        return passages(models_dir)
    except Exception as e:  # noqa: BLE001 - a broken model must not stop the ask function
        log.warning("passage model could not be loaded (%s); questions use keyword search only", e)
        return None


def ensure_library(pool, settings) -> int:
    """Vectors for the papers that have none yet (see listen/embeddings.py). Returns the number made. Papers that
    cannot be read are skipped; a missing model makes this a no-op."""
    from .. import embeddings

    if not settings.embeddings:
        return 0
    if not available(settings.models_dir, SPECTER_BASE):
        log.warning("SPECTER2 is not installed; paper vectors are skipped")
        return 0
    with pool.connection() as conn:
        todo = embeddings.missing(conn)
    if not todo:
        return 0
    try:
        model = specter(settings.models_dir)
    except Exception as e:  # noqa: BLE001 - model files or the ML libraries may be missing
        log.warning("SPECTER2 could not be loaded (%s); paper vectors are skipped", e)
        return 0
    rows = [r for r in todo if (r["title"] or r["abstract"])]
    vectors_made = model.paper_vectors([(r["title"] or "", r["abstract"] or "") for r in rows]) if rows else []
    with pool.connection() as conn, conn.transaction():
        for row, vector in zip(rows, vectors_made):
            embeddings.store(conn, row["id"], vector)
    log.info("paper vectors: %d made", len(rows))
    return len(rows)
