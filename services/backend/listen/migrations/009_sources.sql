-- Keywords from the KBIR keyword tagger (worker/tagger.py): spans of the text, kept beside the LLM's keywords.

ALTER TABLE document_keywords DROP CONSTRAINT document_keywords_source_check;
ALTER TABLE document_keywords ADD CONSTRAINT document_keywords_source_check
    CHECK (source IN ('llm', 'rule', 'frequency', 'tagger'));
