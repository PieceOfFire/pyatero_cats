import asyncio
import hashlib
from datetime import date
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import asyncpg

from catbot.domain import POINTS, choose_cat, daily_terms, validate_cat


def asyncpg_url(url: str) -> str:
    # Neon copies libpq's channel_binding into URLs. asyncpg does not accept it
    # and otherwise sends it to PostgreSQL as an unknown server setting.
    parsed = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parsed.query) if k != "channel_binding"]
    return urlunsplit(parsed._replace(query=urlencode(query)))


class Database:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    @classmethod
    async def connect(cls, url: str, max_size: int = 5) -> "Database":
        # Neon pooled URLs work without session state or prepared statement caching.
        pool = await asyncpg.create_pool(
            dsn=asyncpg_url(url),
            min_size=0,
            max_size=max_size,
            command_timeout=12,
            statement_cache_size=0,
            max_inactive_connection_lifetime=60,
            timeout=15,
        )
        return cls(pool)

    async def close(self):
        try:
            await asyncio.wait_for(self.pool.close(), timeout=10)
        except TimeoutError:
            self.pool.terminate()

    async def migrate(self):
        root = Path(__file__).parent
        files = [root / "schema.sql", root / "migrations" / "002_menus_admin.sql"]
        migrations = []
        for version, path in enumerate(files, 1):
            sql = await asyncio.to_thread(path.read_text, encoding="utf-8")
            migrations.append((version, sql, hashlib.sha256(sql.encode()).hexdigest()))
        async with self.pool.acquire() as conn, conn.transaction():
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
        await conn.execute(
            "INSERT INTO users(telegram_id,spins) VALUES ($1,$2) ON CONFLICT DO NOTHING",
            user_id,
            initial_spins,
        )
        return await conn.fetchrow("SELECT * FROM users WHERE telegram_id=$1 FOR UPDATE", user_id)

    async def profile(self, user_id: int, initial_spins: int):
        async with self.pool.acquire() as conn, conn.transaction():
            user = dict(await self._user(conn, user_id, initial_spins))
            user["owned"] = await conn.fetchval("SELECT count(*) FROM user_cats WHERE user_id=$1", user_id)
            user["total"] = await conn.fetchval("SELECT count(*) FROM cats WHERE active")
            return user

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

    async def set_nickname(self, user_id: int, nickname: str, initial_spins: int):
        async with self.pool.acquire() as conn, conn.transaction():
            await self._user(conn, user_id, initial_spins)
            await conn.execute("UPDATE users SET nickname=$2 WHERE telegram_id=$1", user_id, nickname)

    async def top(self):
        return await self.pool.fetch("""
            SELECT nickname,score FROM users WHERE score>0 ORDER BY score DESC,telegram_id LIMIT 10
        """)

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

    async def set_dialog(self, user_id: int, state: str):
        await self.pool.execute(
            """
            INSERT INTO dialog_states(user_id,state) VALUES ($1,$2)
            ON CONFLICT (user_id) DO UPDATE SET state=$2,expires_at=now()+interval '30 minutes'
        """,
            user_id,
            state,
        )

    async def dialog(self, user_id: int):
        return await self.pool.fetchval(
            "SELECT state FROM dialog_states WHERE user_id=$1 AND expires_at>now()", user_id
        )

    async def clear_dialog(self, user_id: int, state: str | None = None):
        await self.pool.execute(
            "DELETE FROM dialog_states WHERE user_id=$1 AND ($2::text IS NULL OR state=$2)", user_id, state
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

    async def set_cat_active(self, cat_id: str, active: bool):
        return await self.pool.fetchval("UPDATE cats SET active=$2 WHERE id=$1 RETURNING id", cat_id, active)

    async def stats(self):
        return await self.pool.fetchrow("""
            SELECT (SELECT count(*) FROM users) AS users,
                   (SELECT count(*) FROM cats WHERE active) AS cats,
                   (SELECT count(*) FROM rolls) AS rolls,
                   (SELECT count(*) FROM promo_redemptions) AS redemptions
        """)
