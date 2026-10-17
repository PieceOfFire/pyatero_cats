ALTER TABLE users
    ADD COLUMN telegram_username text,
    ADD COLUMN last_known_telegram_username text,
    ADD COLUMN nickname_custom boolean NOT NULL DEFAULT false,
    ADD COLUMN telegram_identity_update_id bigint NOT NULL DEFAULT 0,
    ADD COLUMN telegram_identity_seen_at timestamptz;

-- Existing names may have been chosen manually or imported. Keep them intact.
UPDATE users SET nickname_custom=true WHERE nickname<>'';

COMMENT ON COLUMN users.telegram_username IS 'Current Telegram username without @; null if absent';
COMMENT ON COLUMN users.last_known_telegram_username IS 'Last observed nonempty username; may be outdated';
COMMENT ON COLUMN users.nickname_custom IS 'Manual nickname, independent of Telegram username';
