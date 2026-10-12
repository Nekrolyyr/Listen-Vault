-- Research areas and topic membership (library job 'areas').
-- Every paper has one primary area and up to two more (ids from listen/areas.py). A placement made by hand
-- is pinned and is not changed by the job. Topics get an area and their papers (rebuilt with the topics).
-- A manual topic choice is kept by topic name, so it survives a rebuild: include adds the paper to the topic,
-- exclude keeps it out.

ALTER TABLE topics ADD COLUMN area text, ADD COLUMN paper_count int NOT NULL DEFAULT 0;

CREATE TABLE document_areas (
    document_id  uuid PRIMARY KEY REFERENCES documents (id) ON DELETE CASCADE,
    primary_area text NOT NULL,
    also_areas   text[] NOT NULL DEFAULT '{}',
    confidence   text NOT NULL CHECK (confidence IN ('high', 'medium', 'low')),
    source       text NOT NULL CHECK (source IN ('llm', 'rules', 'manual')),
    pinned       boolean NOT NULL DEFAULT false,
    updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX document_areas_primary ON document_areas (primary_area);

CREATE TABLE document_topics (
    document_id uuid NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
    topic_id    int NOT NULL REFERENCES topics (id) ON DELETE CASCADE,
    PRIMARY KEY (document_id, topic_id)
);
CREATE INDEX document_topics_topic ON document_topics (topic_id);

CREATE TABLE document_topic_overrides (
    document_id uuid NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
    topic_name  text NOT NULL,
    include     boolean NOT NULL,
    PRIMARY KEY (document_id, topic_name)
);

-- The library job 'areas' (research areas for every paper).
ALTER TABLE jobs DROP CONSTRAINT jobs_kind_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check
    CHECK (kind IN ('document', 'zotero_sync', 'qa', 'topics', 'areas'));
