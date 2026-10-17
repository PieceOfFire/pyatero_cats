ALTER TABLE cats
    ADD COLUMN is_draft boolean NOT NULL DEFAULT false,
    ADD COLUMN import_sha256 text UNIQUE,
    ADD CONSTRAINT cats_drafts_inactive CHECK (NOT is_draft OR NOT active);

COMMENT ON COLUMN cats.is_draft IS 'Imported photo awaiting description, rarity and explicit publication';
COMMENT ON COLUMN cats.import_sha256 IS 'SHA-256 of original imported bytes; prevents duplicate imports';
