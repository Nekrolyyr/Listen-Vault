-- Search terms found by rules in the text (source 'rule', see worker/enrich.py): a method named in a list, a
-- dataset named in a sentence. 003 allowed only the LLM and the frequent words.

ALTER TABLE document_keywords DROP CONSTRAINT document_keywords_source_check;
ALTER TABLE document_keywords ADD CONSTRAINT document_keywords_source_check
    CHECK (source IN ('llm', 'rule', 'frequency'));
