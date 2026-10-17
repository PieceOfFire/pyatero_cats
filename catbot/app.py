import asyncio
import contextlib
import hmac
import logging
import time

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.types import BotCommand, Update
from aiohttp import web
from pydantic import ValidationError

from catbot.config import Settings
from catbot.db import Database
from catbot.handlers import create_router
from catbot.runtime import TTLCache, current_update_id, is_navigation

logger = logging.getLogger(__name__)


class BusyUpdate(Exception):
    pass


class Processor:
    def __init__(self, bot: Bot, dispatcher: Dispatcher, db: Database, settings: Settings):
        self.bot, self.dispatcher, self.db, self.settings = bot, dispatcher, db, settings
        self.slots = asyncio.Semaphore(settings.max_concurrent_updates)
        self.navigation_done = TTLCache(ttl=300, capacity=4096)
        self.navigation_running = set()
        self.navigation_pending = {}

    async def process(self, update: Update):
        try:
            await asyncio.wait_for(self.slots.acquire(), timeout=1)
        except TimeoutError as error:
            raise BusyUpdate from error
        claimed = False
        navigation = is_navigation(update)
        started = time.perf_counter()
        context = current_update_id.set(update.update_id)
        pending = None
        user_id = None
        try:
            async with asyncio.timeout(45):
                if navigation:
                    # Redrawing a menu needs no durable economic-operation receipt.
                    # Cross-restart retries are harmless; dialog versions preserve order.
                    if self.navigation_done.get(update.update_id):
                        return
                    if update.update_id in self.navigation_running:
                        raise BusyUpdate
                    self.navigation_running.add(update.update_id)
                    user_id = update.callback_query.from_user.id
                    pending = asyncio.Event()
                    self.navigation_pending.setdefault(user_id, set()).add(pending)
                    try:
                        await self.dispatcher.feed_update(
                            self.bot, update, db=self.db, settings=self.settings
                        )
                        self.navigation_done.put(update.update_id, True)
                    finally:
                        self.navigation_running.discard(update.update_id)
                    return
                if update.message and update.message.from_user:
                    # A player can type as soon as a prompt is painted, while its
                    # state is still being persisted. Wait before consuming text.
                    pending_states = tuple(self.navigation_pending.get(update.message.from_user.id, ()))
                    if pending_states:
                        await asyncio.gather(*(event.wait() for event in pending_states))
                state = await self.db.claim_update(update.update_id)
                if state == "done":
                    return
                if state == "busy":
                    raise BusyUpdate
                claimed = True
                await self.dispatcher.feed_update(self.bot, update, db=self.db, settings=self.settings)
                await self.db.finish_update(update.update_id)
        except BaseException:
            if claimed:
                # On process death the lease expires instead. No 200 is returned on failure.
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self.db.release_update(update.update_id), timeout=5)
            raise
        finally:
            if pending is not None:
                pending.set()
                events = self.navigation_pending.get(user_id)
                if events is not None:
                    events.discard(pending)
                    if not events:
                        self.navigation_pending.pop(user_id, None)
            current_update_id.reset(context)
            self.slots.release()
            logger.info(
                "Update kind=%s elapsed_ms=%d",
                "navigation" if navigation else "durable",
                (time.perf_counter() - started) * 1000,
            )


def make_web_app(processor: Processor, settings: Settings) -> web.Application:
    app = web.Application(client_max_size=1024 * 1024)

    async def health(request):
        # Intentionally no DB query: health checks must not keep Neon compute awake.
        return web.json_response({"status": "ok"})

    async def webhook(request):
        secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not secret or not hmac.compare_digest(secret.encode(), settings.webhook_secret.encode()):
            raise web.HTTPForbidden()
        try:
            update = Update.model_validate(await request.json())
        except (ValueError, ValidationError):
            raise web.HTTPBadRequest() from None
        try:
            await processor.process(update)
        except BusyUpdate:
            return web.Response(status=503, headers={"Retry-After": "3"})
        except Exception as error:
            # Never log payloads, tokens, DSNs or exception text containing URLs.
            logger.error("Update %s failed (%s); Telegram may retry", update.update_id, type(error).__name__)
            return web.Response(status=503, headers={"Retry-After": "5"})
        return web.json_response({"ok": True})

    app.router.add_get("/healthz", health)
    app.router.add_post("/telegram/webhook", webhook)
    return app


async def initialize(settings: Settings):
    bot = Bot(settings.bot_token, session=AiohttpSession(timeout=30))
    db = await Database.connect(settings.database_url, settings.db_pool_max)
    try:
        await db.migrate()
        me = await bot.get_me()
        await db.bind_bot(me.id)
        dispatcher = Dispatcher()
        dispatcher.include_router(create_router())
        return bot, db, dispatcher, Processor(bot, dispatcher, db, settings)
    except BaseException:
        await db.close()
        await bot.session.close()
        raise


async def set_commands(bot: Bot):
    await bot.set_my_commands(
        [
            BotCommand(command="cat", description="Получить котёнка"),
            BotCommand(command="start", description="Главное меню"),
            BotCommand(command="rewards", description="Награды и промокоды"),
            BotCommand(command="daily", description="Ежедневные спины"),
            BotCommand(command="profile", description="Профиль"),
            BotCommand(command="collection", description="Коллекция"),
            BotCommand(command="last", description="Повторить последнее фото"),
            BotCommand(command="top", description="Рейтинг"),
            BotCommand(command="help", description="Помощь"),
        ]
    )


def run_webhook(settings: Settings):
    # Lifecycle initialization is awaited before health endpoint is served.
    async def app_factory():
        bot, db, dispatcher, processor = await initialize(settings)
        app = make_web_app(processor, settings)

        async def cleanup(application):
            # Keep the webhook registered across Render restarts and sleeping periods.
            await bot.session.close()
            await db.close()
            await dispatcher.storage.close()

        app.on_cleanup.append(cleanup)
        try:
            await bot.set_webhook(
                f"{settings.webhook_base_url}/telegram/webhook",
                secret_token=settings.webhook_secret,
                allowed_updates=["message", "callback_query"],
                max_connections=settings.max_concurrent_updates,
                drop_pending_updates=False,
            )
            await set_commands(bot)
        except BaseException:
            await cleanup(app)
            raise
        return app

    web.run_app(app_factory(), host="0.0.0.0", port=settings.port, access_log=None, shutdown_timeout=55)


async def run_polling(settings: Settings):
    bot, db, dispatcher, processor = await initialize(settings)
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await set_commands(bot)
        offset = None
        while True:
            try:
                updates = await bot.get_updates(
                    offset=offset, timeout=20, limit=20, allowed_updates=["message", "callback_query"]
                )
                results = await asyncio.gather(
                    *(processor.process(u) for u in updates), return_exceptions=True
                )
                if any(isinstance(result, BaseException) for result in results):
                    logger.warning("Polling batch not acknowledged; retry in 3 seconds")
                    await asyncio.sleep(3)
                    continue
                if updates:
                    offset = max(u.update_id for u in updates) + 1
            except Exception as error:
                logger.warning("Polling temporarily unavailable (%s)", type(error).__name__)
                await asyncio.sleep(3)
    finally:
        await dispatcher.storage.close()
        await bot.session.close()
        await db.close()
