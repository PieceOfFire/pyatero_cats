"""Offline administration: python -m catbot.tools --help."""

import argparse
import asyncio
import contextlib
import csv
import io
import os
import re
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import BufferedInputFile
from dotenv import load_dotenv

from catbot.config import Settings
from catbot.db import Database
from catbot.domain import POINTS, validate_cat
from catbot.mail_storage import MAX_PHOTO_BYTES, MailStorage


async def read_csv(path: Path):
    text = await asyncio.to_thread(path.read_text, encoding="utf-8-sig")
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    return [
        {str(k).strip().lower(): (v or "").strip() for k, v in row.items() if k is not None}
        for row in csv.DictReader(io.StringIO(text), dialect=dialect)
    ]


async def import_catalog(path: Path, db: Database, bot: Bot, settings: Settings):
    rows = await read_csv(path)
    csv_parent = (await asyncio.to_thread(path.resolve)).parent
    if not rows:
        raise ValueError("CSV пуст")
    # Validate the whole manifest before uploading any files.
    seen = set()
    for row in rows:
        row["id"], row["description"], row["rarity"] = validate_cat(
            row.get("id", ""), row.get("description") or row.get("desc", ""), row.get("rarity", "")
        )
        if row["id"] in seen:
            raise ValueError(f"Повтор ID {row['id']}")
        seen.add(row["id"])
        sources = [bool(row.get(k)) for k in ("telegram_file_id", "local_path", "mail_path")]
        if sum(sources) != 1:
            raise ValueError(f"#{row['id']}: укажите ровно один telegram_file_id, local_path или mail_path")
        if not row.get("telegram_file_id") and settings.media_chat_id is None:
            raise ValueError("Для загрузки фото нужен MEDIA_CHAT_ID")
    mail = None
    try:
        if any(row.get("mail_path") for row in rows):
            mail = await MailStorage(
                os.getenv("MAIL_USER", ""), os.getenv("MAIL_APP_PASSWORD", "")
            ).__aenter__()
        for row in rows:
            file_id, unique_id = row.get("telegram_file_id"), None
            source = row.get("mail_path") or row.get("local_path")
            if not file_id:
                if row.get("mail_path"):
                    data = await mail.download_photo(row["mail_path"])
                else:
                    local = Path(row["local_path"])
                    local = local if local.is_absolute() else csv_parent / local
                    if (await asyncio.to_thread(local.stat)).st_size > MAX_PHOTO_BYTES:
                        raise ValueError(f"#{row['id']}: фото больше 10 МБ")
                    data = await asyncio.to_thread(local.read_bytes)
                for attempt in range(4):
                    try:
                        sent = await bot.send_photo(
                            settings.media_chat_id,
                            BufferedInputFile(data, filename=f"{row['id']}.jpg"),
                            caption=f"#{row['id']} · {row['rarity']}\n{row['description']}",
                        )
                        break
                    except TelegramRetryAfter as error:
                        if attempt == 3:
                            raise
                        await asyncio.sleep(error.retry_after + 1)
                file_id = sent.photo[-1].file_id
                unique_id = sent.photo[-1].file_unique_id
                await asyncio.sleep(1.1)
            await db.add_cat(row["id"], row["description"], row["rarity"], file_id, unique_id, source)
            print(f"Сохранено: #{row['id']}")
    finally:
        if mail:
            await mail.__aexit__(None, None, None)


def parse_legacy_users(rows, known_cats: dict, max_spins: int):
    result, seen = [], set()
    for row in rows:
        user_id = int(row.get("user_id") or row.get("telegram_id") or 0)
        if user_id <= 0 or user_id in seen:
            raise ValueError("USER_ID должен быть положительным и не повторяться")
        seen.add(user_id)
        owned = set(filter(None, re.split(r"[|,;\s]+", row.get("cats_id", ""))))
        missing = owned - known_cats.keys()
        if missing:
            raise ValueError(f"USER_ID {user_id}: сначала импортируйте котят {', '.join(sorted(missing))}")
        spins = int(row.get("spins") or 0)
        score = int(row["sum"]) if row.get("sum") else sum(POINTS[known_cats[k]] for k in owned)
        if not 0 <= spins <= max_spins or score < 0:
            raise ValueError(f"USER_ID {user_id}: недопустимые SPINS/SUM")
        last_daily = date.fromisoformat(row["last_daily"]) if row.get("last_daily") else None
        streak = int(row.get("streak") or 0)
        if streak < 0:
            raise ValueError(f"USER_ID {user_id}: некорректный STREAK")
        result.append((user_id, row.get("nick", "")[:100], spins, score, last_daily, sorted(owned), streak))
    return result


async def import_users(path: Path, db: Database, settings: Settings):
    rows = await read_csv(path)
    async with db.pool.acquire() as conn, conn.transaction():
        cats = await conn.fetch("SELECT id,rarity FROM cats")
        parsed = parse_legacy_users(rows, {c["id"]: c["rarity"] for c in cats}, settings.max_spins)
        count = 0
        for user_id, nick, spins, score, last_daily, owned, streak in parsed:
            inserted = await conn.fetchval(
                """
                INSERT INTO users(telegram_id,nickname,spins,score,last_daily,streak)
                VALUES ($1,$2,$3,$4,$5,$6)
                ON CONFLICT DO NOTHING RETURNING telegram_id
            """,
                user_id,
                nick,
                spins,
                score,
                last_daily,
                streak,
            )
            if inserted is not None:
                await conn.executemany(
                    "INSERT INTO user_cats(user_id,cat_id) VALUES ($1,$2)",
                    [(user_id, cat_id) for cat_id in owned],
                )
                count += 1
    print(f"Добавлено пользователей: {count}. Уже существующие пользователи пропущены.")


def pg_environment(url: str):
    # Credentials live in the child environment, not the process command line.
    from urllib.parse import parse_qs

    parsed = urlsplit(url)
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname or not parsed.path.strip("/"):
        raise ValueError("Некорректный URL PostgreSQL")
    env = dict(os.environ)
    env.update(
        {
            "PGHOST": parsed.hostname,
            "PGPORT": str(parsed.port or 5432),
            "PGDATABASE": unquote(parsed.path.lstrip("/")),
            "PGUSER": unquote(parsed.username or ""),
            "PGPASSWORD": unquote(parsed.password or ""),
            "PGCONNECT_TIMEOUT": "20",
            "PGSSLMODE": parse_qs(parsed.query).get("sslmode", ["require"])[0],
        }
    )
    return env


async def backup(output: Path, upload_mail: bool):
    url = os.getenv("PG_DUMP_DATABASE_URL") or os.getenv("DATABASE_URL", "")
    env = pg_environment(url)
    await asyncio.to_thread(output.mkdir, parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    target = output / f"cats-{stamp}.dump"
    process = await asyncio.create_subprocess_exec(
        os.getenv("PG_DUMP_PATH", "pg_dump"),
        "--format=custom",
        "--no-owner",
        "--no-acl",
        "--file",
        str(target),
        env=env,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=600)
    except BaseException:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        raise
    if process.returncode:
        # Don't print raw pg_dump errors: they may contain connection details.
        raise RuntimeError(
            f"pg_dump завершился с кодом {process.returncode}; проверьте подключение и версию клиента"
        )
    print(f"Резервная копия: {target}")
    if upload_mail:
        folder = os.getenv("MAIL_BACKUP_DIR", "/pyatero-cats/backups").rstrip("/")
        async with MailStorage(os.getenv("MAIL_USER", ""), os.getenv("MAIL_APP_PASSWORD", "")) as mail:
            await mail.mkdirs(folder)
            await mail.upload_file(target, f"{folder}/{target.name}")
        print("Копия загружена в Облако Mail")


async def run(args):
    load_dotenv()
    if args.command == "backup":
        await backup(Path(args.output), args.mail)
        return
    settings = Settings.from_env()
    db = await Database.connect(settings.database_url, settings.db_pool_max)
    bot = Bot(settings.bot_token)
    try:
        await db.migrate()
        await db.bind_bot((await bot.get_me()).id)
        if args.command == "init-db":
            print("Схема готова, база привязана к боту")
        elif args.command == "import-cats":
            await import_catalog(Path(args.csv), db, bot, settings)
        elif args.command == "import-users":
            await import_users(Path(args.csv), db, settings)
        elif args.command == "prune":
            print(await db.prune_receipts())
    finally:
        await bot.session.close()
        await db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    subs.add_parser("init-db", help="Создать схему и привязать базу к текущему боту")
    for name in ("import-cats", "import-users"):
        sub = subs.add_parser(name)
        sub.add_argument("csv")
    sub = subs.add_parser("backup", help="pg_dump; опционально загрузить результат в Mail")
    sub.add_argument("--output", default="backups")
    sub.add_argument("--mail", action="store_true")
    subs.add_parser("prune", help="Удалить webhook receipts старше 7 дней; выдачи сохраняются")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
