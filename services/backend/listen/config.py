"""Settings from environment variables, shared by api and worker."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database_url: str
    data_dir: Path
    # api
    proxy_secret: str = ""
    max_upload_mb: int = 100
    static_dir: Path | None = None
    public_url: str = "http://localhost:8080"   # links in emails
    terms_dir: Path = Path("/app/config/terms")
    # Mail (verification emails). Without SMTP_HOST, accounts are verified at once and a warning is logged.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    # worker
    models_dir: Path = Path("/models")
    llm_url: str = "http://llm:8080"
    grobid_url: str = "http://grobid:8070"
    tts_threads: int = 8
    ocr_jobs: int = 8
    grobid_wait_s: float = 180
    llm_wait_s: float = 300
    lexicon_path: Path = Path("/app/config/normalizer/lexicon.yaml")
    # Language model and the optimisation switches (docs/optimisation-plan.md)
    llm_format: str = "chatml"          # chatml (Qwen3) | raw (BLOOMZ)
    keywords_mode: str = "typed"        # typed (one schema call) | legacy (free-text questions)
    keyword_tagger: bool = False        # KBIR keyword tagger as a second source of keywords
    embeddings: bool = True             # SPECTER2 paper vectors: related papers, areas, topics
    areas_exemplars: bool = False       # place papers by the areas of the nearest papers the user filed (gate failed: off)
    topics_mode: str = "embeddings"     # embeddings (paper clusters) | keywords (keyword clusters)
    qa_retrieval: str = "bm25"          # hybrid (BM25 + passage vectors) | bm25 (hybrid untested on a discriminating set)
    openalex_email: str = ""            # set to look up OpenAlex topics; titles are sent, nothing else

    @property
    def docs_dir(self) -> Path:
        return self.data_dir / "docs"

    @property
    def embed_dir(self) -> Path:
        return self.models_dir / "embed"

    @property
    def tagger_dir(self) -> Path:
        return self.models_dir / "embed" / "kbir-inspec"

    @property
    def smtp_configured(self) -> bool:
        return bool(self.smtp_host and self.smtp_from)



def load() -> Settings:
    env = os.environ
    static = env.get("STATIC_DIR", "/app/static")
    return Settings(
        database_url=env["DATABASE_URL"],
        data_dir=Path(env.get("DATA_DIR", "/data")),
        proxy_secret=env.get("LISTEN_PROXY_SECRET", ""),
        max_upload_mb=int(env.get("MAX_UPLOAD_MB", "100")),
        static_dir=Path(static) if Path(static).is_dir() else None,
        public_url=env.get("PUBLIC_URL", "http://localhost:8080").rstrip("/"),
        terms_dir=Path(env.get("TERMS_DIR", "/app/config/terms")),
        smtp_host=env.get("SMTP_HOST", "").strip(),
        smtp_port=int(env.get("SMTP_PORT", "587")),
        smtp_user=env.get("SMTP_USER", "").strip(),
        smtp_password=env.get("SMTP_PASSWORD", ""),
        smtp_from=env.get("SMTP_FROM", "").strip(),
        models_dir=Path(env.get("MODELS_DIR", "/models")),
        llm_url=env.get("LLM_URL", "http://llm:8080"),
        grobid_url=env.get("GROBID_URL", "http://grobid:8070"),
        tts_threads=int(env.get("TTS_THREADS", "8")),
        ocr_jobs=int(env.get("OCR_JOBS", "8")),
        grobid_wait_s=float(env.get("GROBID_WAIT_S", "180")),
        llm_wait_s=float(env.get("LLM_WAIT_S", "300")),
        lexicon_path=Path(env.get("LEXICON_PATH", "/app/config/normalizer/lexicon.yaml")),
        llm_format=_choice(env, "LLM_FORMAT", "chatml", ("chatml", "raw")),
        keywords_mode=_choice(env, "KEYWORDS_MODE", "typed", ("typed", "legacy")),
        keyword_tagger=_flag(env, "KEYWORD_TAGGER", False),
        embeddings=_flag(env, "EMBEDDINGS", True),
        areas_exemplars=_flag(env, "AREAS_EXEMPLARS", False),
        topics_mode=_choice(env, "TOPICS_MODE", "embeddings", ("embeddings", "keywords")),
        qa_retrieval=_choice(env, "QA_RETRIEVAL", "bm25", ("hybrid", "bm25")),
        openalex_email=env.get("OPENALEX_EMAIL", "").strip(),
    )


def _flag(env, name: str, default: bool) -> bool:
    value = env.get(name, "").strip().lower()
    if not value:
        return default
    return value in ("1", "true", "on", "yes")


def _choice(env, name: str, default: str, options: tuple[str, ...]) -> str:
    value = env.get(name, "").strip().lower() or default
    if value not in options:
        raise ValueError(f"{name} must be one of {', '.join(options)}, not {value!r}")
    return value
