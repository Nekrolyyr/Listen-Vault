-- Topics: groups of keywords that appear together across the library, named by the LLM (library job 'topics').
-- Rebuilt as a whole. A keyword belongs to a topic through `terms`; a paper is on a topic's timeline when
-- one of its keywords is in `terms`.

CREATE TABLE topics (
    id       serial PRIMARY KEY,
    name     text NOT NULL,
    terms    text[] NOT NULL,
    built_at timestamptz NOT NULL DEFAULT now()
);
