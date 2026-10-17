"""Local batch-import folders; filesystem work runs outside the event loop."""

import asyncio
import errno
import hashlib
import os
from pathlib import Path

from catbot.mail_storage import MAX_PHOTO_BYTES


class LocalStorage:
    def __init__(self, inbox, archive):
        self.inbox, self.archive = Path(inbox).resolve(), Path(archive).resolve()
        if self.inbox == self.archive or any(p == Path(p.anchor) for p in (self.inbox, self.archive)):
            raise ValueError("Укажи разные папки входящих и архива, не корень диска")

    def _source(self, path):
        source = Path(path)
        if source.is_symlink() or source.resolve().parent != self.inbox or not source.is_file():
            raise ValueError("Файл должен находиться непосредственно в папке входящих")
        return source.resolve()

    def _target(self, path):
        target = Path(path).resolve()
        if target.parent != self.archive:
            raise ValueError("Файл назначения должен находиться в папке архива")
        return target

    def _mkdirs(self, path):
        folder = Path(path).resolve()
        if folder not in (self.inbox, self.archive):
            raise ValueError("Папка находится вне настроенных путей импорта")
        folder.mkdir(parents=True, exist_ok=True)

    async def mkdirs(self, path):
        await asyncio.to_thread(self._mkdirs, path)

    def _list(self):
        result = []
        for path in self.inbox.iterdir():
            if path.is_symlink() or not path.is_file():
                continue
            source = self._source(path)
            result.append({"path": source.as_posix(), "size": source.stat().st_size})
        return sorted(result, key=lambda entry: entry["path"])

    async def list_files(self, folder):
        if await asyncio.to_thread(Path(folder).resolve) != self.inbox:
            raise ValueError("Можно читать только папку входящих")
        return await asyncio.to_thread(self._list)

    def _read(self, path):
        with self._source(path).open("rb") as handle:
            data = handle.read(MAX_PHOTO_BYTES + 1)
        if not data or len(data) > MAX_PHOTO_BYTES:
            raise ValueError("Нужен непустой файл размером не больше 10 МБ")
        return data

    async def download_photo(self, path):
        return await asyncio.to_thread(self._read, path)

    def _move(self, source, destination):
        # Validate BOTH resolved absolute paths before moving any file.
        source, target = self._source(source), self._target(destination)
        original = source.stat()
        data = self._read(source)
        if hashlib.sha256(data).hexdigest() != target.name.partition("_")[0]:
            raise ValueError("Исходный файл изменился во время импорта; повтори запуск")
        try:
            os.link(source, target)  # Atomic no-overwrite, including on POSIX.
        except FileExistsError:
            raise
        except OSError as error:
            if error.errno not in {errno.EXDEV, errno.EPERM, errno.EACCES, errno.ENOTSUP}:
                raise
            # Different volumes or a filesystem without hard links. Exclusive
            # creation never replaces an existing archive file.
            handle = target.open("xb")
            try:
                with handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
            except BaseException:
                target.unlink(missing_ok=True)
                raise
        current = source.stat()
        if (original.st_ino, original.st_size, original.st_mtime_ns) != (
            current.st_ino,
            current.st_size,
            current.st_mtime_ns,
        ):
            raise ValueError("Исходный файл изменился: копия сохранена, исходник оставлен на месте")
        source.unlink()

    async def move_file(self, source, destination):
        await asyncio.to_thread(self._move, source, destination)
