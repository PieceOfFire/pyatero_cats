import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import Update

from catbot.app import Processor
from catbot.config import Settings
from catbot.db import Database, direct_database_url
from catbot.runtime import NAVIGATION, TTLCache, current_update_id, is_navigation


def navigation(data, update_id=1, username=None):
    return Update.model_validate(
        {
            "update_id": update_id,
            "callback_query": {
                "id": str(update_id),
                "chat_instance": "test",
                "from": {"id": 123, "is_bot": False, "first_name": "Tester", "username": username},
                "data": data,
                "message": {"message_id": 1, "date": 0, "chat": {"id": 123, "type": "private"}},
            },
        }
    )


@pytest.mark.parametrize("data", [*NAVIGATION, "collection:0", "collection:20"])
def test_navigation_allowlist(data):
    assert is_navigation(navigation(data))


@pytest.mark.parametrize(
    "data",
    [
        "spin",
        "daily",
        "reward_daily",
        "subscription_check",
        "nick_use_username",
        "admin_open",
        "admin_stats",
        "admin_addcat",
        "collection:-1",
        "collection:999999999",
        "collection:٣",
        "promo_redeem",
    ],
)
def test_mutations_and_malformed_buttons_never_bypass_durable_receipts(data):
    assert not is_navigation(navigation(data))


async def test_navigation_uses_local_deduplication_without_database_receipts():
    db, dispatcher = AsyncMock(), AsyncMock()
    processor = Processor(AsyncMock(), dispatcher, db, Settings("token", "postgresql://db/cats"))
    await processor.process(navigation("rewards"))
    await processor.process(navigation("rewards"))
    dispatcher.feed_update.assert_awaited_once()
    db.claim_update.assert_not_awaited()
    db.finish_update.assert_not_awaited()


async def test_failed_navigation_is_retryable_and_cleans_context():
    db, dispatcher = AsyncMock(), AsyncMock()
    processor = Processor(AsyncMock(), dispatcher, db, Settings("token", "postgresql://db/cats"))
    dispatcher.feed_update.side_effect = [RuntimeError("failed"), None]
    with pytest.raises(RuntimeError):
        await processor.process(navigation("rewards"))
    await processor.process(navigation("rewards"))
    assert not processor.navigation_pending
    assert not processor.navigation_running
    assert current_update_id.get() == 0


async def test_text_waits_until_the_visible_prompt_is_persisted():
    db, dispatcher = AsyncMock(), AsyncMock()
    db.claim_update.return_value = "claimed"
    processor = Processor(AsyncMock(), dispatcher, db, Settings("token", "postgresql://db/cats"))
    prompt_started, persist_prompt, text_started = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def feed(bot, update, **kwargs):
        if update.callback_query:
            prompt_started.set()
            await persist_prompt.wait()
        else:
            assert persist_prompt.is_set()
            text_started.set()

    dispatcher.feed_update.side_effect = feed
    first = asyncio.create_task(processor.process(navigation("nick_manual")))
    second = None
    try:
        await asyncio.wait_for(prompt_started.wait(), 1)
        update = Update.model_validate(
            {
                "update_id": 2,
                "message": {
                    "message_id": 2,
                    "date": 0,
                    "chat": {"id": 123, "type": "private"},
                    "from": {"id": 123, "is_bot": False, "first_name": "Tester"},
                    "text": "Ник",
                },
            }
        )
        second = asyncio.create_task(processor.process(update))
        await asyncio.sleep(0)
        assert not text_started.is_set()
        persist_prompt.set()
        await asyncio.gather(first, second)
        assert text_started.is_set()
    finally:
        persist_prompt.set()
        await asyncio.gather(*[task for task in (first, second) if task is not None])


def test_display_cache_expires_and_has_bounded_capacity():
    now = [0.0]
    cache = TTLCache(ttl=15, capacity=2, clock=lambda: now[0])
    cache.put(1, "one")
    cache.put(2, "two")
    assert cache.get(1) == "one"
    cache.put(3, "three")
    assert cache.get(2) is None
    now[0] = 16
    assert cache.get(1) is None


async def test_profile_is_one_read_then_cache_and_does_not_lock_user():
    conn = AsyncMock()
    conn.fetchrow.return_value = {"telegram_id": 123, "spins": 3, "owned": 1, "total": 10}
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
    db = Database(pool)
    first = await db.profile(123, 3)
    first["spins"] = 900
    second = await db.profile(123, 3)
    assert second["spins"] == 3
    conn.fetchrow.assert_awaited_once()
    conn.execute.assert_not_awaited()
    assert "FOR UPDATE" not in conn.fetchrow.await_args.args[0]


def test_direct_migration_url_changes_only_neon_pooler_host():
    url = "postgresql://user:secret%40example@ep-test-pooler.eu.aws.neon.tech/cats?sslmode=require"
    assert direct_database_url(url) == url.replace("ep-test-pooler.", "ep-test.")
    assert direct_database_url("postgresql://u:p@localhost/cats") == "postgresql://u:p@localhost/cats"


async def test_identity_uses_callback_actor_and_caches_unchanged_username():
    db, dispatcher = AsyncMock(), AsyncMock()
    db.sync_telegram_user.return_value = True
    processor = Processor(AsyncMock(), dispatcher, db, Settings("token", "postgresql://db/cats"))
    first = navigation("change_nick", 10, "cat_owner")
    await processor.process(first)
    second = navigation("nick_manual", 11, "cat_owner")
    await processor.process(second)
    db.sync_telegram_user.assert_awaited_once_with(123, "cat_owner", 3, 10)
    changed = navigation("profile", 12, "new_owner")
    await processor.process(changed)
    assert db.sync_telegram_user.await_args.args == (123, "new_owner", 3, 12)
    await processor.process(first.model_copy(update={"update_id": 9}))
    assert db.sync_telegram_user.await_count == 2


async def test_start_synchronizes_username_before_rendering_profile():
    db, dispatcher = AsyncMock(), AsyncMock()
    db.claim_update.return_value = "claimed"
    db.sync_telegram_user.return_value = True
    processor = Processor(AsyncMock(), dispatcher, db, Settings("token", "postgresql://db/cats"))
    update = Update.model_validate(
        {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "date": 0,
                "chat": {"id": 123, "type": "private"},
                "from": {"id": 123, "is_bot": False, "first_name": "Cat", "username": "cat_owner"},
                "text": "/start",
            },
        }
    )

    async def feed(*args, **kwargs):
        db.sync_telegram_user.assert_awaited_once_with(123, "cat_owner", 3, 1)

    dispatcher.feed_update.side_effect = feed
    await processor.process(update)


async def test_failed_identity_sync_is_retryable():
    db, dispatcher = AsyncMock(), AsyncMock()
    db.sync_telegram_user.side_effect = [RuntimeError("unavailable"), True]
    processor = Processor(AsyncMock(), dispatcher, db, Settings("token", "postgresql://db/cats"))
    with pytest.raises(RuntimeError):
        await processor.process(navigation("profile"))
    dispatcher.feed_update.assert_not_awaited()
    await processor.process(navigation("profile"))
    assert db.sync_telegram_user.await_count == 2
