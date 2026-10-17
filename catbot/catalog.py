"""Batch import from Mail into unpublished cards, then review and publish a CSV."""

import asyncio
import csv
import hashlib
import io
import os
import tempfile
from pathlib import Path, PurePosixPath

from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import BufferedInputFile

from catbot.mail_storage import MAX_PHOTO_BYTES, mail_path

REVIEW_FIELDS = ["id", "description", "rarity", "mail_path"]
PHOTO_SUFFIXES = {".jpg", ".jpeg", ".png"}


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


def write_review_csv(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    # A failed export must not truncate previous captions. Replace only a complete file.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8-sig",
            newline="",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            writer = csv.DictWriter(handle, fieldnames=REVIEW_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


async def export_drafts(path, db):
    path = Path(path)
    existing = await read_csv(path) if await asyncio.to_thread(path.exists) else []
    saved = {}
    for row in existing:
        if not row.get("id") or row["id"] in saved:
            raise ValueError("В CSV черновиков должны быть уникальные непустые ID")
        saved[row["id"]] = row
    rows = []
    for card in await db.cat_drafts():
        previous = saved.get(card["id"], {})
        rows.append(
            {
                "id": card["id"],
                "description": previous.get("description") or card["description"],
                "rarity": previous.get("rarity", ""),
                "mail_path": card["source_path"] or "",
            }
        )
    await asyncio.to_thread(write_review_csv, path, rows)
    return len(rows)


async def publish_drafts(path, db):
    rows = await read_csv(Path(path))
    if not rows or any(not {"id", "description", "rarity"} <= row.keys() for row in rows):
        raise ValueError("Нужен CSV с колонками id,description,rarity")
    if len({row["id"] for row in rows}) != len(rows) or any(not row["id"] for row in rows):
        raise ValueError("Повторяющиеся или пустые ID в CSV")
    ready = [r for r in rows if r["description"] and r["rarity"]]
    count = await db.publish_cat_drafts(ready)
    return count, len(rows) - len(ready)


async def upload_draft(bot, chat_id, data, name, cat_id):
    for attempt in range(4):
        try:
            sent = await bot.send_photo(
                chat_id,
                BufferedInputFile(data, filename=name),
                caption=f"Черновик #{cat_id}\n{name}",
            )
            return sent.photo[-1]
        except TelegramRetryAfter as error:
            if attempt == 3:
                raise
            await asyncio.sleep(error.retry_after + 1)


async def import_mail(mail, db, bot, settings, inbox, archive, csv_path, limit=100):
    inbox, archive = mail_path(inbox), mail_path(archive)
    if inbox == "/" or archive == "/" or inbox == archive:
        raise ValueError("Укажи разные непустые папки входящих и архива внутри Облака Mail")
    if settings.media_chat_id is None:
        raise ValueError("Для пакетной загрузки фото заполни MEDIA_CHAT_ID")
    if not 1 <= limit <= 10000:
        raise ValueError("Лимит импорта: от 1 до 10000 файлов")
    # Fail before uploading if the existing review CSV is malformed or unwritable.
    await export_drafts(csv_path, db)
    await mail.mkdirs(inbox)
    await mail.mkdirs(archive)
    files = await mail.list_files(inbox)
    photos = [f for f in files if PurePosixPath(f["path"]).suffix.lower() in PHOTO_SUFFIXES]
    stats = {"imported": 0, "reused": 0, "moved": 0, "errors": 0, "ignored": len(files) - len(photos)}
    try:
        for entry in photos[:limit]:
            name = PurePosixPath(entry["path"]).name
            phase = "загрузка"
            try:
                if entry["size"] is not None and entry["size"] > MAX_PHOTO_BYTES:
                    raise ValueError("Фото превышает 10 МБ")
                data = await mail.download_photo(entry["path"])
                sha256 = (await asyncio.to_thread(hashlib.sha256, data)).hexdigest()
                filename = PurePosixPath(name)
                target = archive + "/" + sha256 + "_" + filename.stem[:80] + filename.suffix.lower()
                card = await db.imported_cat(sha256)
                if card is None:
                    photo = await upload_draft(bot, settings.media_chat_id, data, name, f"mail_{sha256}")
                    phase = "сохранение черновика"
                    card = await db.add_cat_draft(sha256, photo.file_id, photo.file_unique_id, target)
                    stats["imported"] += 1
                    await asyncio.sleep(1.1)
                else:
                    stats["reused"] += 1
                # The DB already holds the file_id. A failed MOVE is safe to retry:
                # the next scan finds the same hash and skips the Telegram upload.
                phase = "перенос в архив"
                await mail.move_file(entry["path"], target)
                stats["moved"] += 1
                print(f"Сохранено: #{card['id']} · {name}")
            except Exception as error:
                stats["errors"] += 1
                print(f"{name}: {phase} — {type(error).__name__}; проверь входящие и архив, затем повтори")
    finally:
        stats["drafts"] = await export_drafts(csv_path, db)
    return stats
