ALTER TABLE users ADD COLUMN streak integer NOT NULL DEFAULT 0 CHECK (streak>=0);
CREATE TABLE dialog_states (
    user_id bigint PRIMARY KEY,
    state text NOT NULL,
    expires_at timestamptz NOT NULL DEFAULT now()+interval '30 minutes'
);
CREATE TABLE admin_sessions (
    user_id bigint PRIMARY KEY,
    expires_at timestamptz NOT NULL DEFAULT now()+interval '12 hours'
);
CREATE TABLE promo_codes (
    code text PRIMARY KEY,
    bonus integer NOT NULL CHECK (bonus>0),
    description text NOT NULL DEFAULT '',
    active boolean NOT NULL DEFAULT true,
    created_by bigint NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE promo_redemptions (
    user_id bigint NOT NULL REFERENCES users(telegram_id),
    code text NOT NULL REFERENCES promo_codes(code),
    granted integer NOT NULL CHECK (granted>=0),
    PRIMARY KEY (user_id,code)
);
CREATE TABLE subscription_rewards (
    user_id bigint NOT NULL REFERENCES users(telegram_id),
    channel text NOT NULL,
    granted integer NOT NULL CHECK (granted>=0),
    PRIMARY KEY (user_id,channel)
);
