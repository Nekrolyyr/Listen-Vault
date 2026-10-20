-- Audio is per user (docs/gaps-plan.md, phase 3). A narration is the audio of one paper for the user who added it,
-- with that user's audio settings. Its chapter files and sentence timings belong to the narration.
-- The existing audio becomes the narration of the legacy owner, which added every existing paper; the files stay
-- where they are (their path is stored relative to the paper's folder).

-- Audio and other settings per account (the admin's settings stay in `settings`).
CREATE TABLE user_settings (
    user_id bigint NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    key     text NOT NULL,
    value   jsonb NOT NULL,
    PRIMARY KEY (user_id, key)
);

INSERT INTO user_settings (user_id, key, value)
SELECT u.id, s.key, s.value FROM settings s CROSS JOIN users u
WHERE u.email = 'owner@legacy.invalid' AND s.key IN ('engine', 'voice', 'speech_speed', 'citations', 'reading_wpm');
DELETE FROM settings WHERE key IN ('engine', 'voice', 'speech_speed', 'citations', 'reading_wpm');

CREATE TABLE narrations (
    id          bigserial PRIMARY KEY,
    document_id uuid NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
    user_id     bigint NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    state       text NOT NULL DEFAULT 'queued' CHECK (state IN ('queued', 'running', 'ready', 'failed')),
    settings    jsonb NOT NULL DEFAULT '{}',   -- the audio settings the audio was made with
    duration_s  double precision,
    bytes       bigint NOT NULL DEFAULT 0,
    error       text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (document_id, user_id)
);

CREATE TABLE narration_chapters (
    narration_id bigint NOT NULL REFERENCES narrations (id) ON DELETE CASCADE,
    idx          int NOT NULL,
    audio_file   text NOT NULL,              -- relative to the paper's folder
    duration_s   double precision NOT NULL,
    bytes        bigint NOT NULL DEFAULT 0,
    PRIMARY KEY (narration_id, idx)
);

CREATE TABLE narration_sentences (
    narration_id bigint NOT NULL REFERENCES narrations (id) ON DELETE CASCADE,
    chapter_idx  int NOT NULL,
    idx          int NOT NULL,
    text         text NOT NULL,
    page         int,
    start_s      double precision NOT NULL,
    end_s        double precision NOT NULL,
    rects        jsonb,
    PRIMARY KEY (narration_id, chapter_idx, idx)
);

-- Move the existing audio of the legacy owner.
INSERT INTO narrations (document_id, user_id, state, settings, duration_s)
SELECT d.id, o.id, 'ready', coalesce((SELECT jsonb_object_agg(us.key, us.value) FROM user_settings us
                                      WHERE us.user_id = o.id), '{}'::jsonb), d.duration_s
FROM documents d CROSS JOIN users o
WHERE o.email = 'owner@legacy.invalid'
  AND EXISTS (SELECT 1 FROM chapters c WHERE c.document_id = d.id AND c.audio_file IS NOT NULL);

INSERT INTO narration_chapters (narration_id, idx, audio_file, duration_s)
SELECT n.id, c.idx, 'audio/' || c.audio_file, c.duration_s
FROM chapters c JOIN narrations n ON n.document_id = c.document_id
WHERE c.audio_file IS NOT NULL
  AND n.user_id = (SELECT id FROM users WHERE email = 'owner@legacy.invalid');

INSERT INTO narration_sentences (narration_id, chapter_idx, idx, text, page, start_s, end_s, rects)
SELECT n.id, c.idx, s.idx, s.text, s.page, s.start_s, s.end_s, s.rects
FROM sentences s JOIN chapters c ON c.id = s.chapter_id JOIN narrations n ON n.document_id = c.document_id
WHERE n.user_id = (SELECT id FROM users WHERE email = 'owner@legacy.invalid');

ALTER TABLE chapters DROP COLUMN audio_file, DROP COLUMN duration_s;
DROP TABLE sentences;
ALTER TABLE documents DROP COLUMN duration_s, DROP COLUMN voice;

-- Narration jobs: the audio of a paper for one user (the job's user_id).
CREATE INDEX narrations_user ON narrations (user_id);

-- Job kinds: 'narrate' is new; 'zotero_sync' is gone with the Zotero integration (migration 010).
ALTER TABLE jobs DROP CONSTRAINT jobs_kind_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check CHECK (kind IN ('document', 'narrate', 'qa', 'topics', 'areas'));
