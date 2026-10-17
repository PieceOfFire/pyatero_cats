import asyncio
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message, Update

from catbot import keyboards as kb
from catbot.config import Settings
from catbot.db import Database
from catbot.domain import LABELS, parse_promo, promo_code, validate_cat
from catbot.runtime import current_update_id, is_navigation

menu = kb.menu
logger = logging.getLogger(__name__)


async def render(message: Message, text: str, markup=None, edit=False):
    started = time.perf_counter()
    if edit and message.text is not None:
        try:
            await message.edit_text(text, reply_markup=markup)
            logger.info("Menu rendered elapsed_ms=%d", (time.perf_counter() - started) * 1000)
            return
        except TelegramBadRequest as error:
            if "message is not modified" in str(error).lower():
                return
    await message.answer(text, reply_markup=markup)
    logger.info("Menu rendered elapsed_ms=%d", (time.perf_counter() - started) * 1000)


async def send_card(bot: Bot, chat_id: int, card, settings: Settings):
    caption = (
        f"{LABELS[card['rarity']]} · #{card['cat_id']}\n{card['description']}\n\n"
        f"⭐ +{card['points']} · Спинов после выдачи: {card['spins_after']}"
    )
    try:
        # Main menu is a separate text message so inline navigation can edit it.
        await bot.send_photo(chat_id, card["telegram_file_id"], caption=caption)
    except TelegramBadRequest:
        await bot.send_message(
            chat_id,
            "Карточка сохранена в коллекции, но фото сейчас недоступно. "
            "Сообщи администратору. Повторная отправка: /last — без списания спина.",
        )


def create_router() -> Router:
    router = Router()
    router.message.filter(F.chat.type == ChatType.PRIVATE)
    router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)

    async def require_admin(message, user_id, db, settings):
        if not settings.is_admin(user_id):
            await message.answer("⛔ Эта функция доступна только администратору.")
            return False
        if not await db.admin_active(user_id):
            await message.answer("🔐 Сначала включи админский режим: /admin")
            return False
        return True

    async def main_menu(message, user_id, db, settings, edit=False, note=""):
        profile = await db.profile(user_id, settings.initial_spins)
        nick = profile["nickname"] or f"#{str(user_id)[-6:]}"
        text = (
            f"🏠 Главное меню · {settings.title}\nИмя пользователя: {nick}\n\n"
            f"💰 Баланс: {profile['spins']} спинов\nВыберите действие:"
        )
        if note:
            text += f"\n\n{note}"
        # The entry button depends only on the allowlist. Privileged actions still
        # check the durable admin session; drawing a menu needs no session query.
        await render(message, text, menu(settings, user_id), edit)

    async def profile(message, user_id, db, settings, edit=False):
        record = await db.profile(user_id, settings.initial_spins)
        await render(
            message,
            (
                f"⭐ Профиль · {record['nickname'] or 'Котолюб'}\n\n"
                f"💰 Спинов: {record['spins']}\n⭐ Очков: {record['score']}\n"
                f"📚 В коллекции: {record['owned']} · Активных карточек: {record['total']}\n"
                f"🔥 Стрик: {record['streak']}"
            ),
            kb.back(),
            edit,
        )

    @router.message(CommandStart())
    @router.message(Command("menu", "cancel"))
    async def start(message: Message, db: Database, settings: Settings):
        await db.clear_dialog(message.from_user.id)
        await main_menu(message, message.from_user.id, db, settings)

    @router.message(Command("profile"))
    async def profile_command(message: Message, db: Database, settings: Settings):
        await profile(message, message.from_user.id, db, settings)

    @router.message(Command("help"))
    async def help_command(message: Message, settings: Settings):
        text = (
            "/start — главное меню\n/cat — получить котёнка\n/daily — ежедневная награда\n"
            "/rewards — награды\n/promo КОД — активировать промокод\n"
            "/profile — профиль\n/collection — коллекция\n/last — повторить последнее фото\n"
            "/top — лидерборд\n/nick имя — сменить ник\n/id — твой ID\n/cancel — отменить ввод"
        )
        if settings.is_admin(message.from_user.id):
            text += (
                "\n\n/admin — вход в админский режим\n/adminoff — выход\n"
                "/addcat ID | RARITY | описание — подпись к фото\n"
                "/promocreate КОД | спины | описание — новый промокод"
            )
        await message.answer(text, reply_markup=kb.back())

    @router.message(Command("id"))
    async def identity(message: Message):
        await message.answer(f"Твой ID: {message.from_user.id}\nID чата: {message.chat.id}")

    async def save_nick(message, user_id, raw, db, settings, edit=False, username=False):
        nick = " ".join(raw.split())
        if not 1 <= len(nick) <= 60 or ("@" in nick and not username):
            await render(message, "Ник: 1–60 символов, без @. Попробуй ещё раз.", kb.back(), edit)
            return
        await db.set_nickname(user_id, nick, settings.initial_spins)
        await db.clear_dialog(user_id, "nick")
        await main_menu(message, user_id, db, settings, edit, f"✨ Ник изменён: {nick}")

    @router.message(Command("nick"))
    async def nickname_command(message: Message, command: CommandObject, db: Database, settings: Settings):
        if command.args:
            await save_nick(message, message.from_user.id, command.args, db, settings)
        else:
            await message.answer("✏️ Как установить ник?", reply_markup=kb.nickname())

    async def spin(message, user_id, update_id, bot, db, settings):
        result = await db.roll(update_id, user_id, settings.initial_spins)
        errors = {
            "no_spins": "Спины закончились. Загляни в «Награды»!",
            "empty": "Котята ещё готовятся к встрече! Администратор скоро добавит карточки.",
            "complete": "Ты собрал всех доступных котят! Спин сохранён. Ждём новых карточек 🐾",
        }
        if "error" not in result:
            await send_card(bot, message.chat.id, result, settings)
        await main_menu(message, user_id, db, settings, note=errors.get(result.get("error"), ""))

    @router.message(Command("cat", "spin"))
    async def cat(message: Message, event_update: Update, bot: Bot, db: Database, settings: Settings):
        await spin(message, message.from_user.id, event_update.update_id, bot, db, settings)

    async def daily(message, user_id, db, settings, edit=False):
        result = await db.daily_reward(
            user_id,
            datetime.now(ZoneInfo(settings.timezone)).date(),
            settings.initial_spins,
            settings.daily_spins,
            settings.max_spins,
            settings.daily_reward_mode,
        )
        note = (
            f"🎁 Ежедневная награда: +{result['added']} спинов!"
            if result["claimed"]
            else "🐾 Ты уже получил ежедневную награду сегодня!"
        )
        note += f"\n🔥 Твой стрик: {result['streak']}"
        if settings.daily_reward_mode == "streak":
            note += "\nДни 1–3: +1, дни 4–6: +2, с 7-го дня: +3."
        await main_menu(message, user_id, db, settings, edit, note)

    @router.message(Command("daily"))
    async def daily_command(message: Message, db: Database, settings: Settings):
        await daily(message, message.from_user.id, db, settings)

    @router.message(Command("rewards"))
    async def rewards_command(message: Message, settings: Settings):
        await message.answer("🎁 Меню наград\n\nВыбери:", reply_markup=kb.rewards(settings))

    async def redeem(message, user_id, raw, db, settings, edit=False):
        try:
            code = promo_code(raw)
        except ValueError as error:
            await render(message, str(error), kb.back("rewards"), edit)
            return
        result = await db.redeem_promo(user_id, code, settings.initial_spins, settings.max_spins)
        texts = {
            "invalid": "❌ Неверный или отключённый промокод.",
            "used": "🚫 Ты уже использовал этот промокод.",
            "full": "Баланс достиг лимита. Промокод не потрачен.",
        }
        note = texts.get(result.get("error"))
        if note is None:
            note = f"{result['description']}\n🎉 +{result['added']} спинов!".strip()
        await db.clear_dialog(user_id, "promo")
        await main_menu(message, user_id, db, settings, edit, note)

    @router.message(Command("promo"))
    async def promo_command(message: Message, command: CommandObject, db: Database, settings: Settings):
        if command.args:
            await redeem(message, message.from_user.id, command.args, db, settings)
        else:
            await db.set_dialog(message.from_user.id, "promo")
            await message.answer("✏️ Введи промокод одним сообщением:", reply_markup=kb.back("rewards"))

    async def collection(message, user_id, offset, db, edit=False):
        rows, count = await db.collection(user_id, offset)
        text = f"📚 Коллекция: {count}\n\n" + (
            "\n".join(f"{LABELS[r['rarity']]} · #{r['id']} — {r['description'][:100]}" for r in rows)
            or "Здесь пока пусто. Получи первого котёнка: /cat"
        )
        buttons = []
        if offset:
            buttons.append(kb.button("←", f"collection:{max(0, offset - 10)}"))
        if offset + 10 < count:
            buttons.append(kb.button("→", f"collection:{offset + 10}"))
        keyboard = ([buttons] if buttons else []) + [[kb.button("⬅️ Назад", "back_main")]]
        await render(message, text, InlineKeyboardMarkup(inline_keyboard=keyboard), edit)

    @router.message(Command("collection"))
    async def collection_command(message: Message, db: Database):
        await collection(message, message.from_user.id, 0, db)

    @router.message(Command("last"))
    async def last(message: Message, bot: Bot, db: Database, settings: Settings):
        card = await db.last_roll(message.from_user.id)
        if card:
            await send_card(bot, message.chat.id, card, settings)
        await main_menu(message, message.from_user.id, db, settings, note="" if card else "Выдач пока нет.")

    async def top(message, db, edit=False):
        rows = await db.top()
        lines = [f"{i}. {r['nickname'][:60] or 'Котолюб'} — {r['score']} ⭐" for i, r in enumerate(rows, 1)]
        await render(message, "🏆 Лидерборд\n\n" + ("\n".join(lines) or "Пока пусто"), kb.back(), edit)

    @router.message(Command("top"))
    async def top_command(message: Message, db: Database):
        await top(message, db)

    async def enter_admin(message, user_id, db, settings, edit=False):
        if not settings.is_admin(user_id):
            await render(message, "⛔ Админский режим недоступен для твоего Telegram ID.", kb.back(), edit)
            return
        await db.enter_admin(user_id)
        await db.clear_dialog(user_id)
        await render(
            message, "⚙️ Админский режим включён на 12 часов.\nВыбери действие:", kb.admin_panel(), edit
        )

    @router.message(Command("admin"))
    async def admin_command(message: Message, db: Database, settings: Settings):
        await enter_admin(message, message.from_user.id, db, settings)

    @router.message(Command("adminoff"))
    async def admin_off(message: Message, db: Database, settings: Settings):
        await db.exit_admin(message.from_user.id)
        await main_menu(message, message.from_user.id, db, settings, note="Админский режим выключен.")

    async def register_cat(message, user_id, raw, db, settings):
        if not await require_admin(message, user_id, db, settings):
            return
        photo_message = message if message.photo else message.reply_to_message
        if not photo_message or not photo_message.photo:
            await message.answer(
                "Пришли фото с подписью ID | COM | описание или /addcat ID | COM | описание."
            )
            return
        try:
            parts = raw.split("|", 2)
            if len(parts) != 3:
                raise ValueError("Формат подписи: ID | COM | описание")
            cat_id, description, rarity = validate_cat(parts[0], parts[2], parts[1])
            photo = photo_message.photo[-1]
            await db.add_cat(cat_id, description, rarity, photo.file_id, photo.file_unique_id)
        except ValueError as error:
            await message.answer(str(error))
            return
        await db.clear_dialog(user_id, "admin_addcat")
        await message.answer(
            f"Карточка #{cat_id} сохранена: {LABELS[rarity]}.", reply_markup=kb.admin_panel()
        )

    @router.message(Command("addcat"))
    async def addcat(message: Message, command: CommandObject, db: Database, settings: Settings):
        await register_cat(message, message.from_user.id, command.args or "", db, settings)

    async def create_promo(message, user_id, raw, db, settings):
        if not await require_admin(message, user_id, db, settings):
            return
        try:
            code, bonus, description = parse_promo(raw, settings.max_spins)
        except ValueError as error:
            await message.answer(str(error))
            return
        created = await db.create_promo(code, bonus, description, user_id)
        await db.clear_dialog(user_id, "admin_promo_create")
        await message.answer(
            f"🎟 Промокод {code}: +{bonus} спинов, один раз на пользователя."
            if created
            else "Такой промокод уже существует. Он не изменён. Используй новый код.",
            reply_markup=kb.admin_panel(),
        )

    @router.message(Command("promocreate"))
    async def promocreate(message: Message, command: CommandObject, db: Database, settings: Settings):
        await create_promo(message, message.from_user.id, command.args or "", db, settings)

    async def check_subscription(query, bot, db, settings):
        if not settings.bonus_channel:
            await render(query.message, "Награда за подписку пока не настроена.", kb.back("rewards"), True)
            return
        channel = (
            int(settings.bonus_channel)
            if settings.bonus_channel.startswith("-100")
            else settings.bonus_channel
        )
        try:
            own_member = await bot.get_chat_member(channel, bot.id)
            if own_member.status not in {"administrator", "creator"}:
                await render(
                    query.message,
                    "Проверка подписки временно недоступна. Сообщи администратору.",
                    kb.back("rewards"),
                    True,
                )
                return
            member = await bot.get_chat_member(channel, query.from_user.id)
        except (TelegramBadRequest, TelegramForbiddenError):
            await render(
                query.message,
                "Не удалось проверить подписку. Сообщи администратору.",
                kb.back("rewards"),
                True,
            )
            return
        subscribed = member.status in {"member", "administrator", "creator"} or (
            member.status == "restricted" and getattr(member, "is_member", False)
        )
        if not subscribed:
            await render(
                query.message,
                "Подпишись на канал и нажми «Проверить подписку».",
                kb.subscription(settings),
                True,
            )
            return
        result = await db.claim_subscription(
            query.from_user.id,
            settings.bonus_channel,
            settings.initial_spins,
            settings.subscription_spins,
            settings.max_spins,
        )
        note = {
            "used": "Ты уже получал награду за подписку на этот канал.",
            "full": "Баланс достиг лимита. Награда сохранена на потом.",
        }.get(result.get("error"))
        if note is None:
            note = f"🎉 Спасибо за подписку! +{result['added']} спинов."
        await main_menu(query.message, query.from_user.id, db, settings, True, note)

    async def answer_navigation(query):
        try:
            await query.answer()
        except TelegramAPIError:
            # A failed/expired cosmetic callback answer must not block the menu.
            pass

    async def navigation_view(query, db, settings):
        data, user_id, msg = query.data, query.from_user.id, query.message
        if data == "back_main":
            await main_menu(msg, user_id, db, settings, True)
        elif data == "rewards":
            await render(msg, "🎁 Меню наград\n\nВыбери:", kb.rewards(settings), True)
        elif data == "change_nick":
            await render(msg, "✏️ Как установить ник?", kb.nickname(), True)
        elif data == "nick_manual":
            await render(msg, "✏️ Введи новый ник одним сообщением, без @:", kb.back(), True)
        elif data == "promo_enter":
            await render(msg, "✏️ Введи промокод одним сообщением:", kb.back("rewards"), True)
        elif data == "reward_sub":
            await render(
                msg, "📢 Подпишись на канал и забери награду один раз.", kb.subscription(settings), True
            )
        elif data == "profile":
            await profile(msg, user_id, db, settings, True)
        elif data in {"top", "leaderboard"}:
            await top(msg, db, True)
        else:
            await collection(msg, user_id, int(data.partition(":")[2]), db, True)

    async def navigate(query, event_update, db, settings):
        # Persist dialog transitions concurrently with the visible response. The
        # SQL update_id guard prevents an older clear from erasing a newer prompt.
        context = current_update_id.set(event_update.update_id)
        try:
            async with asyncio.TaskGroup() as group:
                group.create_task(navigation_view(query, db, settings))
                group.create_task(answer_navigation(query))
                state = {"nick_manual": "nick", "promo_enter": "promo"}.get(query.data)
                if state:
                    group.create_task(db.set_dialog(query.from_user.id, state))
                else:
                    group.create_task(db.clear_dialog(query.from_user.id))
        finally:
            current_update_id.reset(context)

    @router.callback_query()
    async def callback(
        query: CallbackQuery, event_update: Update, bot: Bot, db: Database, settings: Settings
    ):
        data, user_id, msg = query.data or "", query.from_user.id, query.message
        if is_navigation(event_update):
            await navigate(query, event_update, db, settings)
            return
        # Verify privileges on every sensitive callback, even if the button was forged.
        if data.startswith("admin_") and data not in {"admin_open", "admin_exit"}:
            if not await require_admin(msg, user_id, db, settings):
                try:
                    await query.answer("Нет доступа", show_alert=True)
                except TelegramBadRequest:
                    pass
                return
        try:
            await query.answer()
        except TelegramBadRequest:
            pass
        # Navigating with any old menu cancels pending input before a new prompt is set.
        await db.clear_dialog(user_id)
        if data == "spin":
            await spin(msg, user_id, event_update.update_id, bot, db, settings)
        elif data in {"daily", "reward_daily"}:
            await daily(msg, user_id, db, settings, True)
        elif data == "nick_use_username":
            if query.from_user.username:
                await save_nick(msg, user_id, f"@{query.from_user.username}", db, settings, True, True)
            else:
                await render(msg, "У тебя нет @username. Введи ник вручную.", kb.nickname(), True)
        elif data == "subscription_check":
            await check_subscription(query, bot, db, settings)
        elif data == "admin_open":
            await enter_admin(msg, user_id, db, settings, True)
        elif data == "admin_exit":
            await db.exit_admin(user_id)
            await main_menu(msg, user_id, db, settings, True, "Админский режим выключен.")
        elif data in {
            "admin_addcat",
            "admin_hidecat",
            "admin_showcat",
            "admin_promo_create",
            "admin_promo_disable",
        }:
            prompts = {
                "admin_addcat": "Пришли фото с подписью ID | COM | описание. Тот же ID обновляет карточку.",
                "admin_hidecat": "Введи ID карточки: она исчезнет из новых выдач, коллекции сохранятся.",
                "admin_showcat": "Введи ID карточки, которую нужно вернуть в выдачи.",
                "admin_promo_create": "Введи: КОД | количество спинов | описание",
                "admin_promo_disable": "Введи промокод для отключения. Уже выданные награды сохранятся.",
            }
            await db.set_dialog(user_id, data)
            await render(msg, prompts[data], kb.back("admin_open"), True)
        elif data == "admin_promos":
            rows = await db.promos()
            lines = [
                f"{'✅' if r['active'] else '⛔'} {r['code']} · +{r['bonus']} · получено: {r['used']}"
                for r in rows
            ]
            await render(
                msg,
                "🎟 Последние 20 промокодов\n\n" + ("\n".join(lines) or "Пока пусто"),
                kb.back("admin_open"),
                True,
            )
        elif data == "admin_stats":
            stat = await db.stats()
            await render(
                msg,
                f"📊 Статистика\nПользователей: {stat['users']}\n"
                f"Активных карточек: {stat['cats']}\nВыдач: {stat['rolls']}\n"
                f"Активаций промокодов: {stat['redemptions']}",
                kb.back("admin_open"),
                True,
            )
        else:
            await render(msg, "Эта кнопка устарела. Открой главное меню.", kb.back(), True)

    @router.message(F.text | F.photo)
    async def dialog_input(message: Message, db: Database, settings: Settings):
        user_id = message.from_user.id
        state = await db.dialog(user_id)
        raw = message.text or message.caption or ""
        if not state:
            await main_menu(message, user_id, db, settings, note="Выбери действие кнопками ниже.")
            return
        if state.startswith("admin_") and not await require_admin(message, user_id, db, settings):
            return
        if state == "nick":
            await save_nick(message, user_id, raw, db, settings)
        elif state == "promo":
            await redeem(message, user_id, raw, db, settings)
        elif state == "admin_addcat":
            await register_cat(message, user_id, raw, db, settings)
        elif state == "admin_promo_create":
            await create_promo(message, user_id, raw, db, settings)
        elif state in {"admin_hidecat", "admin_showcat", "admin_promo_disable"}:
            try:
                if state == "admin_promo_disable":
                    found = await db.disable_promo(promo_code(raw))
                else:
                    cat_id = raw.strip()
                    if not cat_id or len(cat_id) > 100:
                        raise ValueError("Введи ID карточки")
                    found = await db.set_cat_active(cat_id, state == "admin_showcat")
            except ValueError as error:
                await message.answer(str(error))
                return
            await db.clear_dialog(user_id, state)
            await message.answer(
                "✅ Сохранено." if found else "Не найдено. Проверь ID или код.", reply_markup=kb.admin_panel()
            )

    return router
