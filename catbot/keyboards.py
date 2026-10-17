from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from catbot.config import Settings


def button(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


def back(data="back_main", text="⬅️ Назад"):
    return InlineKeyboardMarkup(inline_keyboard=[[button(text, data)]])


def menu(settings: Settings, user_id: int | None = None, admin_mode: bool = False):
    rows = [
        [button("🎰 Спин", "spin")],
        [button("🎁 Награды", "rewards")],
        [button("✏️ Сменить ник", "change_nick")],
        [button("🏆 Лидерборд", "leaderboard")],
        [button("📚 Коллекция", "collection:0"), button("⭐ Профиль", "profile")],
    ]
    if settings.band_url:
        rows.append([InlineKeyboardButton(text="🎵 Пятеро котят", url=settings.band_url)])
    if user_id is not None and settings.is_admin(user_id):
        rows.append([button("⚙️ Админ-панель" if admin_mode else "🔐 Админский режим", "admin_open")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def rewards(settings: Settings):
    rows = [[button("🗓 Ежедневная", "reward_daily")]]
    if settings.bonus_channel:
        rows.append([button("📢 За подписку", "reward_sub")])
    rows.extend([[button("✏️ Ввести промокод", "promo_enter")], [button("⬅️ Назад", "back_main")]])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def nickname():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [button("✨ Использовать @username", "nick_use_username")],
            [button("✏️ Ввести вручную", "nick_manual")],
            [button("⬅️ Назад", "back_main")],
        ]
    )


def admin_panel():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [button("➕ Добавить / обновить котёнка", "admin_addcat")],
            [button("🙈 Скрыть карточку", "admin_hidecat"), button("👁 Вернуть карточку", "admin_showcat")],
            [button("🎟 Создать промокод", "admin_promo_create")],
            [button("📋 Промокоды", "admin_promos"), button("⛔ Отключить код", "admin_promo_disable")],
            [button("📊 Статистика", "admin_stats")],
            [button("🏠 Главное меню", "back_main")],
            [button("🔓 Выйти из админского режима", "admin_exit")],
        ]
    )


def subscription(settings: Settings):
    rows = []
    if settings.subscription_url:
        rows.append([InlineKeyboardButton(text="📢 Открыть канал", url=settings.subscription_url)])
    rows.extend([[button("✅ Проверить подписку", "subscription_check")], [button("⬅️ Назад", "rewards")]])
    return InlineKeyboardMarkup(inline_keyboard=rows)
