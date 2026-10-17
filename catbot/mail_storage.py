import asyncio
from pathlib import Path
from urllib.parse import quote

import aiohttp

MAX_PHOTO_BYTES = 10 * 1024 * 1024


def webdav_url(path: str) -> str:
    parts = path.replace("\\", "/").split("/")
    if any(p in {".", ".."} for p in parts) or "\x00" in path:
        raise ValueError("Недопустимый путь WebDAV")
    return "https://webdav.cloud.mail.ru/" + quote("/".join(p for p in parts if p), safe="/")


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
