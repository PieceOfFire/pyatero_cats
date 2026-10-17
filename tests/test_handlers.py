from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, EditMessageText, GetChatMember, SendMessage, SendPhoto
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from catbot.config import Settings
from catbot.handlers import create_router


class MemoryTelegram(BaseSession):
    def __init__(self, member_status="member", bot_status="administrator"):
        super().__init__()
        self.requests = []
        self.member_status = member_status
        self.bot_status = bot_status

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):  # noqa: ASYNC109 -- BaseSession API
        self.requests.append(method)
        if isinstance(method, AnswerCallbackQuery):
            return True
        if isinstance(method, GetChatMember):
            status = self.bot_status if method.user_id == bot.id else self.member_status
            return SimpleNamespace(status=status, is_member=False)
        return Message(message_id=99, date=datetime.now(UTC), chat=Chat(id=123, type="private"))

    async def stream_content(self, *args, **kwargs):
        raise AssertionError("User handlers must never download image files")
        yield  # pragma: no cover


def message(text=None, caption=None, photo=None):
    return Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=123, type="private"),
        from_user=User(id=123, is_bot=False, first_name="Tester"),
        text=text,
        caption=caption,
        photo=photo,
    )


async def dispatch(msg, db, settings=None, session=None):
    session = session or MemoryTelegram()
    bot = Bot("123456789:abcdefghijklmnopqrstuvwxyz123456789", session=session)
    dp = Dispatcher()
    dp.include_router(create_router())
    await dp.feed_update(
        bot,
        Update(
            update_id=42, **({"callback_query": msg} if isinstance(msg, CallbackQuery) else {"message": msg})
        ),
        db=db,
        settings=settings or Settings(bot.token, "postgresql://db/cats"),
    )
    await bot.session.close()
    return session.requests


def fake_db():
    db = AsyncMock()
    db.profile.return_value = {
        "nickname": "Тестовый кот",
        "spins": 3,
        "score": 1,
        "streak": 1,
        "owned": 1,
        "total": 10,
    }
    db.dialog.return_value = None
    db.admin_active.return_value = False
    return db


def callback(data, username=None):
    return CallbackQuery(
        id="test-query",
        chat_instance="test-chat",
        data=data,
        from_user=User(id=123, is_bot=False, first_name="Tester", username=username),
        message=Message(
            message_id=5,
            date=datetime.now(UTC),
            chat=Chat(id=123, type="private"),
            from_user=User(id=999, is_bot=True, first_name="Bot"),
            text="Меню",
        ),
    )


async def test_cat_sends_existing_file_id_without_downloading():
    db = fake_db()
    db.roll.return_value = {
        "telegram_file_id": "existing_telegram_file_id",
        "cat_id": "c1",
        "description": "Кот",
        "rarity": "COM",
        "points": 1,
        "spins_after": 2,
    }
    requests = await dispatch(message(text="/cat"), db)
    photo = next(r for r in requests if isinstance(r, SendPhoto))
    assert photo.photo == "existing_telegram_file_id"
    db.roll.assert_awaited_once_with(42, 123, 3)


async def test_empty_catalog_does_not_try_to_send_photo():
    db = fake_db()
    db.roll.return_value = {"error": "empty"}
    requests = await dispatch(message(text="/cat"), db)
    assert all(isinstance(r, SendMessage) for r in requests)


async def test_non_admin_cannot_add_card():
    db = fake_db()
    await dispatch(message(text="/addcat c1 | COM | Кот"), db)
    db.add_cat.assert_not_awaited()


async def test_admin_photo_caption_registers_card():
    db = fake_db()
    db.admin_active.return_value = True
    settings = Settings("token", "postgresql://db/cats", admin_ids=frozenset({123}))
    await dispatch(
        message(
            caption="/addcat c1 | RARE | Кот",
            photo=[
                {
                    "file_id": "telegram_photo_id",
                    "file_unique_id": "unique",
                    "width": 100,
                    "height": 100,
                }
            ],
        ),
        db,
        settings,
    )
    db.add_cat.assert_awaited_once_with("c1", "Кот", "RARE", "telegram_photo_id", "unique")


async def test_original_inline_menu_and_balance():
    requests = await dispatch(message(text="/start"), fake_db())
    sent = next(r for r in requests if isinstance(r, SendMessage))
    assert "Главное меню" in sent.text and "Тестовый кот" in sent.text
    buttons = [b for row in sent.reply_markup.inline_keyboard for b in row]
    assert {"spin", "rewards", "change_nick", "leaderboard"} <= {b.callback_data for b in buttons}
    assert not any(b.callback_data.startswith("admin_") for b in buttons)
    assert not any(
        "winter" in (b.callback_data or "") or "super_pick" in (b.callback_data or "") for b in buttons
    )


async def test_rewards_navigation_edits_existing_menu():
    db = fake_db()
    requests = await dispatch(callback("rewards"), db)
    edited = next(r for r in requests if isinstance(r, EditMessageText))
    buttons = [b.callback_data for row in edited.reply_markup.inline_keyboard for b in row]
    assert buttons == ["reward_daily", "promo_enter", "back_main"]


async def test_manual_nickname_survives_separate_dispatcher_instance():
    db = fake_db()
    await dispatch(callback("nick_manual"), db)
    db.set_dialog.assert_awaited_once_with(123, "nick")
    db.dialog.return_value = "nick"
    await dispatch(message(text="Новый кот"), db)
    db.set_nickname.assert_awaited_once_with(123, "Новый кот", 3)


async def test_username_uses_player_id_instead_of_bot_message_author():
    db = fake_db()
    await dispatch(callback("nick_use_username", "cat_owner"), db)
    db.set_nickname.assert_awaited_once_with(123, "@cat_owner", 3)


async def test_ordinary_user_cannot_enter_admin_mode():
    db = fake_db()
    await dispatch(message(text="/admin"), db)
    db.enter_admin.assert_not_awaited()


async def test_allowlisted_admin_must_enable_mode_before_changes():
    db = fake_db()
    settings = Settings("token", "postgresql://db/cats", admin_ids=frozenset({123}))
    requests = await dispatch(message(text="/addcat c1 | COM | Кот"), db, settings)
    assert any("Сначала включи" in (getattr(r, "text", "") or "") for r in requests)
    db.add_cat.assert_not_awaited()


async def test_admin_entry_and_exit():
    db = fake_db()
    settings = Settings("token", "postgresql://db/cats", admin_ids=frozenset({123}))
    await dispatch(message(text="/admin"), db, settings)
    db.enter_admin.assert_awaited_once_with(123)
    await dispatch(message(text="/adminoff"), db, settings)
    db.exit_admin.assert_awaited_once_with(123)


async def test_forged_admin_callback_does_not_read_stats():
    db = fake_db()
    await dispatch(callback("admin_stats"), db)
    db.stats.assert_not_awaited()
    db.admin_active.assert_not_awaited()


async def test_forged_admin_dialog_rechecks_allowlist():
    db = fake_db()
    db.dialog.return_value = "admin_hidecat"
    await dispatch(message(text="c1"), db)
    db.set_cat_active.assert_not_awaited()


async def test_promo_creation_requires_active_admin():
    db = fake_db()
    settings = Settings("token", "postgresql://db/cats", admin_ids=frozenset({123}))
    await dispatch(message(text="/promocreate cats | 5 | Привет"), db, settings)
    db.create_promo.assert_not_awaited()
    db.admin_active.return_value = True
    await dispatch(message(text="/promocreate cats | 5 | Привет"), db, settings)
    db.create_promo.assert_awaited_once_with("CATS", 5, "Привет", 123)


async def test_promo_input_uses_persistent_dialog():
    db = fake_db()
    await dispatch(callback("promo_enter"), db)
    db.set_dialog.assert_awaited_once_with(123, "promo")
    db.dialog.return_value = "promo"
    db.redeem_promo.return_value = {"added": 5, "balance": 8, "description": "Привет"}
    await dispatch(message(text="cats"), db)
    db.redeem_promo.assert_awaited_once_with(123, "CATS", 3, 999)


async def test_daily_callback_uses_streak_rewards():
    db = fake_db()
    db.daily_reward.return_value = {"added": 1, "balance": 4, "streak": 1, "claimed": True}
    await dispatch(callback("reward_daily"), db)
    assert db.daily_reward.await_args.args[-1] == "streak"
    db.daily.assert_not_awaited()


async def test_subscription_requires_membership():
    db = fake_db()
    settings = Settings("token", "postgresql://db/cats", bonus_channel="@cats_band")
    await dispatch(callback("subscription_check"), db, settings, MemoryTelegram(member_status="left"))
    db.claim_subscription.assert_not_awaited()


async def test_subscription_requires_bot_administrator():
    db = fake_db()
    settings = Settings("token", "postgresql://db/cats", bonus_channel="@cats_band")
    await dispatch(callback("subscription_check"), db, settings, MemoryTelegram(bot_status="member"))
    db.claim_subscription.assert_not_awaited()


async def test_subscribed_user_receives_reward_for_configured_channel():
    db = fake_db()
    settings = Settings("token", "postgresql://db/cats", bonus_channel="@cats_band")
    db.claim_subscription.return_value = {"added": 3, "balance": 6}
    await dispatch(callback("subscription_check"), db, settings)
    db.claim_subscription.assert_awaited_once_with(123, "@cats_band", 3, 3, 999)
