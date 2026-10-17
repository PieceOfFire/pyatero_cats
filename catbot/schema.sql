-- Initial schema. Applied once under a transaction-level advisory lock.
CREATE TABLE bot_meta (key text PRIMARY KEY, value text NOT NULL);
CREATE TABLE users (
    telegram_id bigint PRIMARY KEY,
    nickname text NOT NULL DEFAULT '',
    spins integer NOT NULL CHECK (spins >= 0),
    score bigint NOT NULL DEFAULT 0 CHECK (score >= 0),
    last_daily date,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE cats (
    id text PRIMARY KEY,
    description text NOT NULL,
    rarity text NOT NULL CHECK (rarity IN ('COM','UCOM','RARE','EPIC','LEG')),
    telegram_file_id text NOT NULL,
    telegram_file_unique_id text,
    source_path text,
    active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE user_cats (
    user_id bigint NOT NULL REFERENCES users(telegram_id),
    cat_id text NOT NULL REFERENCES cats(id),
    acquired_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, cat_id)
);
CREATE TABLE rolls (
    update_id bigint PRIMARY KEY,
    user_id bigint NOT NULL REFERENCES users(telegram_id),
    cat_id text NOT NULL REFERENCES cats(id),
    points integer NOT NULL,
    spins_after integer NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX rolls_user_history ON rolls(user_id, created_at DESC);
CREATE INDEX users_leaderboard ON users(score DESC, telegram_id);
CREATE TABLE update_receipts (
    update_id bigint PRIMARY KEY,
    status text NOT NULL CHECK (status IN ('processing','done')),
    lease_until timestamptz NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX update_receipts_cleanup ON update_receipts(updated_at);
