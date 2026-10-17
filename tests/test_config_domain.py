from datetime import date
from urllib.parse import unquote

import pytest

from catbot.config import Settings, parse_admin_ids
from catbot.db import asyncpg_url
from catbot.domain import choose_cat, daily_terms, parse_promo, validate_cat
from catbot.mail_storage import webdav_url
from catbot.tools import parse_legacy_users, pg_environment


def test_empty_catalog():
    with pytest.raises(ValueError):
        choose_cat([])


def test_missing_rarity_falls_back_to_existing_card():
    class Rng:
        def choices(self, *args, **kwargs):
            return ["LEG"]

        def choice(self, values):
            return values[0]

    cat = {"id": "one", "rarity": "COM"}
    assert choose_cat([cat], Rng()) == cat


@pytest.mark.parametrize(
    "cat_id,description,rarity",
    [
        ("", "кот", "COM"),
        ("cat one", "кот", "COM"),
        ("a|b", "кот", "COM"),
        ("one", "", "COM"),
        ("one", "x" * 701, "COM"),
        ("one", "кот", "SUPER"),
    ],
)
def test_bad_catalog_values(cat_id, description, rarity):
    with pytest.raises(ValueError):
        validate_cat(cat_id, description, rarity)


def test_webhook_needs_strong_secret():
    with pytest.raises(ValueError, match="WEBHOOK_SECRET"):
        Settings(
            "token", "postgresql://db/cats", mode="webhook", webhook_base_url="https://bot.test"
        ).validate()


def test_settings_hide_credentials():
    settings = Settings("secret_token", "secret_dsn", webhook_secret="secret_hook")
    assert "secret_token" not in repr(settings)
    assert "secret_dsn" not in repr(settings)
    assert "secret_hook" not in repr(settings)


@pytest.mark.parametrize("path", ["../secret", "/cats/../secret", "https://other/../secret", "x\x00"])
def test_webdav_rejects_traversal(path):
    with pytest.raises(ValueError):
        webdav_url(path)


def test_webdav_encodes_paths_on_fixed_host():
    url = webdav_url("/Котята/мой кот.jpg")
    assert url.startswith("https://webdav.cloud.mail.ru/")
    assert unquote(url).endswith("/Котята/мой кот.jpg")
    assert webdav_url("https://evil.example/cat").startswith("https://webdav.cloud.mail.ru/")


def test_legacy_user_preserves_ids_collection_score_and_date():
    rows = [
        {
            "user_id": "123",
            "nick": "Котолюб",
            "cats_id": "a | b | a",
            "spins": "3",
            "last_daily": "2026-10-17",
            "sum": "42",
        }
    ]
    assert parse_legacy_users(rows, {"a": "COM", "b": "RARE"}, 999) == [
        (123, "Котолюб", 3, 42, date(2026, 10, 17), ["a", "b"], 0)
    ]


def test_import_rejects_unknown_cards_before_writing():
    with pytest.raises(ValueError, match="сначала импортируйте"):
        parse_legacy_users([{"user_id": "1", "cats_id": "unknown"}], {}, 999)


def test_libpq_uses_environment_for_credentials():
    env = pg_environment("postgresql://cats:p%40ss@db.example/cats?sslmode=require")
    assert env["PGPASSWORD"] == "p@ss"
    assert env["PGSSLMODE"] == "require"
    assert env["PGHOST"] == "db.example"


def test_neon_libpq_url_is_compatible_with_asyncpg():
    url = asyncpg_url("postgresql://u:p%40ss@host/db?sslmode=require&channel_binding=require")
    assert "channel_binding" not in url
    assert "sslmode=require" in url
    assert "p%40ss" in url


@pytest.mark.parametrize("previous_streak,bonus", [(0, 1), (2, 1), (3, 2), (5, 2), (6, 3), (30, 3)])
def test_daily_streak_reward_boundaries(previous_streak, bonus):
    reward, streak = daily_terms(date(2026, 10, 16), previous_streak, date(2026, 10, 17), 99, "streak")
    assert (reward, streak) == (bonus, previous_streak + 1)


def test_daily_streak_resets_after_missing_day():
    assert daily_terms(date(2026, 10, 15), 20, date(2026, 10, 17), 3, "streak") == (1, 1)


def test_daily_repeat_does_not_increase_streak():
    assert daily_terms(date(2026, 10, 17), 7, date(2026, 10, 17), 3, "streak") == (0, 7)


def test_single_and_multiple_admin_secrets_are_compatible(monkeypatch):
    monkeypatch.setenv("ADMIN_ID", "123")
    monkeypatch.setenv("ADMIN_TELEGRAM_ID", "456")
    monkeypatch.setenv("ADMIN_IDS", "123,789")
    assert parse_admin_ids() == frozenset({123, 456, 789})


def test_admin_secret_rejects_username(monkeypatch):
    monkeypatch.setenv("ADMIN_ID", "@username")
    with pytest.raises(ValueError, match="числовой"):
        parse_admin_ids()


def test_parse_promo_normalizes_case():
    assert parse_promo(" cats2026 | 5 | Привет ", 999) == ("CATS2026", 5, "Привет")


@pytest.mark.parametrize("raw", ["code", "code | -5", "code | 0", "code | 1000", "a b | 2"])
def test_promo_rejects_invalid_bonus_or_code(raw):
    with pytest.raises(ValueError):
        parse_promo(raw, 999)
