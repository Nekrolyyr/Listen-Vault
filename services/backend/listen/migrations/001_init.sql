-- Documents (papers and articles) with their chapters, sentence timings, jobs and playback state.

CREATE TABLE documents (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_type       text NOT NULL CHECK (source_type IN ('pdf', 'url')),
    source_url        text,
    original_filename text,
    title             text NOT NULL DEFAULT '',
    authors           text[] NOT NULL DEFAULT '{}',
    published         text,
    has_pdf           boolean NOT NULL DEFAULT false,
    ocr_applied       boolean NOT NULL DEFAULT false,
    status            text NOT NULL DEFAULT 'queued'
                      CHECK (status IN ('queued', 'processing', 'ready', 'failed')),
    error             text,
    duration_s        double precision,
    voice             text,
    listened          boolean NOT NULL DEFAULT false,
    last_listened_at  timestamptz,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE chapters (
    id          bigserial PRIMARY KEY,
    document_id uuid NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
    idx         int NOT NULL,
    kind        text NOT NULL CHECK (kind IN ('overview', 'chapter')),
    title       text NOT NULL,
    intro       text,
    start_page  int,
    word_count  int NOT NULL DEFAULT 0,
    audio_file  text,
    duration_s  double precision,
    UNIQUE (document_id, idx)
);

CREATE TABLE sentences (
    chapter_id bigint NOT NULL REFERENCES chapters (id) ON DELETE CASCADE,
    idx        int NOT NULL,
    text       text NOT NULL,
    page       int,
    start_s    double precision NOT NULL,
    end_s      double precision NOT NULL,
    PRIMARY KEY (chapter_id, idx)
);

CREATE TABLE jobs (
    id             bigserial PRIMARY KEY,
    document_id    uuid NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
    state          text NOT NULL DEFAULT 'queued'
                   CHECK (state IN ('queued', 'running', 'done', 'failed')),
    stage          text,
    progress_done  int NOT NULL DEFAULT 0,
    progress_total int NOT NULL DEFAULT 0,
    attempts       int NOT NULL DEFAULT 0,
    error          text,
    options        jsonb NOT NULL DEFAULT '{}',
    run_after      timestamptz NOT NULL DEFAULT now(),
    created_at     timestamptz NOT NULL DEFAULT now(),
    started_at     timestamptz,
    finished_at    timestamptz,
    heartbeat_at   timestamptz,
    worker         text
);
CREATE INDEX jobs_open ON jobs (run_after) WHERE state IN ('queued', 'running');
CREATE INDEX jobs_document ON jobs (document_id, id DESC);

CREATE TABLE playback (
    document_id uuid PRIMARY KEY REFERENCES documents (id) ON DELETE CASCADE,
    chapter_idx int NOT NULL DEFAULT 0,
    position_s  double precision NOT NULL DEFAULT 0,
    rate        double precision NOT NULL DEFAULT 1,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE settings (
    key   text PRIMARY KEY,
    value jsonb NOT NULL
);
