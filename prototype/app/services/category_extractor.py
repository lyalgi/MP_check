"""Извлечение категории WB из карточки SKU.

Используем публичный card.wb.ru/cards/v2/detail — принимает до ~50 nm
за запрос. Запросы идут через app.services.wb_http (requests + прокси-ротация).

Алгоритм (по ТЗ, шаг 3):
  1. Получить детали для списка SKU (один HTTP-запрос на пачку 50).
  2. Среди фильтрованных кандидатов — самый «продающийся» (макс. feedbacks).
  3. Из самого популярного — subjectId/subjectName/subjectParent.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable

from app.schemas import AnalogSku
from app.services import wb_http
from app.services.providers.wb_public import DEST, _product_to_analog
from app.settings import settings

logger = logging.getLogger(__name__)

CARD_DETAIL_URL = "https://card.wb.ru/cards/v4/detail"


@dataclass(frozen=True)
class CardCategory:
    nm_id: int
    subject_id: int | None
    subject_name: str | None
    parent_id: int | None
    parent_name: str | None
    brand: str | None


async def fetch_cards_raw(nm_ids: Iterable[int]) -> list[dict]:
    """Сырые JSON-карточки (поле subjectId/subjectName в них есть)."""
    nm_ids = [int(n) for n in nm_ids]
    if not nm_ids:
        return []
    out: list[dict] = []
    for chunk_start in range(0, len(nm_ids), 50):
        chunk = nm_ids[chunk_start:chunk_start + 50]
        params = {
            "appType": "1",
            "curr": "rub",
            "dest": DEST,
            "spp": "30",
            "nm": ";".join(str(n) for n in chunk),
        }
        data = await wb_http.get_json(CARD_DETAIL_URL, params, timeout=settings.request_timeout_seconds)
        if data is None:
            continue
        # v4: products лежат на верхнем уровне; v2/v9: внутри data.products
        products = data.get("products") or ((data.get("data") or {}).get("products") or [])
        out.extend(products)
    return out


async def fetch_cards(nm_ids: Iterable[int]) -> list[AnalogSku]:
    raws = await fetch_cards_raw(nm_ids)
    return [_product_to_analog(p) for p in raws]


def extract_category(products_raw: list[dict], db=None) -> CardCategory | None:
    """Из карточки достать subjectId. Имена резолвим из таблицы wb_subjects.
    В v4-ответе card.wb.ru есть только id (subjectId/subjectParentId), имена приходят
    из загруженного справочника subjects.json."""
    if not products_raw:
        return None
    p = products_raw[0]
    subj_id = int(p["subjectId"]) if p.get("subjectId") is not None else None
    parent_id = int(p["subjectParentId"]) if p.get("subjectParentId") is not None else None
    subject_name = p.get("subjectName") or None
    parent_name = p.get("subjectParent") or None
    if db is not None and (subj_id and (not subject_name or not parent_name)):
        from app.services import subjects as subjects_svc  # ленивый импорт
        info = subjects_svc.lookup(db, subj_id)
        if info:
            subject_name = info.name
            parent_name = info.parent_name
    return CardCategory(
        nm_id=int(p.get("id") or p.get("nmId") or 0),
        subject_id=subj_id,
        subject_name=subject_name,
        parent_id=parent_id,
        parent_name=parent_name,
        brand=p.get("brand") or None,
    )


def pick_top_by_feedbacks(items: list[AnalogSku]) -> AnalogSku | None:
    """Стабильный выбор самого «продающегося» SKU.
    Разрешение равенств: отзывы → рейтинг → остатки → nm_id (детерминированно)."""
    if not items:
        return None
    return max(items, key=lambda a: (a.feedbacks, a.rating or 0.0, a.stocks or 0, a.nm_id))


def vote_subject_id(raw_cards: list[dict], analogs: list[AnalogSku], min_share: float = 0.6) -> tuple[int | None, float]:
    """Взвешенное голосование по subjectId.
    Каждая карточка отдаёт голос своим feedbacks. Если доминирующий subject набирает
    долю ≥ min_share от суммы feedbacks — возвращаем его. Иначе (None, share_лидера).

    Это устойчиво к «шумной» выдаче визуального поиска, где 1 случайный лидер из
    смежной категории мог увести эталон."""
    if not raw_cards or not analogs:
        return None, 0.0
    weights: dict[int, float] = {}
    fb_by_nm = {a.nm_id: a.feedbacks for a in analogs}
    for p in raw_cards:
        sid = p.get("subjectId")
        nm = int(p.get("id") or 0)
        if sid is None or nm == 0:
            continue
        w = float(fb_by_nm.get(nm, p.get("feedbacks") or 0))
        if w <= 0:
            continue
        weights[int(sid)] = weights.get(int(sid), 0.0) + w
    if not weights:
        return None, 0.0
    total = sum(weights.values())
    leader, leader_w = max(weights.items(), key=lambda kv: kv[1])
    share = leader_w / total if total > 0 else 0.0
    if share >= min_share:
        return leader, share
    return None, share


def pick_raw_card_for_subject(raw_cards: list[dict], subject_id: int) -> dict | None:
    """Из RAW-карточек выбрать представителя нужного subjectId — самый отзывчивый."""
    candidates = [p for p in raw_cards if int(p.get("subjectId") or 0) == subject_id]
    if not candidates:
        return None
    return max(candidates, key=lambda p: (
        int(p.get("feedbacks") or 0),
        float(p.get("reviewRating") or 0),
        int(p.get("id") or 0),
    ))
