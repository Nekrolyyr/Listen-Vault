-- Storage per user (docs/gaps-plan.md, phase 4): the PDFs of the user's library (each holder counts the PDF) and the
-- bytes of the user's own audio. The limit is 1 GiB by default; an administrator can change it per user.
ALTER TABLE users ADD COLUMN storage_quota_bytes bigint NOT NULL DEFAULT 1073741824 CHECK (storage_quota_bytes >= 0);
ALTER TABLE documents ADD COLUMN pdf_bytes bigint NOT NULL DEFAULT 0;
-- Existing files are measured by `python -m listen.cli measure-storage` (the migration cannot read the files).
