import asyncio
import hashlib
from contextlib import asynccontextmanager
from datetime import date
from functools import wraps
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import asyncpg

from catbot.domain import POINTS, choose_cat, daily_terms, validate_cat
from catbot.runtime import TTLCache, current_update_id


def invalidates_profile(user_position=0):
    """Display caches never participate in balance checks or reward decisions."""

    def decorate(function):
        @wraps(function)
        async def wrapped(self, *args, **kwargs):
            user_id = (
                None
                if user_position is None
                else (kwargs["user_id"] if "user_id" in kwargs else args[user_position])
            )

            def invalidate():
                self._profile_generation += 1
                if user_id is None:
                    self._profiles.clear()
                else:
                    self._profiles.discard(user_id)

            invalidate()
            try:
                return await function(self, *args, **kwargs)
            finally:
                invalidate()

        return wrapped

    return decorate


def asyncpg_url(url: str) -> str:
    # Neon copies libpq's channel_binding into URLs. asyncpg does not accept it
    # and otherwise sends it to PostgreSQL as an unknown server setting.
    parsed = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parsed.query) if k != "channel_binding"]
    return urlunsplit(parsed._replace(query=urlencode(query)))


def direct_database_url(url: str) -> str:
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if host.endswith(".neon.tech") and "-pooler." in host:
        parsed = parsed._replace(netloc=parsed.netloc.replace(host, host.replace("-pooler.", ".", 1)))
    return asyncpg_url(urlunsplit(parsed))


class Database:
    def __init__(self, pool: asyncpg.Pool, migration_url: str | None = None):
        self.pool = pool
        self._migration_url = migration_url
        self._profiles = TTLCache(ttl=15)
        self._profile_generation = 0

    @classmethod
    async def connect(cls, url: str, max_size: int = 5) -> "Database":
        # Neon PgBouncer supports protocol-level prepared statements. Caching avoids
        # repeated parse/describe network round trips for every query.
        pool = await asyncpg.create_pool(
            dsn=asyncpg_url(url),
            min_size=0,
            max_size=max_size,
            command_timeout=12,
            statement_cache_size=100,
            max_inactive_connection_lifetime=60,
            timeout=15,
        )
        return cls(pool, direct_database_url(url))

    async def close(self):
        try:
            await asyncio.wait_for(self.pool.close(), timeout=10)
        except TimeoutError:
            self.pool.terminate()

    @asynccontextmanager
    async def _migration_connection(self):
        if self._migration_url:
            conn = await asyncpg.connect(self._migration_url, timeout=20, command_timeout=60)
            try:
                yield conn
            finally:
                await conn.close()
        else:
            async with self.pool.acquire() as conn:
                yield conn

    async def migrate(self):
        root = Path(__file__).parent
        files = [
            root / "schema.sql",
            root / "migrations" / "002_menus_admin.sql",
            root / "migrations" / "003_dialog_order.sql",
            root / "migrations" / "004_telegram_identity.sql",
        ]
        migrations = []
        for version, path in enumerate(files, 1):
            sql = await asyncio.to_thread(path.read_text, encoding="utf-8")
            migrations.append((version, sql, hashlib.sha256(sql.encode()).hexdigest()))
        async with self._migration_connection() as conn, conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(827361001)")
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS schema_versions (
                    version integer PRIMARY KEY, checksum text NOT NULL,
                    applied_at timestamptz NOT NULL DEFAULT now()
                )
            """)
            for version, sql, checksum in migrations:
                previous = await conn.fetchval(
                    "SELECT checksum FROM schema_versions WHERE version=$1", version
                )
                if previous is None:
                    await conn.execute(sql)
                    await conn.execute(
                        "INSERT INTO schema_versions(version, checksum) VALUES ($1,$2)", version, checksum
                    )
                elif previous != checksum:
                    raise RuntimeError(f"Миграция {version} изменена после применения: нужна новая миграция")

    async def bind_bot(self, bot_id: int):
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "INSERT INTO bot_meta(key,value) VALUES ('bot_id',$1) ON CONFLICT DO NOTHING", str(bot_id)
            )
            bound = await conn.fetchval("SELECT value FROM bot_meta WHERE key='bot_id'")
            if bound != str(bot_id):
                raise RuntimeError(
                    "Эта база привязана к другому боту. Используйте отдельную базу и новые file_id"
                )

    @staticmethod
    async def _user(conn, user_id: int, initial_spins: int):
        user = await conn.fetchrow("SELECT * FROM users WHERE telegram_id=$1 FOR UPDATE", user_id)
        if user is not None:
            return user
        await conn.execute(
            "INSERT INTO users(telegram_id,spins) VALUES ($1,$2) ON CONFLICT DO NOTHING",
            user_id,
            initial_spins,
        )
        return await conn.fetchrow("SELECT * FROM users WHERE telegram_id=$1 FOR UPDATE", user_id)

    @invalidates_profile()
    async def sync_telegram_user(
        self, user_id: int, username: str | None, initial_spins: int, update_id: int
    ):
        """Register the real actor; retain manual names, balances and stable Telegram ID."""
        async with self.pool.acquire() as conn:
            applied = await conn.fetchval(
                """
                INSERT INTO users(
                    telegram_id,spins,nickname,telegram_username,last_known_telegram_username,
                    telegram_identity_update_id,telegram_identity_seen_at
                ) VALUES ($1,$2,CASE WHEN $3::text IS NULL THEN '' ELSE '@'||$3 END,$3,$3,$4,now())
                ON CONFLICT (telegram_id) DO UPDATE SET
                    telegram_username=EXCLUDED.telegram_username,
                    last_known_telegram_username=COALESCE(
                        EXCLUDED.telegram_username,users.last_known_telegram_username
                    ),
                    nickname=CASE WHEN users.nickname_custom THEN users.nickname
                        ELSE EXCLUDED.nickname END,
                    telegram_identity_update_id=EXCLUDED.telegram_identity_update_id,
                    telegram_identity_seen_at=now()
                WHERE users.telegram_identity_update_id<=EXCLUDED.telegram_identity_update_id
                    OR users.telegram_identity_seen_at IS NULL
                    OR users.telegram_identity_seen_at<now()-interval '1 day'
                RETURNING telegram_id
                """,
                user_id,
                initial_spins,
                username or None,
                update_id,
            )
        return applied is not None

    async def admin_users(self, offset=0):
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT telegram_id,nickname,telegram_username,last_known_telegram_username
                   FROM users ORDER BY created_at,telegram_id LIMIT 10 OFFSET $1""",
                offset,
            )
            count = await conn.fetchval("SELECT count(*) FROM users")
        return rows, count

    async def profile(self, user_id: int, initial_spins: int):
        cached = self._profiles.get(user_id)
        if cached is not None:
            return dict(cached)
        generation = self._profile_generation
        sql = """
            SELECT u.*,(SELECT count(*) FROM user_cats WHERE user_id=u.telegram_id) AS owned,
                       (SELECT count(*) FROM cats WHERE active) AS total
            FROM users u WHERE u.telegram_id=$1
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(sql, user_id)
            if row is None:
                await conn.execute(
                    "INSERT INTO users(telegram_id,spins) VALUES ($1,$2) ON CONFLICT DO NOTHING",
                    user_id,
                    initial_spins,
                )
                row = await conn.fetchrow(sql, user_id)
        user = dict(row)
        if generation == self._profile_generation:
            self._profiles.put(user_id, user)
        return dict(user)

    @invalidates_profile(user_position=1)
    async def roll(self, update_id: int, user_id: int, initial_spins: int):
        async with self.pool.acquire() as conn, conn.transaction():
            user = await self._user(conn, user_id, initial_spins)
            saved = await conn.fetchrow(
                """
                SELECT r.*, c.description, c.rarity, c.telegram_file_id
                FROM rolls r JOIN cats c ON c.id=r.cat_id WHERE r.update_id=$1
            """,
                update_id,
            )
            if saved:
                if saved["user_id"] != user_id:
                    raise RuntimeError("Конфликт update_id")
                return dict(saved)
            if user["spins"] <= 0:
                return {"error": "no_spins"}
            # Catalog is small. One query, no Google or image downloads on the request path.
            candidates = await conn.fetch(
                """
                SELECT c.* FROM cats c WHERE c.active AND NOT EXISTS (
                    SELECT 1 FROM user_cats uc WHERE uc.user_id=$1 AND uc.cat_id=c.id
                )
            """,
                user_id,
            )
            if not candidates:
                total = await conn.fetchval("SELECT count(*) FROM cats WHERE active")
                return {"error": "complete" if total else "empty"}
            cat = choose_cat(candidates)
            points, spins = POINTS[cat["rarity"]], user["spins"] - 1
            await conn.execute("INSERT INTO user_cats(user_id,cat_id) VALUES ($1,$2)", user_id, cat["id"])
            await conn.execute(
                "UPDATE users SET spins=$2, score=score+$3 WHERE telegram_id=$1", user_id, spins, points
            )
            await conn.execute(
                """
                INSERT INTO rolls(update_id,user_id,cat_id,points,spins_after) VALUES ($1,$2,$3,$4,$5)
            """,
                update_id,
                user_id,
                cat["id"],
                points,
                spins,
            )
            return {
                **dict(cat),
                "cat_id": cat["id"],
                "points": points,
                "spins_after": spins,
                "update_id": update_id,
            }

    async def daily(self, user_id: int, today: date, initial_spins: int, bonus: int, max_spins: int):
        result = await self.daily_reward(user_id, today, initial_spins, bonus, max_spins, "fixed")
        return result["added"], result["balance"]

    @invalidates_profile()
    async def daily_reward(self, user_id, today, initial_spins, bonus, max_spins, mode="streak"):
        async with self.pool.acquire() as conn, conn.transaction():
            user = await self._user(conn, user_id, initial_spins)
            reward, streak = daily_terms(user["last_daily"], user["streak"], today, bonus, mode)
            if not reward:
                return {"added": 0, "balance": user["spins"], "streak": streak, "claimed": False}
            spins = max(user["spins"], min(max_spins, user["spins"] + reward))
            await conn.execute(
                "UPDATE users SET spins=$2,last_daily=$3,streak=$4 WHERE telegram_id=$1",
                user_id,
                spins,
                today,
                streak,
            )
            return {"added": spins - user["spins"], "balance": spins, "streak": streak, "claimed": True}

    async def collection(self, user_id: int, offset: int = 0):
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT c.id,c.description,c.rarity FROM user_cats uc JOIN cats c ON c.id=uc.cat_id
                WHERE uc.user_id=$1 ORDER BY uc.acquired_at,c.id LIMIT 10 OFFSET $2
            """,
                user_id,
                offset,
            )
            count = await conn.fetchval("SELECT count(*) FROM user_cats WHERE user_id=$1", user_id)
            return rows, count

    async def last_roll(self, user_id: int):
        return await self.pool.fetchrow(
            """
            SELECT r.*,c.description,c.rarity,c.telegram_file_id FROM rolls r JOIN cats c ON c.id=r.cat_id
            WHERE r.user_id=$1 ORDER BY r.created_at DESC,r.update_id DESC LIMIT 1
        """,
            user_id,
        )

    @invalidates_profile()
    async def set_nickname(self, user_id: int, nickname: str, initial_spins: int, *, custom=True):
        async with self.pool.acquire() as conn, conn.transaction():
            await self._user(conn, user_id, initial_spins)
            await conn.execute(
                "UPDATE users SET nickname=$2,nickname_custom=$3 WHERE telegram_id=$1",
                user_id,
                nickname,
                custom,
            )

    async def top(self):
        return await self.pool.fetch("""
            SELECT nickname,score FROM users WHERE score>0 ORDER BY score DESC,telegram_id LIMIT 10
        """)

    @invalidates_profile(user_position=None)
    async def add_cat(self, cat_id, description, rarity, file_id, unique_id=None, source_path=None):
        cat_id, description, rarity = validate_cat(cat_id, description, rarity)
        if not file_id or file_id.startswith(("http:", "https:")):
            raise ValueError("Нужен Telegram file_id, а не URL")
        await self.pool.execute(
            """
            INSERT INTO cats(id,description,rarity,telegram_file_id,telegram_file_unique_id,source_path)
            VALUES ($1,$2,$3,$4,$5,$6)
            ON CONFLICT (id) DO UPDATE SET description=excluded.description,rarity=excluded.rarity,
                telegram_file_id=excluded.telegram_file_id,
                telegram_file_unique_id=excluded.telegram_file_unique_id,
                source_path=COALESCE(excluded.source_path,cats.source_path),active=true
        """,
            cat_id,
            description,
            rarity,
            file_id,
            unique_id,
            source_path,
        )

    async def claim_update(self, update_id: int) -> str:
        # A lease is longer than the HTTP processing deadline (45 s). Crashes leave no ACK,
        # Telegram retries, and a new process can reclaim the expired lease.
        row = await self.pool.fetchrow(
            """
            INSERT INTO update_receipts(update_id,status,lease_until)
            VALUES ($1,'processing',now()+interval '90 seconds')
            ON CONFLICT (update_id) DO UPDATE SET status='processing',
                lease_until=now()+interval '90 seconds',updated_at=now()
            WHERE update_receipts.status='processing' AND update_receipts.lease_until<now()
            RETURNING status
        """,
            update_id,
        )
        if row:
            return "claimed"
        state = await self.pool.fetchval("SELECT status FROM update_receipts WHERE update_id=$1", update_id)
        return "done" if state == "done" else "busy"

    async def finish_update(self, update_id: int):
        await self.pool.execute(
            "UPDATE update_receipts SET status='done',updated_at=now() WHERE update_id=$1", update_id
        )

    async def release_update(self, update_id: int):
        await self.pool.execute(
            """
            UPDATE update_receipts SET lease_until=now(),updated_at=now()
            WHERE update_id=$1 AND status='processing'
        """,
            update_id,
        )

    async def prune_receipts(self):
        # Run explicitly from maintenance CLI, never a periodic DB ping on Render.
        return await self.pool.execute(
            "DELETE FROM update_receipts WHERE updated_at < now()-interval '7 days'"
        )

    async def set_dialog(self, user_id: int, state: str, *, update_id: int | None = None):
        await self.pool.execute(
            """
            INSERT INTO dialog_states(user_id,state,update_id) VALUES ($1,$2,$3)
            ON CONFLICT (user_id) DO UPDATE SET state=$2,update_id=$3,expires_at=now()+interval '30 minutes'
            WHERE dialog_states.update_id<=$3 OR dialog_states.expires_at<=now()
        """,
            user_id,
            state,
            current_update_id.get() if update_id is None else update_id,
        )

    async def dialog(self, user_id: int):
        return await self.pool.fetchval(
            "SELECT NULLIF(state,'') FROM dialog_states WHERE user_id=$1 AND expires_at>now()", user_id
        )

    async def clear_dialog(self, user_id: int, state: str | None = None, *, update_id: int | None = None):
        version = current_update_id.get() if update_id is None else update_id
        if state is None:
            await self.set_dialog(user_id, "", update_id=version)
        else:
            await self.pool.execute(
                """
                UPDATE dialog_states SET state='',update_id=$3,expires_at=now()+interval '30 minutes'
                WHERE user_id=$1 AND state=$2 AND (update_id<=$3 OR expires_at<=now())
            """,
                user_id,
                state,
                version,
            )

    async def enter_admin(self, user_id: int):
        await self.pool.execute(
            """
            INSERT INTO admin_sessions(user_id) VALUES ($1)
            ON CONFLICT (user_id) DO UPDATE SET expires_at=now()+interval '12 hours'
        """,
            user_id,
        )

    async def admin_active(self, user_id: int) -> bool:
        return bool(
            await self.pool.fetchval(
                "SELECT EXISTS(SELECT 1 FROM admin_sessions WHERE user_id=$1 AND expires_at>now())", user_id
            )
        )

    async def exit_admin(self, user_id: int):
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("DELETE FROM admin_sessions WHERE user_id=$1", user_id)
            await conn.execute("DELETE FROM dialog_states WHERE user_id=$1", user_id)

    async def create_promo(self, code: str, bonus: int, description: str, admin_id: int):
        # An existing code cannot be silently changed/re-enabled by a repeated request.
        return await self.pool.fetchval(
            """
            INSERT INTO promo_codes(code,bonus,description,created_by) VALUES ($1,$2,$3,$4)
            ON CONFLICT DO NOTHING RETURNING code
        """,
            code,
            bonus,
            description,
            admin_id,
        )

    async def promos(self):
        return await self.pool.fetch("""
            SELECT p.code,p.bonus,p.active,count(r.user_id) AS used
            FROM promo_codes p LEFT JOIN promo_redemptions r ON r.code=p.code
            GROUP BY p.code ORDER BY p.created_at DESC LIMIT 20
        """)

    async def disable_promo(self, code: str):
        return await self.pool.fetchval(
            "UPDATE promo_codes SET active=false WHERE code=$1 RETURNING code", code
        )

    @invalidates_profile()
    async def redeem_promo(self, user_id: int, code: str, initial_spins: int, max_spins: int):
        async with self.pool.acquire() as conn, conn.transaction():
            user = await self._user(conn, user_id, initial_spins)
            promo = await conn.fetchrow("SELECT * FROM promo_codes WHERE code=$1 FOR SHARE", code)
            if not promo or not promo["active"]:
                return {"error": "invalid"}
            used = await conn.fetchval(
                "SELECT 1 FROM promo_redemptions WHERE user_id=$1 AND code=$2", user_id, code
            )
            if used:
                return {"error": "used"}
            added = max(0, min(promo["bonus"], max_spins - user["spins"]))
            if not added:
                return {"error": "full"}
            await conn.execute(
                "INSERT INTO promo_redemptions(user_id,code,granted) VALUES ($1,$2,$3)", user_id, code, added
            )
            await conn.execute("UPDATE users SET spins=spins+$2 WHERE telegram_id=$1", user_id, added)
            return {"added": added, "balance": user["spins"] + added, "description": promo["description"]}

    @invalidates_profile()
    async def claim_subscription(self, user_id: int, channel: str, initial: int, bonus: int, maximum: int):
        async with self.pool.acquire() as conn, conn.transaction():
            user = await self._user(conn, user_id, initial)
            if await conn.fetchval(
                "SELECT 1 FROM subscription_rewards WHERE user_id=$1 AND channel=$2", user_id, channel
            ):
                return {"error": "used"}
            added = max(0, min(bonus, maximum - user["spins"]))
            if not added:
                return {"error": "full"}
            await conn.execute(
                "INSERT INTO subscription_rewards(user_id,channel,granted) VALUES ($1,$2,$3)",
                user_id,
                channel,
                added,
            )
            await conn.execute("UPDATE users SET spins=spins+$2 WHERE telegram_id=$1", user_id, added)
            return {"added": added, "balance": user["spins"] + added}

    @invalidates_profile(user_position=None)
    async def set_cat_active(self, cat_id: str, active: bool):
        return await self.pool.fetchval("UPDATE cats SET active=$2 WHERE id=$1 RETURNING id", cat_id, active)

    async def stats(self):
        return await self.pool.fetchrow("""
            SELECT (SELECT count(*) FROM users) AS users,
                   (SELECT count(*) FROM cats WHERE active) AS cats,
                   (SELECT count(*) FROM rolls) AS rolls,
                   (SELECT count(*) FROM promo_redemptions) AS redemptions
        """)
