-- Where each spoken sentence sits on the PDF: [[page, x, y, width, height], ...] in PDF points, one rectangle
-- per line it covers. Lets the reader scroll to and highlight the sentence being read. NULL for articles and
-- for sentences generated before this migration (process the paper from scratch to fill them in).

ALTER TABLE sentences ADD COLUMN rects jsonb;
