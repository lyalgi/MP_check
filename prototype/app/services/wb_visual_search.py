"""Визуальный поиск WB — поиск аналогов по фото.

Реверс из официального chrome-extension Wildberries Image Search (CRX-id
bhadlmjencobfbdjhpocebojhhnpemam, content-script.js):

    POST https://search-by-photo.wb.ru/uploadsearch
    multipart/form-data, поле «image» = JPEG/PNG.
    Заголовки:
        RequestUUID:   <случайный uuid4>
        Signature:     base64( AES-CTR( AES-CTR( AES-CTR( msg ))))   x3 итерации
                       msg = "RequestUUID:<uuid>"
                       key = sha256( decode_xor_key() )
                       iv  = 16 random bytes (на каждую итерацию)
        test-properties: ab_testing=false
        userid:        0

    Ответ:
        { status: "OK", engine: "siglip2_sigma",
          result: [ { im_name: <nm_id>, cosine: null }, ... ] }

Архитектура:
    VisualSearchProvider — Protocol.
    Реализации:
      - WBPhotoSearchProvider — реальный, по умолчанию в боевом режиме.
      - MockVisualSearchProvider — фикс. список для офлайн-тестов.
      - HttpVisualSearchProvider — конфигурируемый кастом-URL (для будущего/MPStats).
"""
from __future__ import annotations

import base64
import hashlib
import io
import logging
import os
import re
import uuid
from typing import Protocol

import requests
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from app.settings import settings

logger = logging.getLogger(__name__)

UPLOAD_URL = "https://search-by-photo.wb.ru/uploadsearch"
_SALT = b"b723375b3aac60afa239c149"
_ENCODED_KEY = bytes([
    84, 7, 81, 11, 3, 86, 84, 91, 82, 0, 85, 86, 83, 3, 83, 94, 4, 10, 2, 15, 6, 3, 81, 90,
    7, 5, 7, 4, 1, 82, 5, 87, 4, 85, 89, 80, 82, 0, 89, 7, 85, 87, 5, 12, 87, 6, 82, 9,
    90, 2, 84, 85, 2, 86, 84, 1, 1, 84, 83, 83, 84, 7, 82, 94,
])


def _decode_key() -> bytes:
    return bytes(_ENCODED_KEY[i] ^ _SALT[i % len(_SALT)] for i in range(len(_ENCODED_KEY)))


def _make_signature(message: str) -> str:
    """3 итерации AES-CTR, между ними base64-обмен."""
    aes_key = hashlib.sha256(_decode_key()).digest()
    payload = message.encode("utf-8")
    out: str | None = None
    for _ in range(3):
        iv = os.urandom(16)
        cipher = Cipher(algorithms.AES(aes_key), modes.CTR(iv))
        enc = cipher.encryptor()
        ct = enc.update(payload) + enc.finalize()
        out = base64.b64encode(iv + ct).decode("ascii")
        payload = out.encode("utf-8")
    assert out is not None
    return out


class VisualSearchProvider(Protocol):
    async def search_by_image(self, image_bytes: bytes) -> list[int]: ...
    async def search_by_seed_nm(self, nm_id: int) -> list[int]: ...


class WBPhotoSearchProvider:
    """Реальный визуальный поиск WB."""

    # WB обрабатывает изображение через siglip2-модель — это медленно (5-15с).
    # Поэтому общий request_timeout_seconds (10с) недостаточен. Явный таймаут.
    DEFAULT_TIMEOUT = 30.0

    def __init__(self, timeout: float | None = None):
        self.timeout = timeout or self.DEFAULT_TIMEOUT

    async def search_by_image(self, image_bytes: bytes) -> list[int]:
        import asyncio
        return await asyncio.to_thread(self._sync_search, image_bytes)

    def _sync_search(self, image_bytes: bytes) -> list[int]:
        """Реальный визуальный поиск WB.

        WB часто закрывает соединение посередине запроса (RemoteDisconnected) —
        видимо, периодический rate-limit. Делаем до MAX_ATTEMPTS попыток с
        новым RequestUUID и коротким backoff (0s, 1s) — чтобы не зависнуть
        больше чем на ~35-40 секунд в худшем случае (2 попытки × ~15с + backoff).
        """
        import time as _time

        MAX_ATTEMPTS = 2
        BACKOFF = (0.0, 1.0)
        upload_bytes = _normalize_image_for_upload(image_bytes)
        last_err = None
        for attempt in range(MAX_ATTEMPTS):
            if BACKOFF[attempt] > 0:
                _time.sleep(BACKOFF[attempt])
            request_uuid = str(uuid.uuid4())
            signature = _make_signature(f"RequestUUID:{request_uuid}")
            headers = {
                "Signature": signature,
                "RequestUUID": request_uuid,
                "test-properties": "ab_testing=false",
                "userid": "0",
            }
            files = {"image": ("photo.jpg", upload_bytes, "image/jpeg")}
            try:
                r = requests.post(UPLOAD_URL, headers=headers, files=files, timeout=self.timeout)
            except requests.RequestException as e:
                last_err = e
                logger.warning("ошибка транспорта WB photo search (попытка %d): %s", attempt + 1, e)
                continue
            if r.status_code != 200:
                logger.warning("WB photo search вернул не 200 (попытка %d): %d %s",
                               attempt + 1, r.status_code, r.text[:200])
                last_err = f"HTTP {r.status_code}"
                continue
            try:
                data = r.json()
            except ValueError:
                logger.warning("WB photo search вернул не JSON (попытка %d): %s", attempt + 1, r.text[:200])
                last_err = "non-json"
                continue
            if data.get("status") != "OK":
                logger.warning("WB photo search статус=%s ошибка=%s",
                               data.get("status"), data.get("error"))
                last_err = data.get("status")
                continue
            result = data.get("result") or []
            nm_ids = [int(item["im_name"]) for item in result if "im_name" in item]
            logger.info("визуальный поиск WB вернул %d товаров (движок=%s, попытка=%d)",
                        len(nm_ids), data.get("engine"), attempt + 1)
            return nm_ids
        logger.warning("WB photo search не сработал после %d попыток (последняя ошибка: %s)", MAX_ATTEMPTS, last_err)
        return []

    async def search_by_seed_nm(self, nm_id: int) -> list[int]:
        # Реальный сценарий: пользователь дал nm_id, а не фото.
        # У WB есть «похожие товары» через wbxcatalog-карточки, но это другой эндпоинт.
        # Возвращаем сам seed; engine воспользуется им как единственным аналогом.
        return [nm_id]


class MockVisualSearchProvider:
    """Детерминированный список SKU. Только для офлайн-тестов и разработки."""

    _DEFAULT = [143489486, 162345678, 178901234, 191234567, 200000001]

    def __init__(self, nm_ids: list[int] | None = None):
        self.nm_ids = list(nm_ids) if nm_ids is not None else list(self._DEFAULT)

    async def search_by_image(self, image_bytes: bytes) -> list[int]:
        return list(self.nm_ids)

    async def search_by_seed_nm(self, nm_id: int) -> list[int]:
        if not self.nm_ids:
            return []
        return [nm_id, *self.nm_ids]


class HttpVisualSearchProvider:
    """Кастомный URL для будущих провайдеров (например, MPStats)."""

    def __init__(self, url: str, timeout: float | None = None):
        self.url = url
        self.timeout = timeout or settings.request_timeout_seconds

    async def search_by_image(self, image_bytes: bytes) -> list[int]:
        if not self.url:
            logger.warning("HttpVisualSearchProvider: URL не задан — возвращаем пусто")
            return []
        import httpx
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as c:
                r = await c.post(self.url, files={"file": ("photo.jpg", image_bytes, "image/jpeg")})
                r.raise_for_status()
                data = r.json()
        except Exception as e:
            logger.warning("HttpVisualSearchProvider не сработал: %s", e)
            return []
        return _extract_nm_ids(data)

    async def search_by_seed_nm(self, nm_id: int) -> list[int]:
        return []


def _extract_nm_ids(data) -> list[int]:
    if isinstance(data, list):
        return [int(x) for x in data if str(x).isdigit()]
    if isinstance(data, dict):
        for key in ("nm_ids", "products", "items", "data", "result"):
            v = data.get(key)
            if isinstance(v, list):
                out: list[int] = []
                for x in v:
                    if isinstance(x, (int, str)) and str(x).isdigit():
                        out.append(int(x))
                    elif isinstance(x, dict):
                        for k in ("nm_id", "nmId", "id", "im_name"):
                            if k in x and str(x[k]).isdigit():
                                out.append(int(x[k]))
                                break
                if out:
                    return out
    return []


def _normalize_image_for_upload(image_bytes: bytes) -> bytes:
    """Преобразовать фото закупщика/1688 в обычный JPEG для поиска WB."""
    try:
        try:
            import pillow_heif

            pillow_heif.register_heif_opener()
        except Exception:
            pass
        from PIL import Image, ImageOps

        with Image.open(io.BytesIO(image_bytes)) as img:
            img = ImageOps.exif_transpose(img).convert("RGB")
            # WB siglip ужимает вход до ~384px внутри себя, поэтому 800px/q75
            # не теряют качество совпадения, но в разы режут прокси-трафик загрузки.
            px = settings.visual_upload_max_px
            img.thumbnail((px, px), Image.Resampling.LANCZOS)
            out = io.BytesIO()
            img.save(out, format="JPEG", quality=settings.visual_upload_quality, optimize=True)
            return out.getvalue()
    except Exception as e:  # noqa: BLE001
        logger.warning("не удалось нормализовать изображение, отправляем исходные байты: %s", e)
        return image_bytes


_WB_URL_RE = re.compile(r"(?:wildberries\.ru/catalog/|wb\.ru/catalog/|nm[=/])(\d{4,12})")


def parse_wb_nm_from_url(url_or_text: str) -> int | None:
    if not url_or_text:
        return None
    s = url_or_text.strip()
    if s.isdigit() and 4 <= len(s) <= 12:
        return int(s)
    m = _WB_URL_RE.search(s)
    return int(m.group(1)) if m else None


def get_visual_search_provider() -> VisualSearchProvider:
    name = settings.visual_search_provider
    if name == "wb_photo":
        return WBPhotoSearchProvider()
    if name == "http":
        return HttpVisualSearchProvider(url=settings.visual_search_url)
    if name == "mock":
        return MockVisualSearchProvider()
    logger.warning("неизвестный visual_search_provider=%r, используем wb_photo", name)
    return WBPhotoSearchProvider()
