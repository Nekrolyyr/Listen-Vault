-- Area placements (see worker/classify.py): `agreement` is the share of the nearest papers the user filed under the
-- same primary area (null when the paper has no vector yet). 'exemplars' is a placement taken from those papers.

ALTER TABLE document_areas ADD COLUMN agreement real;
ALTER TABLE document_areas DROP CONSTRAINT document_areas_source_check;
ALTER TABLE document_areas ADD CONSTRAINT document_areas_source_check
    CHECK (source IN ('llm', 'rules', 'manual', 'exemplars'));
