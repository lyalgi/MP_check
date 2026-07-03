from __future__ import annotations

import asyncio
import io
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas import LookupResponse
from app.services.engine import LookupInput, engine

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["Оценка товара"])

MAX_IMAGE_BYTES = 8 * 1024 * 1024  # 8 МБ

# magic-bytes принимаемых форматов
_JPEG_MAGIC = (b"\xff\xd8\xff",)
_PNG_MAGIC = (b"\x89PNG\r\n\x1a\n",)
_WEBP_MAGIC = (b"RIFF",)   # + проверка "WEBP" в offset 8


def _detect_image_kind(data: bytes) -> str | None:
    if any(data.startswith(m) for m in _JPEG_MAGIC):
        return "jpeg"
    if any(data.startswith(m) for m in _PNG_MAGIC):
        return "png"
    if any(data.startswith(m) for m in _WEBP_MAGIC) and len(data) >= 12 and data[8:12] == b"WEBP":
        return "webp"
    # HEIC/HEIF — есть «ftyp» на offset 4, и среди brands heic/heif/heix/mif1
    if len(data) >= 12 and data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in (b"heic", b"heif", b"heix", b"mif1", b"mif2", b"msf1"):
            return "heic"
    return None


def _normalize_image(data: bytes) -> tuple[bytes, str]:
    """Преобразовать в JPEG если нужно. Pillow тянет PNG/WEBP штатно, HEIC — только
    с pillow-heif (опционально). Если HEIC и pillow-heif не установлен — 415."""
    kind = _detect_image_kind(data)
    if kind is None:
        raise HTTPException(415, "Неизвестный формат изображения. Снимок в JPEG/PNG.")
    if kind == "jpeg":
        return data, "image/jpeg"
    if kind == "heic":
        try:
            import pillow_heif  # type: ignore
            pillow_heif.register_heif_opener()
        except ImportError:
            raise HTTPException(
                415,
                "HEIC не поддерживается. Переключите камеру телефона в JPEG (Настройки → Камера → "
                "Форматы → Наиболее совместимый).",
            )
    from PIL import Image  # noqa: PLC0415
    try:
        img = Image.open(io.BytesIO(data))
        if img.mode != "RGB":
            img = img.convert("RGB")
        # Сжимаем по широкой стороне до 1280px — экономим трафик и ускоряем визуальный поиск.
        img.thumbnail((1280, 1280))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85, optimize=True)
        return buf.getvalue(), "image/jpeg"
    except Exception as e:
        logger.warning("не удалось преобразовать изображение: %s", e)
        raise HTTPException(415, f"Не удалось обработать изображение ({kind}): {e}")


@router.post("/lookup", response_model=LookupResponse)
async def lookup(
    purchase_price: Annotated[float, Form(ge=0.01)],
    image: Annotated[UploadFile | None, File(description="Фото товара")] = None,
    seed_url: Annotated[str | None, Form(description="Ссылка на похожий WB-товар")] = None,
    query: Annotated[str | None, Form(description="Уточнение: вес/объём/шт/цвет")] = None,
    nm_ids: Annotated[str | None, Form(description="nm_id с устройства (поиск на телефоне), через запятую")] = None,
    db: Session = Depends(get_db),
):
    image_bytes: bytes | None = None
    if image is not None:
        raw = await image.read()
        if not raw:
            image_bytes = None
        elif len(raw) > MAX_IMAGE_BYTES:
            raise HTTPException(413, f"Слишком большое фото (>{MAX_IMAGE_BYTES // (1024*1024)} МБ)")
        else:
            image_bytes, _ = _normalize_image(raw)

    # nm_id, найденные на телефоне закупщика (визуальный поиск прошёл с его IP).
    seed_nm_ids: list[int] | None = None
    if nm_ids:
        parsed = [int(x) for x in nm_ids.replace(";", ",").split(",") if x.strip().isdigit()]
        seed_nm_ids = parsed[:300] or None

    if not image_bytes and not seed_url and not seed_nm_ids:
        raise HTTPException(422, "Нужно либо фото, либо ссылка на похожий WB-товар")

    inp = LookupInput(
        image_bytes=image_bytes,
        purchase_price=float(purchase_price),
        seed_url=seed_url,
        query=(query or "").strip() or None,
        seed_nm_ids=seed_nm_ids,
    )
    # Жёсткий лимит времени — не даём пайплайну висеть дольше 70с (интерфейс ждёт 80с,
    # запас 10с на сеть/сериализацию). Если выбили лимит — вернём 504,
    # фронт разблокирует кнопку.
    try:
        return await asyncio.wait_for(engine.evaluate(db, inp), timeout=70.0)
    except asyncio.TimeoutError:
        raise HTTPException(
            504,
            "Превышено время обработки (70с). WB или сеть не отвечают — повторите попытку.",
        )
