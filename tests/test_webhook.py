import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from aiogram.types import Update
from aiohttp import web

from catbot.app import BusyUpdate, Processor, make_web_app
from catbot.config import Settings

SETTINGS = Settings(
    "123456:test",
    "postgresql://db/cats",
    mode="webhook",
    webhook_base_url="https://bot.test",
    webhook_secret="a" * 40,
)


def processor():
    db = AsyncMock()
    db.claim_update.return_value = "claimed"
    dispatcher = AsyncMock()
    return Processor(AsyncMock(), dispatcher, db, SETTINGS)


async def test_receipt_completes_only_after_handler():
    p = processor()
    order = []

    async def feed(*args, **kwargs):
        order.append("handler")

    async def finish(*args):
        order.append("done")

    p.dispatcher.feed_update.side_effect = feed
    p.db.finish_update.side_effect = finish
    await p.process(Update(update_id=1))
    assert order == ["handler", "done"]


async def test_failed_handler_leaves_update_retryable():
    p = processor()
    p.dispatcher.feed_update.side_effect = RuntimeError("network failure")
    with pytest.raises(RuntimeError):
        await p.process(Update(update_id=2))
    p.db.finish_update.assert_not_awaited()
    p.db.release_update.assert_awaited_once_with(2)


async def test_done_update_is_not_dispatched_twice():
    p = processor()
    p.db.claim_update.return_value = "done"
    await p.process(Update(update_id=3))
    p.dispatcher.feed_update.assert_not_awaited()


async def test_busy_duplicate_is_not_acknowledged():
    p = processor()
    p.db.claim_update.return_value = "busy"
    with pytest.raises(BusyUpdate):
        await p.process(Update(update_id=4))
    p.db.release_update.assert_not_awaited()


async def test_different_updates_really_process_concurrently():
    p = processor()
    both_started = asyncio.Event()
    count = 0

    async def feed(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=1)

    p.dispatcher.feed_update.side_effect = feed
    await asyncio.gather(p.process(Update(update_id=5)), p.process(Update(update_id=6)))
    assert count == 2


class Request:
    def __init__(self, secret="a" * 40, payload=None):
        self.headers = {"X-Telegram-Bot-Api-Secret-Token": secret}
        self.payload = payload if payload is not None else {"update_id": 1}

    async def json(self):
        return self.payload


def route(app, method, path):
    return next(r.handler for r in app.router.routes() if r.method == method and r.resource.canonical == path)


async def test_webhook_rejects_bad_secret_without_processing():
    p = AsyncMock()
    handler = route(make_web_app(p, SETTINGS), "POST", "/telegram/webhook")
    with pytest.raises(web.HTTPForbidden):
        await handler(Request(secret="wrong"))
    p.process.assert_not_awaited()


async def test_webhook_invalid_payload_is_400():
    handler = route(make_web_app(AsyncMock(), SETTINGS), "POST", "/telegram/webhook")
    with pytest.raises(web.HTTPBadRequest):
        await handler(Request(payload={"update_id": "not-a-number"}))


async def test_webhook_does_not_acknowledge_processing_failure():
    p = AsyncMock()
    p.process.side_effect = RuntimeError("sensitive connection details")
    handler = route(make_web_app(p, SETTINGS), "POST", "/telegram/webhook")
    response = await handler(Request())
    assert response.status == 503
    assert "sensitive" not in (response.text or "")


async def test_webhook_acknowledges_success():
    handler = route(make_web_app(AsyncMock(), SETTINGS), "POST", "/telegram/webhook")
    response = await handler(Request())
    assert response.status == 200
    assert json.loads(response.text) == {"ok": True}


async def test_health_does_not_touch_neon():
    p = processor()
    response = await route(make_web_app(p, SETTINGS), "GET", "/healthz")(Request())
    assert response.status == 200
    p.db.claim_update.assert_not_awaited()
