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
    listen_user: str = ""
    proxy_secret: str = ""
    max_upload_mb: int = 100
    static_dir: Path | None = None
    # worker
    models_dir: Path = Path("/models")
    llm_url: str = "http://llm:8080"
    grobid_url: str = "http://grobid:8070"
    tts_threads: int = 8
    ocr_jobs: int = 8
    grobid_wait_s: float = 180
    llm_wait_s: float = 300
    lexicon_path: Path = Path("/app/config/normalizer/lexicon.yaml")
    # Zotero (optional): every item is catalogued; processing is always started by hand
    zotero_user_id: str = ""
    zotero_api_key: str = ""
    zotero_sync_s: float = 1800

    @property
    def docs_dir(self) -> Path:
        return self.data_dir / "docs"

    @property
    def zotero_configured(self) -> bool:
        return bool(self.zotero_user_id and self.zotero_api_key)


def load() -> Settings:
    env = os.environ
    static = env.get("STATIC_DIR", "/app/static")
    return Settings(
        database_url=env["DATABASE_URL"],
        data_dir=Path(env.get("DATA_DIR", "/data")),
        listen_user=env.get("LISTEN_USER", ""),
        proxy_secret=env.get("LISTEN_PROXY_SECRET", ""),
        max_upload_mb=int(env.get("MAX_UPLOAD_MB", "100")),
        static_dir=Path(static) if Path(static).is_dir() else None,
        models_dir=Path(env.get("MODELS_DIR", "/models")),
        llm_url=env.get("LLM_URL", "http://llm:8080"),
        grobid_url=env.get("GROBID_URL", "http://grobid:8070"),
        tts_threads=int(env.get("TTS_THREADS", "8")),
        ocr_jobs=int(env.get("OCR_JOBS", "8")),
        grobid_wait_s=float(env.get("GROBID_WAIT_S", "180")),
        llm_wait_s=float(env.get("LLM_WAIT_S", "300")),
        lexicon_path=Path(env.get("LEXICON_PATH", "/app/config/normalizer/lexicon.yaml")),
        zotero_user_id=env.get("ZOTERO_USER_ID", "").strip(),
        zotero_api_key=env.get("ZOTERO_API_KEY", "").strip(),
        zotero_sync_s=float(env.get("ZOTERO_SYNC_MINUTES", "30")) * 60,
    )
