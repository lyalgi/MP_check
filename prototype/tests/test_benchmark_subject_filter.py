"""Бенчмарк не должен подтягиваться из ЧУЖОЙ категории.

Баг: фото замазки (Корректирующие ленты, subj 3779) → menu-shard указал на
одежду → в примерах майки/трусы/корсеты, медиана рынка 1100₽. Фикс: топ
фильтруется по resolved subject_id; чужой шард отбрасывается.
"""
from __future__ import annotations

import asyncio

from app.services import wb_http
from app.services.providers import wb_public


def _prod(nm, subj, price, fb=50):
    return {
        "id": nm, "subjectId": subj, "name": f"sku-{nm}", "brand": "B",
        "priceU": int(price * 100), "salePriceU": int(price * 90),
        "feedbacks": fb, "reviewRating": 4.5, "totalQuantity": 10,
    }


def _patch(monkeypatch, products):
    p = wb_public.WBPublicProvider()

    async def fake_lookup(path):
        return {"shard": "x", "query": "cat=1"}

    async def fake_get(url, params, timeout=None):
        return {"products": products}

    monkeypatch.setattr(p, "lookup_category", fake_lookup)
    monkeypatch.setattr(wb_http, "get_json", fake_get)
    return p


def test_discards_foreign_subject_shard(monkeypatch):
    # шард вернул одежду (subj 999) на запрос канцелярии (нужен 3779)
    p = _patch(monkeypatch, [_prod(1, 999, 1100), _prod(2, 999, 1200), _prod(3, 999, 1000)])
    res = asyncio.run(p.top_n_in_category("Канц/Ленты", limit=100, subject_id=3779))
    assert res == []


def test_keeps_matching_subject(monkeypatch):
    # есть свои (3779, ~200₽) + чужая одежда (999, 1100₽) → одежду отбрасываем
    p = _patch(monkeypatch, [_prod(1, 3779, 200), _prod(2, 3779, 220), _prod(3, 999, 1100)])
    res = asyncio.run(p.top_n_in_category("Канц/Ленты", limit=100, subject_id=3779))
    assert len(res) == 2
    assert all(a.price <= 300 for a in res)   # чужая одежда (1100₽) выкинута


def test_no_subject_id_keeps_all(monkeypatch):
    # без subject_id фильтр не применяется (обратная совместимость)
    p = _patch(monkeypatch, [_prod(1, 999, 1100), _prod(2, 3779, 200)])
    res = asyncio.run(p.top_n_in_category("Канц/Ленты", limit=100))
    assert len(res) == 2
