-- Accounts, library membership and shared documents (docs/multi-user-plan.md, phases 1 and 2).
-- A document is stored and processed once, however many users have it in their library. What belongs to one
-- user (that they have the paper, whether they listened to it, where they stopped) lives in library_entries.

CREATE TABLE users (
    id                bigserial PRIMARY KEY,
    email             text NOT NULL UNIQUE CHECK (email = lower(email)),
    password_hash     text,
    is_admin          boolean NOT NULL DEFAULT false,
    is_disabled       boolean NOT NULL DEFAULT false,
    email_verified_at timestamptz,
    page_quota        int NOT NULL DEFAULT 300 CHECK (page_quota >= 0),
    created_at        timestamptz NOT NULL DEFAULT now(),
    last_login_at     timestamptz
);

CREATE TABLE sessions (
    token_hash   text PRIMARY KEY,
    user_id      bigint NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    created_at   timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz NOT NULL,
    last_seen_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX sessions_user ON sessions (user_id);

CREATE TABLE email_tokens (
    token_hash text PRIMARY KEY,
    user_id    bigint NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    purpose    text NOT NULL CHECK (purpose IN ('verify')),
    expires_at timestamptz NOT NULL
);

-- Login and registration attempts, counted per IP and per email for rate limiting.
CREATE TABLE auth_attempts (
    key text NOT NULL,
    at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX auth_attempts_key ON auth_attempts (key, at);

-- The terms of service. The text is in config/terms/<version>.md; the hash detects an edited file.
CREATE TABLE terms_versions (
    version      text PRIMARY KEY,
    text_sha256  text NOT NULL,
    published_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE user_consents (
    user_id     bigint NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    version     text NOT NULL REFERENCES terms_versions (version),
    accepted_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, version)
);

CREATE TABLE library_entries (
    user_id         bigint NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    document_id     uuid NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
    added_at        timestamptz NOT NULL DEFAULT now(),
    listened        boolean NOT NULL DEFAULT false,
    last_listened_at timestamptz,
    PRIMARY KEY (user_id, document_id)
);
CREATE INDEX library_entries_document ON library_entries (document_id);

CREATE TABLE admin_audit (
    id             bigserial PRIMARY KEY,
    actor_id       bigint REFERENCES users (id) ON DELETE SET NULL,
    action         text NOT NULL,
    target_user_id bigint,
    detail         jsonb NOT NULL DEFAULT '{}',
    created_at     timestamptz NOT NULL DEFAULT now()
);

-- The existing library belongs to the one user of the single-user version. A placeholder owner is created
-- here; `listen.cli make-admin <email>` claims it with a real address and password.
INSERT INTO users (email, is_admin, page_quota, email_verified_at)
SELECT 'owner@legacy.invalid', true, 1000000, now() WHERE EXISTS (SELECT 1 FROM documents);

INSERT INTO library_entries (user_id, document_id, added_at, listened, last_listened_at)
SELECT u.id, d.id, d.created_at, d.listened, d.last_listened_at
FROM documents d CROSS JOIN users u WHERE u.email = 'owner@legacy.invalid';

ALTER TABLE documents DROP COLUMN listened, DROP COLUMN last_listened_at;
ALTER TABLE documents ADD COLUMN content_sha256 text, ADD COLUMN pages int;
CREATE UNIQUE INDEX documents_content_sha256 ON documents (content_sha256) WHERE content_sha256 IS NOT NULL;
CREATE INDEX documents_source_url ON documents (source_url) WHERE source_url IS NOT NULL;

-- Playback is per user. The existing position belongs to the legacy owner.
ALTER TABLE playback DROP CONSTRAINT playback_pkey;
ALTER TABLE playback ADD COLUMN user_id bigint REFERENCES users (id) ON DELETE CASCADE;
UPDATE playback SET user_id = (SELECT id FROM users WHERE email = 'owner@legacy.invalid');
ALTER TABLE playback ALTER COLUMN user_id SET NOT NULL;
ALTER TABLE playback ADD PRIMARY KEY (user_id, document_id);

-- Questions belong to the user who asked them.
ALTER TABLE questions ADD COLUMN user_id bigint REFERENCES users (id) ON DELETE CASCADE;
UPDATE questions SET user_id = (SELECT id FROM users WHERE email = 'owner@legacy.invalid');

-- Jobs record who asked, so the worker can share its time between users.
ALTER TABLE jobs ADD COLUMN user_id bigint REFERENCES users (id) ON DELETE SET NULL;
CREATE INDEX jobs_user_open ON jobs (user_id) WHERE state IN ('queued', 'running');
