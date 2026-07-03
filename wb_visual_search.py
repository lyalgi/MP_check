#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Визуальный поиск товаров Wildberries ПО ФОТО — самостоятельный модуль.

ЧТО ДЕЛАЕТ
    Вход:  байты картинки (jpg/png/webp/...).
    Выход: список артикулов (nm_id) визуально похожих товаров на Wildberries.
    Внутри — как на сайте ВБ, ДВА шага:
      1) category-detection.wildberries.ru/api/triton_predict_sync (без подписи) →
         определяет категорию фото (метку, напр. "dress");
      2) search-by-photo.wb.ru/uploadsearch?label_list=<метка> (с подписью запроса) →
         ищет похожие, СУЖАЯ выдачу до этой категории — поэтому точнее.
    Шаг 1 можно выключить (auto_label=False) или задать метку вручную (label_list="...").

ЗАВИСИМОСТИ
    pip install requests cryptography
    (Pillow — опционально, только чтобы привести экзотические форматы к JPEG.)

ЗАПУСК ИЗ КОНСОЛИ
    python wb_visual_search.py путь/к/фото.jpg
    -> печатает найденные артикулы и ссылки на карточки ВБ.

ИСПОЛЬЗОВАНИЕ В КОДЕ
    from wb_visual_search import search_by_photo
    nm_ids = search_by_photo(open("photo.jpg", "rb").read())   # -> [123456, ...]

ПРО ПОДПИСЬ И ЛЕГАЛЬНОСТЬ
    Эндпоинт принимает заголовок `Signature` = AES-CTR(message), где ключ — это
    ПУБЛИЧНЫЙ ключ из официального chrome-расширения «WB Image Search» (он уже
    общедоступен, не секрет и ничей токен). Никаких личных ключей/токенов тут нет.
    Эндпоинт неофициальный (реверс мобильного приложения) и может в любой момент
    смениться — держите вызов за одной функцией, как здесь. Используйте ответственно
    и в рамках правил Wildberries.

ВАЖНО ПРО IP
    С серверных IP ВБ-поиск иногда отдаёт 403/429 (анти-бот). Надёжнее звать его
    с «пользовательского» IP (например, прямо из браузера/телефона) или через прокси.
    Есть и браузерный (JavaScript) вариант той же логики — спросите, вышлю отдельно.
"""
from __future__ import annotations

import base64
import hashlib
import io
import os
import sys
import uuid

import requests
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# ─────────────────────────── эндпоинт и ключ подписи ────────────────────────
UPLOAD_URL = "https://search-by-photo.wb.ru/uploadsearch"
# Детектор категории по фото (БЕЗ подписи). Сайт зовёт его ПЕРЕД поиском и подставляет
# метку в `?label_list=...` — поэтому выдача сайта точнее/уже нашей. Поле файла: image_file.
DETECT_URL = "https://category-detection.wildberries.ru/api/triton_predict_sync"
TIMEOUT = 30.0

# Ключ подписи из chrome-расширения WB Image Search (публичный, не секрет):
# реальный ключ = _ENCODED_KEY XOR _SALT, затем SHA-256 → ключ AES-256.
_SALT = b"b723375b3aac60afa239c149"
_ENCODED_KEY = bytes([
    84, 7, 81, 11, 3, 86, 84, 91, 82, 0, 85, 86, 83, 3, 83, 94, 4, 10, 2, 15, 6, 3, 81, 90,
    7, 5, 7, 4, 1, 82, 5, 87, 4, 85, 89, 80, 82, 0, 89, 7, 85, 87, 5, 12, 87, 6, 82, 9,
    90, 2, 84, 85, 2, 86, 84, 1, 1, 84, 83, 83, 84, 7, 82, 94,
])


def _signature(message: str) -> str:
    """Подпись запроса: 3 раунда AES-CTR со случайным IV, каждый раз base64(IV+ct)."""
    key = bytes(_ENCODED_KEY[i] ^ _SALT[i % len(_SALT)] for i in range(len(_ENCODED_KEY)))
    aes_key = hashlib.sha256(key).digest()
    payload = message.encode("utf-8")
    out = ""
    for _ in range(3):
        iv = os.urandom(16)
        enc = Cipher(algorithms.AES(aes_key), modes.CTR(iv)).encryptor()
        ct = enc.update(payload) + enc.finalize()
        out = base64.b64encode(iv + ct).decode("ascii")
        payload = out.encode("utf-8")
    return out


def _to_jpeg(image_bytes: bytes) -> bytes:
    """webp/heic/png → JPEG (поиск ждёт обычный JPEG). Без Pillow — отдаём как есть."""
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=85)
        return buf.getvalue()
    except Exception:
        return image_bytes


def detect_categories(image_bytes: bytes) -> list[str]:
    """Фото → список категорий-меток (как «dress»), которые ВБ кладёт в `label_list`.
    Без подписи. Пусто, если детектор не уверен (логотип/нестандартный кадр).

    Формат ответа эндпоинта: {"predictions":[...], "predictions_ocr":[...], "engine":...}.
    Элементы `predictions` приводим к строкам-меткам (бывают строкой или объектом)."""
    try:
        r = requests.post(
            DETECT_URL, files={"image_file": ("photo.jpg", _to_jpeg(image_bytes), "image/jpeg")},
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            return []
        out: list[str] = []
        for p in (r.json().get("predictions") or []):
            if isinstance(p, str):
                out.append(p)
            elif isinstance(p, dict):
                lbl = p.get("label") or p.get("class") or p.get("name") or p.get("category")
                if lbl:
                    out.append(str(lbl))
        return out
    except Exception:
        return []


def search_by_photo(image_bytes: bytes, label_list: str | None = None,
                    auto_label: bool = True, retries: int = 2) -> list[int]:
    """Байты картинки → список артикулов (nm_id) визуально похожих товаров ВБ.

    КАК НА САЙТЕ (рекомендуется): `auto_label=True` — сначала определяем категорию фото
    (detect_categories) и подставляем её в `?label_list=...`, чтобы выдача была точнее/уже
    (иначе результаты шире, чем на сайте). Можно задать метку вручную: `label_list="dress"`,
    либо выключить шаг: `auto_label=False`.

    Пустой список — если поиск ничего не вернул или эндпоинт ответил ошибкой
    (анти-бот, недоступность, нераспознанное фото). Исключения наружу не пробрасываем.
    """
    image_bytes = _to_jpeg(image_bytes)
    if label_list is None and auto_label:
        cats = detect_categories(image_bytes)
        label_list = cats[0] if cats else None   # сайт подставляет одну метку
    params = {"label_list": label_list} if label_list else None
    for _ in range(max(1, retries)):
        ruid = str(uuid.uuid4())
        headers = {
            "Signature": _signature(f"RequestUUID:{ruid}"),
            "RequestUUID": ruid,
            "test-properties": "ab_testing=false",
            "userid": "0",
        }
        try:
            r = requests.post(
                UPLOAD_URL, params=params, headers=headers,
                files={"image": ("photo.jpg", image_bytes, "image/jpeg")},
                timeout=TIMEOUT,
            )
            if r.status_code == 200:
                data = r.json()
                if data.get("status") == "OK":
                    return [int(i["im_name"]) for i in (data.get("result") or []) if "im_name" in i]
        except Exception:
            pass
    return []


def search_by_file(path: str) -> list[int]:
    """Удобная обёртка: путь к файлу → артикулы."""
    with open(path, "rb") as f:
        return search_by_photo(f.read())


# ─────────────────────────── демо из консоли ────────────────────────────────
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Использование: python wb_visual_search.py путь/к/фото.jpg")
        raise SystemExit(2)

    with open(sys.argv[1], "rb") as f:
        img = f.read()
    cats = detect_categories(img)
    print("Категория по фото:", ", ".join(cats) if cats else "(не определена → поиск без сужения)")
    nm_ids = search_by_photo(img)
    if not nm_ids:
        print("Похожие не найдены (или эндпоинт недоступен/заблокировал запрос). "
              "Попробуйте другое фото, прокси или вызов с пользовательского IP.")
        raise SystemExit(1)

    print(f"Найдено артикулов: {len(nm_ids)}\n")
    for nm in nm_ids[:20]:
        print(f"  {nm}  https://www.wildberries.ru/catalog/{nm}/detail.aspx")
    if len(nm_ids) > 20:
        print(f"  ... и ещё {len(nm_ids) - 20}")
