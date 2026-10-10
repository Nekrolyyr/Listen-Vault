# Build context: the repository root (see compose.yaml). Targets: api, worker.

FROM node:22-alpine AS web
WORKDIR /web
COPY services/web/package.json services/web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY services/web/ ./
RUN npm run build

FROM python:3.12-slim-bookworm AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
# uid/gid 1000 matches the host user that owns /vault/listen-vault
RUN groupadd -g 1000 app && useradd -u 1000 -g app -m -d /home/app app
WORKDIR /app
COPY services/backend/pyproject.toml ./
COPY services/backend/listen ./listen

FROM base AS api
RUN pip install .
COPY --from=web /web/dist /app/static
USER app
EXPOSE 8000
CMD ["listen-api"]

FROM base AS worker
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg tesseract-ocr tesseract-ocr-eng ghostscript \
 && rm -rf /var/lib/apt/lists/*
RUN pip install ".[worker]"
USER app
CMD ["listen-worker"]
