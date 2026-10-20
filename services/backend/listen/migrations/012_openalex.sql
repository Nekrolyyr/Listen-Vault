-- OpenAlex topics of a paper, looked up by title when OPENALEX_EMAIL is set (worker/openalex.py). work_id is null
-- when OpenAlex has no work with that title, so the lookup is not repeated.

CREATE TABLE document_openalex (
    document_id uuid PRIMARY KEY REFERENCES documents (id) ON DELETE CASCADE,
    work_id     text,
    topics      jsonb NOT NULL DEFAULT '[]',
    fetched_at  timestamptz NOT NULL DEFAULT now()
);
