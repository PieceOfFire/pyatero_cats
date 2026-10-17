import os
import re
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    bot_token: str = field(repr=False)
    database_url: str = field(repr=False)
    mode: str = "polling"
    webhook_base_url: str = ""
    webhook_secret: str = field(default="", repr=False)
    port: int = 10000
    admin_ids: frozenset[int] = frozenset()
    media_chat_id: int | None = None
    title: str = "Пятеро котят"
    band_url: str = ""
    timezone: str = "Asia/Novosibirsk"
    initial_spins: int = 3
    daily_spins: int = 3
    max_spins: int = 999
    max_concurrent_updates: int = 8
    db_pool_max: int = 5
    bonus_channel: str = ""
    bonus_channel_url: str = ""
    subscription_spins: int = 3
    daily_reward_mode: str = "streak"

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.admin_ids

    @property
    def subscription_url(self) -> str:
        if self.bonus_channel_url:
            return self.bonus_channel_url
        return f"https://t.me/{self.bonus_channel[1:]}" if self.bonus_channel.startswith("@") else ""

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        token = os.getenv("BOT_TOKEN", "").strip()
        database = os.getenv("DATABASE_URL", "").strip()
        if not token or not database:
            raise ValueError("Заполните BOT_TOKEN и DATABASE_URL в .env или переменных окружения")
        if not database.startswith(("postgres://", "postgresql://")):
            raise ValueError("DATABASE_URL должен быть строкой подключения PostgreSQL")
        result = cls(
            bot_token=token,
            database_url=database,
            mode=os.getenv("MODE", "polling"),
            webhook_base_url=(os.getenv("WEBHOOK_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL", "")).rstrip(
                "/"
            ),
            webhook_secret=os.getenv("WEBHOOK_SECRET", ""),
            port=int(os.getenv("PORT", "10000")),
            admin_ids=parse_admin_ids(),
            media_chat_id=int(os.environ["MEDIA_CHAT_ID"]) if os.getenv("MEDIA_CHAT_ID") else None,
            title=os.getenv("BOT_TITLE", "Пятеро котят"),
            band_url=os.getenv("BAND_URL", ""),
            timezone=os.getenv("TIMEZONE", "Asia/Novosibirsk"),
            initial_spins=int(os.getenv("INITIAL_SPINS", "3")),
            daily_spins=int(os.getenv("DAILY_SPINS", "3")),
            max_spins=int(os.getenv("MAX_SPINS", "999")),
            max_concurrent_updates=int(os.getenv("MAX_CONCURRENT_UPDATES", "8")),
            db_pool_max=int(os.getenv("DB_POOL_MAX", "5")),
            bonus_channel=os.getenv("BONUS_CHANNEL", "").strip(),
            bonus_channel_url=os.getenv("BONUS_CHANNEL_URL", "").strip(),
            subscription_spins=int(os.getenv("SUBSCRIPTION_SPINS", "3")),
            daily_reward_mode=os.getenv("DAILY_REWARD_MODE", "streak").strip().lower(),
        )
        result.validate()
        return result

    def validate(self) -> None:
        ZoneInfo(self.timezone)
        if self.mode not in {"polling", "webhook"}:
            raise ValueError("MODE: polling или webhook")
        if not 1 <= self.port <= 65535:
            raise ValueError("Некорректный PORT")
        if not 1 <= self.max_concurrent_updates <= 40 or not 1 <= self.db_pool_max <= 20:
            raise ValueError("Некорректный лимит параллельности или пула")
        if not 0 <= self.initial_spins <= self.max_spins or not 1 <= self.daily_spins <= self.max_spins:
            raise ValueError("Некорректное количество спинов")
        if self.band_url and not self.band_url.startswith("https://"):
            raise ValueError("BAND_URL должен начинаться с https://")
        if any(uid <= 0 for uid in self.admin_ids):
            raise ValueError("ID администратора должен быть положительным числом из /id")
        if not 1 <= self.subscription_spins <= self.max_spins:
            raise ValueError("Некорректный SUBSCRIPTION_SPINS")
        if self.daily_reward_mode not in {"streak", "fixed"}:
            raise ValueError("DAILY_REWARD_MODE: streak или fixed")
        if self.bonus_channel and not (
            re.fullmatch(r"@[A-Za-z0-9_]{5,32}", self.bonus_channel)
            or re.fullmatch(r"-100\d+", self.bonus_channel)
        ):
            raise ValueError("BONUS_CHANNEL: @username канала или числовой ID -100…")
        if self.bonus_channel_url and not self.bonus_channel_url.startswith("https://"):
            raise ValueError("BONUS_CHANNEL_URL должен начинаться с https://")
        if self.mode == "webhook":
            if not self.webhook_base_url.startswith("https://"):
                raise ValueError("Для webhook нужен WEBHOOK_BASE_URL или RENDER_EXTERNAL_URL с https://")
            if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", self.webhook_secret):
                raise ValueError("WEBHOOK_SECRET: 32–256 символов A-Z, a-z, 0-9, _ или -")


def parse_admin_ids() -> frozenset[int]:
    # ADMIN_ID is the primary secret. Keep older configuration names compatible.
    values = ",".join(os.getenv(key, "") for key in ("ADMIN_ID", "ADMIN_TELEGRAM_ID", "ADMIN_IDS"))
    try:
        result = frozenset(int(value.strip()) for value in values.split(",") if value.strip())
    except ValueError:
        raise ValueError("ADMIN_ID/ADMIN_IDS: вставьте числовой Telegram ID из /id, не @username") from None
    if any(value <= 0 for value in result):
        raise ValueError("ADMIN_ID/ADMIN_IDS должны быть положительными")
    return result
