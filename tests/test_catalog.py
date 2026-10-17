import asyncio
import errno
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from catbot.catalog import (
    export_drafts,
    import_local,
    import_mail,
    publish_drafts,
    read_csv,
    write_review_csv,
)
from catbot.config import Settings
from catbot.local_storage import LocalStorage
from catbot.mail_storage import MAX_PHOTO_BYTES, MailStorage, parse_listing


def listing(href="/inbox/cat%20one.jpg"):
    return f"""<d:multistatus xmlns:d="DAV:">
      <d:response><d:href>/inbox/</d:href><d:propstat><d:prop>
        <d:resourcetype><d:collection/></d:resourcetype></d:prop>
        <d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>
      <d:response><d:href>{href}</d:href><d:propstat><d:prop>
        <d:resourcetype/><d:getcontentlength>123</d:getcontentlength></d:prop>
        <d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>
      <d:response><d:href>/elsewhere/other.jpg</d:href><d:propstat><d:prop/>
        <d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>
    </d:multistatus>""".encode()


def test_webdav_listing_handles_names_and_ignores_folder_and_unrelated_entries():
    assert parse_listing(listing(), "/inbox") == [{"path": "/inbox/cat one.jpg", "size": 123}]
    assert parse_listing(listing("/inbox/nested/cat.jpg"), "/inbox") == []


@pytest.mark.parametrize(
    "href",
    [
        "https://evil.test/inbox/a.jpg",
        "/inbox/%2e%2e/secret.jpg",
        "/inbox/%00.jpg",
    ],
)
def test_webdav_listing_rejects_unsafe_hrefs(href):
    with pytest.raises(ValueError):
        parse_listing(listing(href), "/inbox")


def test_webdav_listing_rejects_xml_entities():
    with pytest.raises(ValueError):
        parse_listing(b'<!DOCTYPE foo [<!ENTITY x "hello">]><foo/>', "/inbox")


async def test_mail_move_never_overwrites_existing_archive_file():
    mail = MailStorage("test@example.test", "dummy")
    mail.session = MagicMock()
    mail.session.request.return_value.__aenter__ = AsyncMock(return_value=SimpleNamespace(status=412))
    mail.session.request.return_value.__aexit__ = AsyncMock(return_value=False)
    with pytest.raises(FileExistsError):
        await mail.move_file("/inbox/cat.jpg", "/archive/cat.jpg")
    kwargs = mail.session.request.call_args.kwargs
    assert kwargs["headers"]["Overwrite"] == "F" and not kwargs["allow_redirects"]


class DraftDB:
    def __init__(self):
        self.cards = {}
        self.fail_write = False

    async def imported_cat(self, sha256):
        return self.cards.get(sha256)

    async def add_cat_draft(self, sha256, file_id, unique_id, source_path):
        if self.fail_write:
            raise RuntimeError("DB unavailable")
        card = {"id": f"mail_{sha256}", "description": "", "source_path": source_path}
        self.cards[sha256] = card
        return card

    async def cat_drafts(self):
        return list(self.cards.values())


class Folder:
    def __init__(self, files):
        self.files = dict(files)
        self.fail_move = False
        self.moves = []

    async def mkdirs(self, path):
        pass

    async def list_files(self, path):
        return [{"path": p, "size": len(data)} for p, data in self.files.items() if p.startswith(path + "/")]

    async def download_photo(self, path):
        return self.files[path]

    async def move_file(self, source, target):
        if self.fail_move:
            raise RuntimeError("MOVE unavailable")
        self.files[target] = self.files.pop(source)
        self.moves.append((source, target))


def setup_import(monkeypatch):
    monkeypatch.setattr("catbot.catalog.asyncio.sleep", AsyncMock())
    db, bot = DraftDB(), AsyncMock()
    bot.send_photo.return_value = SimpleNamespace(photo=[SimpleNamespace(file_id="file", file_unique_id="u")])
    settings = Settings("token", "postgresql://db/cats", media_chat_id=123)
    return db, bot, settings


async def test_batch_import_deduplicates_contents_then_preserves_review_edits(tmp_path, monkeypatch):
    db, bot, settings = setup_import(monkeypatch)
    mail = Folder({"/inbox/one.jpg": b"image", "/inbox/renamed.png": b"image", "/inbox/readme.txt": b"hi"})
    path = tmp_path / "drafts.csv"
    stats = await import_mail(mail, db, bot, settings, "/inbox", "/archive", path)
    assert (stats["imported"], stats["reused"], stats["moved"], stats["ignored"]) == (1, 1, 2, 1)
    bot.send_photo.assert_awaited_once()
    rows = await read_csv(path)
    assert len(rows) == 1 and rows[0]["description"] == rows[0]["rarity"] == ""
    rows[0].update(description="Кот на репетиции", rarity="EPIC")
    await asyncio.to_thread(write_review_csv, path, rows)
    await import_mail(mail, db, bot, settings, "/inbox", "/archive", path)
    assert (await read_csv(path))[0]["description"] == "Кот на репетиции"
    assert (await read_csv(path))[0]["rarity"] == "EPIC"
    bot.send_photo.assert_awaited_once()


async def test_retry_after_move_failure_does_not_reupload_photo(tmp_path, monkeypatch):
    db, bot, settings = setup_import(monkeypatch)
    mail = Folder({"/inbox/one.jpg": b"image"})
    mail.fail_move = True
    path = tmp_path / "drafts.csv"
    first = await import_mail(mail, db, bot, settings, "/inbox", "/archive", path)
    assert first["errors"] == 1 and "/inbox/one.jpg" in mail.files
    mail.fail_move = False
    second = await import_mail(mail, db, bot, settings, "/inbox", "/archive", path)
    assert second["reused"] == second["moved"] == 1 and second["errors"] == 0
    bot.send_photo.assert_awaited_once()


async def test_db_failure_never_moves_source_file(tmp_path, monkeypatch):
    db, bot, settings = setup_import(monkeypatch)
    db.fail_write = True
    mail = Folder({"/inbox/one.jpg": b"image"})
    stats = await import_mail(mail, db, bot, settings, "/inbox", "/archive", tmp_path / "drafts.csv")
    assert stats["errors"] == 1 and not mail.moves and "/inbox/one.jpg" in mail.files


async def test_oversized_photo_is_not_downloaded_or_uploaded(tmp_path, monkeypatch):
    db, bot, settings = setup_import(monkeypatch)
    mail = AsyncMock()
    mail.list_files.return_value = [{"path": "/inbox/huge.jpg", "size": MAX_PHOTO_BYTES + 1}]
    stats = await import_mail(mail, db, bot, settings, "/inbox", "/archive", tmp_path / "drafts.csv")
    assert stats["errors"] == 1
    mail.download_photo.assert_not_awaited()
    bot.send_photo.assert_not_awaited()


async def test_export_does_not_overwrite_malformed_existing_csv(tmp_path):
    path = tmp_path / "drafts.csv"
    await asyncio.to_thread(path.write_text, "id,description,rarity\nx,one,COM\nx,two,RARE", encoding="utf-8")
    with pytest.raises(ValueError):
        await export_drafts(path, DraftDB())
    assert "one" in await asyncio.to_thread(path.read_text, encoding="utf-8")


async def test_publish_only_complete_rows_and_never_uploads_again(tmp_path):
    path = tmp_path / "drafts.csv"
    await asyncio.to_thread(
        write_review_csv,
        path,
        [
            {"id": "one", "description": "Кот", "rarity": "RARE", "source_path": ""},
            {"id": "two", "description": "", "rarity": "", "source_path": ""},
        ],
    )
    db = AsyncMock()
    db.publish_cat_drafts.return_value = 1
    assert await publish_drafts(path, db) == (1, 1)
    assert [r["id"] for r in db.publish_cat_drafts.await_args.args[0]] == ["one"]


async def test_local_batch_import_works_without_mail_password_and_deduplicates(tmp_path, monkeypatch):
    db, bot, settings = setup_import(monkeypatch)
    monkeypatch.delenv("MAIL_USER", raising=False)
    monkeypatch.delenv("MAIL_APP_PASSWORD", raising=False)
    inbox, archive, csv_path = tmp_path / "inbox", tmp_path / "imported", tmp_path / "drafts.csv"
    await asyncio.to_thread(inbox.mkdir)
    await asyncio.to_thread((inbox / "Кот.jpg").write_bytes, b"image")
    await asyncio.to_thread((inbox / "Другой кот.png").write_bytes, b"image")
    await asyncio.to_thread((inbox / "readme.txt").write_text, "ignored", encoding="utf-8")
    result = await import_local(db, bot, settings, inbox, archive, csv_path)
    assert (result["imported"], result["reused"], result["moved"], result["errors"]) == (1, 1, 2, 0)
    assert not await asyncio.to_thread((inbox / "Кот.jpg").exists)
    assert await asyncio.to_thread((inbox / "readme.txt").exists)
    archived = await asyncio.to_thread(lambda: list(archive.iterdir()))
    assert len(archived) == 2
    assert await asyncio.gather(*(asyncio.to_thread(p.read_bytes) for p in archived)) == [b"image", b"image"]
    bot.send_photo.assert_awaited_once()
    rows = await read_csv(csv_path)
    assert rows[0]["source_path"].startswith(archive.as_posix())
    assert rows[0]["description"] == rows[0]["rarity"] == ""


def test_local_import_refuses_same_folder_or_drive_root(tmp_path):
    with pytest.raises(ValueError):
        LocalStorage(tmp_path, tmp_path)
    with pytest.raises(ValueError):
        LocalStorage(tmp_path.anchor, tmp_path)


async def test_local_move_never_overwrites_and_checks_source_hash(tmp_path):
    inbox, archive = tmp_path / "inbox", tmp_path / "archive"
    storage = LocalStorage(inbox, archive)
    await storage.mkdirs(inbox)
    await storage.mkdirs(archive)
    source = inbox / "cat.jpg"
    await asyncio.to_thread(source.write_bytes, b"image")
    target = archive / (hashlib.sha256(b"image").hexdigest() + "_cat.jpg")
    await asyncio.to_thread(target.write_bytes, b"keep existing")
    with pytest.raises(FileExistsError):
        await storage.move_file(source, target)
    assert await asyncio.to_thread(target.read_bytes) == b"keep existing"
    wrong_hash = archive / ("0" * 64 + "_cat.jpg")
    with pytest.raises(ValueError):
        await storage.move_file(source, wrong_hash)
    assert await asyncio.to_thread(source.exists)


async def test_local_storage_rejects_paths_outside_configured_folders(tmp_path):
    storage = LocalStorage(tmp_path / "inbox", tmp_path / "archive")
    await storage.mkdirs(storage.inbox)
    await storage.mkdirs(storage.archive)
    outside = tmp_path / "outside.jpg"
    await asyncio.to_thread(outside.write_bytes, b"image")
    with pytest.raises(ValueError):
        await storage.download_photo(outside)
    inside = storage.inbox / "cat.jpg"
    await asyncio.to_thread(inside.write_bytes, b"image")
    with pytest.raises(ValueError):
        await storage.move_file(inside, tmp_path / (hashlib.sha256(b"image").hexdigest() + "_cat.jpg"))
    assert await asyncio.to_thread(inside.exists)


async def test_local_import_skips_symlinks(tmp_path):
    storage = LocalStorage(tmp_path / "inbox", tmp_path / "archive")
    await storage.mkdirs(storage.inbox)
    outside = tmp_path / "outside.jpg"
    await asyncio.to_thread(outside.write_bytes, b"image")
    link = storage.inbox / "link.jpg"
    try:
        await asyncio.to_thread(link.symlink_to, outside)
    except OSError:
        pytest.skip("Creating symlinks is unavailable for this Windows account")
    assert await storage.list_files(storage.inbox) == []
    with pytest.raises(ValueError):
        await storage.download_photo(link)


async def test_local_move_between_volumes_copies_before_removing_source(tmp_path, monkeypatch):
    storage = LocalStorage(tmp_path / "inbox", tmp_path / "archive")
    await storage.mkdirs(storage.inbox)
    await storage.mkdirs(storage.archive)
    source = storage.inbox / "cat.jpg"
    await asyncio.to_thread(source.write_bytes, b"image")
    target = storage.archive / (hashlib.sha256(b"image").hexdigest() + "_cat.jpg")
    monkeypatch.setattr(
        "catbot.local_storage.os.link", MagicMock(side_effect=OSError(errno.EXDEV, "cross-device"))
    )
    await storage.move_file(source, target)
    assert await asyncio.to_thread(target.read_bytes) == b"image"
    assert not await asyncio.to_thread(source.exists)


async def test_export_updates_legacy_csv_source_column_without_losing_labels(tmp_path):
    path = tmp_path / "drafts.csv"
    await asyncio.to_thread(
        path.write_text, "id,description,rarity,mail_path\nx,Kitten,RARE,/old.jpg", encoding="utf-8"
    )
    db = AsyncMock()
    db.cat_drafts.return_value = [{"id": "x", "description": "", "source_path": "/current.jpg"}]
    await export_drafts(path, db)
    row = (await read_csv(path))[0]
    assert row["description"] == "Kitten" and row["rarity"] == "RARE"
    assert row["source_path"] == "/current.jpg" and "mail_path" not in row
