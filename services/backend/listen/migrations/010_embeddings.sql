-- One paper vector per paper (SPECTER2, see listen/embeddings.py). Used for related papers, the areas of similar
-- papers and the topic clusters. `model` names the model that made the vector; a vector of another model is redone.

CREATE TABLE document_embeddings (
    document_id uuid PRIMARY KEY REFERENCES documents (id) ON DELETE CASCADE,
    model       text NOT NULL,
    vector      real[] NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now()
);
