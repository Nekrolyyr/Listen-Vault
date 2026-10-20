-- Zotero is removed (docs/gaps-plan.md, phase 1). Its catalogue rows were the only content of the items that were
-- never processed: those are deleted. Processed papers stay as ordinary papers.

DELETE FROM documents WHERE status = 'catalogued';
DELETE FROM jobs WHERE kind = 'zotero_sync';
DROP TABLE zotero_links;
DROP TABLE zotero_state;
