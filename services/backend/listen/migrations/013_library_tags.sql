-- Custom tags the user puts on a paper in their library. Like the listened flag they belong to the user, so two users
-- who share a paper keep their own tags. The entry's foreign key removes them with the paper's library entry.

CREATE TABLE library_tags (
    user_id     bigint NOT NULL,
    document_id uuid NOT NULL,
    tag         text NOT NULL CHECK (tag = lower(tag) AND length(tag) BETWEEN 1 AND 32),
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, document_id, tag),
    FOREIGN KEY (user_id, document_id) REFERENCES library_entries (user_id, document_id) ON DELETE CASCADE
);
