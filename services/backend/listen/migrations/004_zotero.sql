-- Zotero integration.
--   * 'catalogued': a Zotero item that is known but not queued for processing yet
--   * jobs can now belong to no document (kind 'zotero_sync'); 'qa' and 'topics' are used by later phases
--   * abstract and DOI come from the metadata source (Zotero) and feed keyword extraction and the overview

ALTER TABLE documents DROP CONSTRAINT documents_status_check;
ALTER TABLE documents ADD CONSTRAINT documents_status_check
    CHECK (status IN ('catalogued', 'queued', 'processing', 'ready', 'failed'));
ALTER TABLE documents ADD COLUMN abstract text;
ALTER TABLE documents ADD COLUMN doi text;

ALTER TABLE jobs ALTER COLUMN document_id DROP NOT NULL;
ALTER TABLE jobs ADD COLUMN kind text NOT NULL DEFAULT 'document'
    CHECK (kind IN ('document', 'zotero_sync', 'qa', 'topics'));
ALTER TABLE jobs ADD CONSTRAINT jobs_document_required CHECK (kind <> 'document' OR document_id IS NOT NULL);

-- One row per Zotero item that became a document. pdf_attachment is the key of the item's PDF attachment.
CREATE TABLE zotero_links (
    document_id     uuid PRIMARY KEY REFERENCES documents (id) ON DELETE CASCADE,
    zotero_key      text NOT NULL UNIQUE,
    library_version integer NOT NULL,
    pdf_attachment  text,
    tags            text[] NOT NULL DEFAULT '{}',
    collections     text[] NOT NULL DEFAULT '{}',
    synced_at       timestamptz NOT NULL DEFAULT now()
);

-- Progress of the sync with the Zotero library (a single row).
CREATE TABLE zotero_state (
    id             int PRIMARY KEY CHECK (id = 1),
    last_version   integer NOT NULL DEFAULT 0,
    last_synced_at timestamptz,
    last_error     text
);
INSERT INTO zotero_state (id) VALUES (1);
