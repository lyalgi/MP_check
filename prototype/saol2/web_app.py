#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Бэкенд v2 для СУЩЕСТВУЮЩЕГО мобильного UI (web/) — с фото.

UI (web/app.js) делает поиск по фото на телефоне и шлёт сюда `nm_ids`, либо
(резерв) само фото `image`, либо ссылку `seed_url`. Мы прогоняем это через
движок v2 (MPStats) и отдаём ответ в полях, которые ждёт app.js.

Запуск (из папки prototype):  python -m saol2.serve   →  http://localhost:8765
"""
from __future__ import annotations

import re
from pathlib import Path

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from saol2.pipeline import analyze
from saol2.scoring import Settings

WEB_DIR = Path(__file__).resolve().parents[1] / "web"

app = FastAPI(title="SAOL v2")
if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


@app.get("/")
def index():
    return FileResponse(str(WEB_DIR / "index.html"))


_NM_RE = re.compile(r"(?:catalog/|nm[=/])(\d{4,12})")


def _parse_nm(url: str) -> int | None:
    s = (url or "").strip()
    if s.isdigit() and 4 <= len(s) <= 12:
        return int(s)
    m = _NM_RE.search(s)
    return int(m.group(1)) if m else None


@app.post("/api/v1/lookup")
async def lookup(
    purchase_price: float = Form(...),
    nm_ids: str = Form(None),
    seed_url: str = Form(None),
    query: str = Form(None),
    image: UploadFile = File(None),
):
    from saol2 import visual
    from saol2.mpstats import MPStats

    nms: list[int] | None = None
    seed_nm: int | None = None
    if nm_ids:  # артикулы, найденные визуальным поиском на телефоне
        nms = [int(x) for x in nm_ids.replace(" ", "").split(",") if x]
    elif image is not None:  # резерв: визуальный поиск ВБ на сервере по фото
        nms = visual.search_by_image(await image.read())
    elif seed_url:  # ссылка/артикул: его картинка → визуально похожие
        seed_nm = _parse_nm(seed_url)
        if seed_nm:
            nms = visual.search_similar_by_nm(MPStats(), seed_nm)

    if not nms:
        return JSONResponse(_empty("Не удалось найти похожие. Переснимите фото или вставьте другую ссылку."))

    result = analyze(nms=nms, seed_nm=seed_nm, purchase_price=purchase_price,
                     settings=Settings(), query=query)
    return JSONResponse(_to_ui(result))


# ─────────────── маппинг v2 → поля, которые читает web/app.js ───────────────
_ADVICE = {
    "STRONG": "Сильный спрос на WB. Можно расширенный тест.",
    "GREEN": "Хороший спрос и запас наценки. Брать на тест.",
    "YELLOW": "Спрос есть, но средний — возьми небольшую пробу.",
    "RED": "Слабый спрос или нет запаса наценки — не брать.",
    "UNKNOWN": "Данных не хватает — переснимите фото или вставьте ссылку.",
}


def _empty(msg: str) -> dict:
    return {"verdict": "UNKNOWN", "advice": msg, "verdict_reasons": [], "examples": []}


def _demand_word(r: dict) -> str:
    """Качественное слово спроса — по абсолюту (выручка типа ÷ денежный пол). Позиция в
    категории теперь ≈ всегда средняя (ниша≈категория), поэтому основа — деньги типа."""
    a = r.get("abs_demand") or 0
    return "GREEN" if a >= 1.5 else "YELLOW" if a >= 0.7 else "RED"


def _to_ui(r: dict) -> dict:
    if r.get("error"):
        return _empty(r["error"])
    subj = r.get("subject_name") or ""
    parent, _, sub = subj.partition(" / ")
    if not sub:
        parent, sub = None, subj or None

    examples = []
    for e in r.get("examples") or []:
        examples.append({
            "nm_id": e.get("nm"),
            "name": e.get("name"),
            "url": f"https://www.wildberries.ru/catalog/{e.get('nm')}/detail.aspx",
            "price": e.get("price"),
            "image": e.get("image"),
            "orders_month": e.get("orders_month"),
            "redeemed_month": e.get("redeemed_month"),
            "buyout_pct": e.get("buyout_pct"),
        })

    seed = r.get("seed") or {}
    return {
        "verdict": r.get("verdict", "UNKNOWN"),
        "provisional": r.get("provisional", True),
        "niche_scope": r.get("niche_scope"),   # 'vid' узнан вид | 'type' широко (вид не распознан)
        "liquidity_score": r.get("score_100"),
        "category_pct": r.get("category_pct"),
        "abs_demand": r.get("abs_demand"),
        "test_units": r.get("test_units"),
        # спрос — теперь по ВЫКУПАМ
        "wb_demand_units_month": r.get("redeemed_month_median"),
        "wb_orders_units_month": r.get("orders_month_median"),
        "lead_orders_month": r.get("lead_orders_month"),
        "lead_revenue_month": r.get("lead_revenue_month"),
        "wb_demand_verdict": _demand_word(r),
        "ratio_to_top": r.get("ratio_to_top"),
        "buyout_pct_median": r.get("buyout_pct_median"),
        "market_price_median": r.get("market_price_median"),
        "markup": r.get("markup"),
        "margin_pct": r.get("margin_pct"),
        # тренд + экономика
        "trend_label": r.get("trend_label"),
        "trend_ratio": r.get("trend_ratio"),
        "niche_revenue_month": r.get("niche_revenue_month"),
        "test_capital": r.get("test_capital"),
        "potential_margin": r.get("potential_margin"),
        "price_segment": r.get("price_segment"),
        "size_spread": r.get("size_spread"),
        "advice": _ADVICE.get(r.get("verdict", "UNKNOWN"), ""),
        "verdict_reasons": r.get("reasons") or [],
        "wb_subject_name": sub,
        "wb_parent_name": parent,
        "subject_vote_share": r.get("vote_share"),
        # история Разноторга в v2 пока не подключена (сырые данные) — следующий этап
        "retail_history_markup": None,
        "retail_history_profitability": None,
        # сезонность и «твой товар» — для расширенного показа
        "seed": {
            "nm_id": seed.get("nm"), "name": seed.get("name"), "image": seed.get("image"),
            "orders_month": seed.get("orders_month"), "redeemed_month": seed.get("redeemed_month"),
            "orders_30d": seed.get("orders_30d"), "monthly_orders": seed.get("monthly_orders"),
            "price": seed.get("price"), "buyout_pct": seed.get("buyout_pct"),
        } if seed else None,
        "examples": examples,
        "seasonality": r.get("seasonality"),
        "category_seasonality": r.get("category_seasonality"),
        "notes": r.get("notes") or [],
    }
