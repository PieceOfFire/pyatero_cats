-- Late completion of an older navigation request must not clear a newer prompt.
ALTER TABLE dialog_states ADD COLUMN update_id bigint NOT NULL DEFAULT 0;
