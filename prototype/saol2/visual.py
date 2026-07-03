"""Визуальный поиск похожих товаров (ВБ siglip2) — по фото или по артикулу.

Похожие ВБ-поиском визуально релевантны (мишка → мишки), в отличие от MPStats
`similar` (соседи по категории: мишка → подушка-кирпич). Данные по найденным
артикулам берём из MPStats.

Сам визуальный поиск — в saol_core.visual_search (реверс мобильного эндпоинта ВБ).
"""
from __future__ import annotations

import io
import logging

import requests

logger = logging.getLogger(__name__)

_UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148"


def _to_jpeg(image_bytes: bytes) -> bytes:
    """webp/heic/png → JPEG (ВБ-поиск ждёт обычный JPEG). Без PIL — отдаём как есть."""
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=85)
        return buf.getvalue()
    except Exception:
        return image_bytes


def search_by_image(image_bytes: bytes) -> list[int]:
    """Фото → артикулы визуально похожих карточек ВБ."""
    import saol_core
    try:
        return saol_core.visual_search(_to_jpeg(image_bytes))
    except Exception as e:  # noqa: BLE001
        logger.warning("визуальный поиск по фото упал: %s", e)
        return []


def _seed_image_url(client, nm: int) -> str | None:
    full = client.item_full(nm)
    photos = ((full or {}).get("photo") or {}).get("list") or []
    if not photos:
        return None
    return photos[0].get("f") or photos[0].get("t")


def search_similar_by_nm(client, nm: int) -> list[int]:
    """Артикул → его картинка из MPStats → визуальный поиск похожих ВБ."""
    url = _seed_image_url(client, nm)
    if not url:
        logger.warning("нет картинки для nm=%s — визуальный поиск пропущен", nm)
        return []
    try:
        img = requests.get(url, timeout=20, headers={"User-Agent": _UA}).content
    except Exception as e:  # noqa: BLE001
        logger.warning("не удалось скачать картинку nm=%s: %s", nm, e)
        return []
    return search_by_image(img)
