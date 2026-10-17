import asyncio
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit
from xml.etree import ElementTree

import aiohttp

MAX_PHOTO_BYTES = 10 * 1024 * 1024
MAX_LIST_BYTES = 4 * 1024 * 1024


def webdav_url(path: str) -> str:
    parts = path.replace("\\", "/").split("/")
    if any(p in {".", ".."} for p in parts) or "\x00" in path:
        raise ValueError("Недопустимый путь WebDAV")
    return "https://webdav.cloud.mail.ru/" + quote("/".join(p for p in parts if p), safe="/")


def mail_path(path: str) -> str:
    return unquote(urlsplit(webdav_url(path)).path).rstrip("/") or "/"


def parse_listing(data: bytes, folder: str):
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise ValueError("Недопустимый XML WebDAV")
    root = ElementTree.fromstring(data)
    parent = mail_path(folder).rstrip("/")
    result = []
    for response in root.findall("{DAV:}response"):
        href = urlsplit(response.findtext("{DAV:}href", default=""))
        if href.netloc and (href.scheme != "https" or href.netloc != "webdav.cloud.mail.ru"):
            raise ValueError("Недопустимый адрес в ответе WebDAV")
        path = mail_path(unquote(href.path))
        if path.rpartition("/")[0] != parent or not path.rpartition("/")[2]:
            continue  # Depth: 1 only. Never consume descendants or unrelated folders.
        for propstat in response.findall("{DAV:}propstat"):
            if " 200 " not in propstat.findtext("{DAV:}status", default=""):
                continue
            prop = propstat.find("{DAV:}prop")
            if prop is None or prop.find("{DAV:}resourcetype/{DAV:}collection") is not None:
                continue
            length = prop.findtext("{DAV:}getcontentlength")
            result.append({"path": path, "size": int(length) if length else None})
            break
    return sorted(result, key=lambda entry: entry["path"])


class MailStorage:
    def __init__(self, user: str, app_password: str):
        if not user or not app_password:
            raise ValueError("Заполните MAIL_USER и MAIL_APP_PASSWORD")
        self.auth = aiohttp.BasicAuth(user, app_password)
        self.session = None

    async def __aenter__(self):
        self.session = aiohttp.ClientSession(
            auth=self.auth, timeout=aiohttp.ClientTimeout(total=600, connect=20, sock_read=60)
        )
        return self

    async def __aexit__(self, *args):
        await self.session.close()

    async def list_files(self, folder: str):
        body = (
            b'<d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/><d:getcontentlength/></d:prop></d:propfind>'
        )
        async with self.session.request(
            "PROPFIND",
            webdav_url(folder),
            data=body,
            headers={"Depth": "1", "Content-Type": "application/xml"},
            allow_redirects=False,
        ) as response:
            if response.status != 207:
                raise RuntimeError(f"WebDAV PROPFIND: HTTP {response.status}")
            data = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                data.extend(chunk)
                if len(data) > MAX_LIST_BYTES:
                    raise ValueError("Слишком большой список файлов; раздели импорт на несколько папок")
        return await asyncio.to_thread(parse_listing, bytes(data), folder)

    async def move_file(self, source: str, destination: str):
        async with self.session.request(
            "MOVE",
            webdav_url(source),
            headers={"Destination": webdav_url(destination), "Overwrite": "F"},
            allow_redirects=False,
        ) as response:
            if response.status == 412:
                raise FileExistsError("Файл с таким именем уже есть в архиве Mail")
            if response.status not in {201, 204}:
                raise RuntimeError(f"WebDAV MOVE: HTTP {response.status}")

    async def download_photo(self, path: str) -> bytes:
        async with self.session.get(webdav_url(path), allow_redirects=False) as response:
            if response.status != 200:
                raise RuntimeError(f"WebDAV download: HTTP {response.status}")
            if response.content_length and response.content_length > MAX_PHOTO_BYTES:
                raise ValueError("Фото превышает 10 МБ")
            data = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                data.extend(chunk)
                if len(data) > MAX_PHOTO_BYTES:
                    raise ValueError("Фото превышает 10 МБ")
            if not data:
                raise ValueError("Пустой файл")
            return bytes(data)

    async def mkdirs(self, path: str):
        parts = path.strip("/").split("/")
        for i in range(1, len(parts) + 1):
            async with self.session.request(
                "MKCOL", webdav_url("/".join(parts[:i])), allow_redirects=False
            ) as r:
                if r.status not in {201, 405}:
                    raise RuntimeError(f"WebDAV MKCOL: HTTP {r.status}")

    async def upload_file(self, local_path: Path, remote_path: str):
        async def chunks():
            handle = await asyncio.to_thread(local_path.open, "rb")
            try:
                while chunk := await asyncio.to_thread(handle.read, 256 * 1024):
                    yield chunk
            finally:
                await asyncio.to_thread(handle.close)

        size = (await asyncio.to_thread(local_path.stat)).st_size
        async with self.session.put(
            webdav_url(remote_path),
            data=chunks(),
            headers={"Content-Length": str(size)},
            allow_redirects=False,
        ) as response:
            if response.status not in {200, 201, 204}:
                raise RuntimeError(f"WebDAV upload: HTTP {response.status}")
