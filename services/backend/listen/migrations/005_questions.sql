-- Questions asked about a paper (LLM Q&A). The answer is written by a 'qa' job in the worker.
-- citations: [{"n": 1, "page": 3, "title": "Methods", "text": "first words of the passage"}]

CREATE TABLE questions (
    id          bigserial PRIMARY KEY,
    document_id uuid NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
    question    text NOT NULL,
    answer      text,
    citations   jsonb NOT NULL DEFAULT '[]',
    status      text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'done', 'failed')),
    error       text,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX questions_document ON questions (document_id, id);
