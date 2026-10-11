-- Search and indexing terms per paper or article: the task, methods, datasets, topics and keywords the
-- LLM found, plus frequent words from the text (source 'frequency', weight 0.5) for recall.
-- Replaced as a whole each time a document is enriched.

CREATE TABLE document_keywords (
    document_id uuid NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
    kind        text NOT NULL CHECK (kind IN ('task', 'method', 'dataset', 'topic', 'keyword')),
    term        text NOT NULL,
    source      text NOT NULL CHECK (source IN ('llm', 'frequency')),
    weight      double precision NOT NULL DEFAULT 1,
    PRIMARY KEY (document_id, kind, term)
);
CREATE INDEX document_keywords_term ON document_keywords (term);
