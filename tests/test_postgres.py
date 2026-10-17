"""Run only against an EMPTY dedicated database; never a production Neon database."""

import asyncio
import os
from datetime import date

import pytest
import pytest_asyncio
from asyncpg import CheckViolationError

from catbot.db import Database

pytestmark = pytest.mark.postgres


@pytest_asyncio.fixture
async def db():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to an empty PostgreSQL test database")
    database = await Database.connect(url)
    # No destructive reset of existing tables. Refuse to use populated databases.
    exists = await database.pool.fetchval("SELECT to_regclass('public.users')")
    if exists:
        await database.close()
        pytest.fail("Test database must be empty: create a NEW test database/Neon branch")
    await database.migrate()
    await database.bind_bot(123)
    try:
        yield database
    finally:
        # Only tables made by this fixture in the explicitly configured empty test DB.
        await database.pool.execute("""
            DROP TABLE dialog_states, admin_sessions, promo_redemptions, subscription_rewards, promo_codes,
                       update_receipts, rolls, user_cats, cats, users, bot_meta, schema_versions
        """)
        await database.close()


async def seed(db):
    for n in range(6):
        await db.add_cat(f"c{n}", f"Кот {n}", "COM", f"file{n}")


async def test_parallel_spins_never_overspend(db):
    await seed(db)
    results = await asyncio.gather(*(db.roll(i, 10, 3) for i in range(10)))
    assert sum("error" not in r for r in results) == 3
    profile = await db.profile(10, 3)
    assert (profile["spins"], profile["owned"], profile["score"]) == (0, 3, 3)


async def test_same_update_spends_once_and_returns_same_card(db):
    await seed(db)
    results = await asyncio.gather(*(db.roll(100, 10, 3) for _ in range(10)))
    assert len({r["cat_id"] for r in results}) == 1
    assert (await db.profile(10, 3))["spins"] == 2


async def test_parallel_daily_grants_once(db):
    results = await asyncio.gather(*(db.daily(10, date(2026, 10, 17), 3, 3, 999) for _ in range(8)))
    assert sum(added for added, _ in results) == 3
    assert (await db.profile(10, 3))["spins"] == 6


async def test_roll_transaction_rolls_back_after_midway_failure(db):
    await seed(db)
    # Force an error on final history insertion after collection and balance updates.
    await db.pool.execute("ALTER TABLE rolls ADD CONSTRAINT reject_test CHECK (points<0)")
    with pytest.raises(CheckViolationError):
        await db.roll(1, 10, 3)
    profile = await db.profile(10, 3)
    assert (profile["spins"], profile["owned"], profile["score"]) == (3, 0, 0)


async def test_completed_collection_does_not_spend(db):
    await db.add_cat("only", "Кот", "COM", "file")
    await db.roll(1, 10, 3)
    assert await db.roll(2, 10, 3) == {"error": "complete"}
    assert (await db.profile(10, 3))["spins"] == 2


async def test_bot_binding_migrations_and_receipts(db):
    await db.migrate()
    with pytest.raises(RuntimeError):
        await db.bind_bot(999)
    assert await db.claim_update(1) == "claimed"
    assert await db.claim_update(1) == "busy"
    await db.release_update(1)
    assert await db.claim_update(1) == "claimed"
    await db.finish_update(1)
    assert await db.claim_update(1) == "done"


async def test_parallel_promo_is_granted_only_once(db):
    await db.create_promo("CATS", 5, "Привет", 123)
    results = await asyncio.gather(*(db.redeem_promo(10, "CATS", 3, 999) for _ in range(8)))
    assert sum(r.get("added", 0) for r in results) == 5
    assert (await db.profile(10, 3))["spins"] == 8


async def test_disabled_code_cannot_be_redeemed_or_overwritten(db):
    await db.create_promo("CATS", 5, "Привет", 123)
    await db.disable_promo("CATS")
    assert await db.create_promo("CATS", 100, "Изменено", 123) is None
    assert await db.redeem_promo(10, "CATS", 3, 999) == {"error": "invalid"}


async def test_parallel_subscription_reward_is_granted_once(db):
    results = await asyncio.gather(*(db.claim_subscription(10, "@cats_band", 3, 3, 999) for _ in range(8)))
    assert sum(r.get("added", 0) for r in results) == 3
    assert (await db.profile(10, 3))["spins"] == 6


async def test_promo_remains_available_at_full_balance(db):
    await db.create_promo("CATS", 5, "", 123)
    assert await db.redeem_promo(10, "CATS", 999, 999) == {"error": "full"}
    assert not await db.pool.fetchval("SELECT count(*) FROM promo_redemptions")


async def test_streak_and_daily_are_atomic(db):
    day = date(2026, 10, 17)
    results = await asyncio.gather(*(db.daily_reward(10, day, 3, 3, 999) for _ in range(8)))
    assert sum(r["added"] for r in results) == 1
    assert (await db.profile(10, 3))["streak"] == 1


async def test_admin_and_dialog_expiration(db):
    await db.enter_admin(123)
    await db.set_dialog(123, "admin_addcat")
    assert await db.admin_active(123)
    assert await db.dialog(123) == "admin_addcat"
    await db.pool.execute("UPDATE admin_sessions SET expires_at=now()-interval '1 second'")
    assert not await db.admin_active(123)
    await db.exit_admin(123)
    assert await db.dialog(123) is None


async def test_late_navigation_clear_does_not_erase_newer_prompt(db):
    await db.set_dialog(123, "nick", update_id=20)
    await db.clear_dialog(123, update_id=10)
    assert await db.dialog(123) == "nick"
    await db.set_dialog(123, "promo", update_id=15)
    assert await db.dialog(123) == "nick"
    await db.clear_dialog(123, update_id=21)
    await db.set_dialog(123, "nick", update_id=20)
    assert await db.dialog(123) is None


async def test_expired_dialog_accepts_randomized_update_sequence_after_long_idle(db):
    await db.set_dialog(123, "nick", update_id=999999)
    await db.pool.execute("UPDATE dialog_states SET expires_at=now()-interval '1 second'")
    await db.set_dialog(123, "promo", update_id=1)
    assert await db.dialog(123) == "promo"


async def test_profile_cache_does_not_control_balance_and_invalidates_on_change(db):
    await seed(db)
    assert (await db.profile(123, 3))["spins"] == 3
    await db.pool.execute("UPDATE users SET spins=0 WHERE telegram_id=123")
    assert await db.roll(1, 123, 3) == {"error": "no_spins"}
    assert (await db.profile(123, 3))["spins"] == 0
    await db.set_nickname(123, "Новое имя", 3)
    assert (await db.profile(123, 3))["nickname"] == "Новое имя"
