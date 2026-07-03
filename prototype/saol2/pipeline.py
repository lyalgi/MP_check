#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SAOL v2 — пайплайн оценки нового товара (методология docs/МЕТОДОЛОГИЯ_v2.md).

Набор аналогов:
  • --nms 111,222,333   — готовый список (для тестов / ручной выдачи);
  • --nm 152490541      — seed: берём похожие из MPStats similar (если отдаёт);
  • фото                — ВБ визуальный поиск (подключим отдельным шагом).

Запуск из папки prototype:
  python -m saol2.pipeline --nm 152490541 --price 200
  python -m saol2.pipeline --nms 152490541,143396280 --price 200
"""
from __future__ import annotations

import argparse
import os
import re
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

from saol2.metrics import (ItemMetrics, build_item_metrics_from_row, calendar_monthly,
                           fetch_item_metrics, series_trend)
from saol2.mpstats import MPStats, trend_window, year_window
from saol2.scoring import Settings, score, vote_category

_MONTHS_RU = ["янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]

# Категории, где «вид» товара — это ПРИНТ на плоском (его держит визуал siglip2), а не форма:
# для них ниша = визуальный поиск, НЕ MPStats identical (тот обобщает по крою: пантера→«оверсайз»).
# Старт с «Одежды»; расширять (постеры, наклейки, чехлы) по мере появления кейсов.
_PRINT_ROOTS = {"Одежда"}


def _is_print_category(subject_name: str | None) -> bool:
    """Корень категории (до « / ») попадает в принт-категории → нишу берём из визуала."""
    root = (subject_name or "").split(" / ")[0].strip()
    return root in _PRINT_ROOTS


# размер из текста (Уточнение / название похожего): длина→см, объём→мл. Длинные единицы — раньше.
_SIZE_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(мл|ml|см|cm|литр[а-я]*|л|l|м)(?![а-яёa-z])", re.IGNORECASE)


def _parse_size(text: str | None) -> tuple[float, str] | None:
    """Первый внятный размер: (значение, вид) — длина в см ('len') или объём в мл ('vol')."""
    if not text:
        return None
    for m in _SIZE_RE.finditer(text):
        val = float(m.group(1).replace(",", "."))
        unit = m.group(2).lower()
        if unit in ("см", "cm"):
            return val, "len"
        if unit == "м":
            return val * 100, "len"
        if unit in ("мл", "ml"):
            return val, "vol"
        return val * 1000, "vol"   # литр/л/l
    return None


_SIZE_UNIT = {"len": "см", "vol": "мл"}


def _apply_size(analogs: list[ItemMetrics], query: str | None, notes: list[str],
                s: Settings) -> tuple[list[ItemMetrics], dict | None]:
    """Фильтр ниши по размеру из «Уточнения» (если задан) + ВСЕГДА разброс размеров.
    Возвращает (ниша, разброс|None). Карточки без размера в названии не выбрасываем."""
    live = [a for a in analogs if a.ok and a.in_stock and a.orders_year > 0]
    sized = [(a, _parse_size(a.name)) for a in live]

    # разброс по доминирующему виду измерения (для показа «размеры 90–140 см»)
    spread = None
    pairs = [sz for _, sz in sized if sz]
    if pairs:
        kinds = [k for _, k in pairs]
        kind = max(set(kinds), key=kinds.count)
        vals = sorted(v for v, k in pairs if k == kind)
        if len(vals) >= 3:
            spread = {"lo": round(vals[0]), "hi": round(vals[-1]),
                      "unit": _SIZE_UNIT[kind], "n": len(vals)}

    target = _parse_size(query)
    if not target:
        return analogs, spread
    tv, tk = target
    lo, hi = tv * s.size_band_lo, tv * s.size_band_hi
    in_band = [a for a, sz in sized if sz and sz[1] == tk and lo <= sz[0] <= hi]
    if len(in_band) < 3:
        notes.append(f"размер ~{round(tv)} {_SIZE_UNIT[tk]} из «Уточнения»: близких мало — фильтр не применён")
        return analogs, spread
    # держим близкие по размеру + карточки без размера (их вид не определить — не штрафуем)
    keep = [a for a, sz in sized if not sz or (sz[1] == tk and lo <= sz[0] <= hi)]
    notes.append(f"фильтр по размеру ~{round(tv)} {_SIZE_UNIT[tk]} из «Уточнения»: "
                 f"{len(in_band)} близких из {len(live)}")
    if spread:
        spread["filtered_to"] = round(tv)
    return keep, spread


def _niche_seasonality(client: MPStats, analogs: list[ItemMetrics], subject_id: int | None,
                       cap: int = 40) -> tuple[dict | None, float | None]:
    """Сезонность + ТРЕНД ниши по ВСЕМУ живому пулу (не топ-N!). ВАЖНО: брать топ-N ПО ПРОДАЖАМ
    для тренда НЕЛЬЗЯ — это survivorship bias: топами становятся выросшие, и тренд систематически
    задирается вверх (медведь: топ-10 → ×3.1 «растёт», весь пул → ×0.76 «падает»). Полный пул —
    наименее смещённая оценка. Мелкая база уже отсекается в series_trend (past<5 → None). cap —
    предохранитель на огромные ниши (fallback-категория 200 карточек). Тянем by_period в параллель."""
    if not subject_id:
        return None, None
    # Для сезонности/тренда НЕ требуем наличие: история заказов за год валидна и без остатка.
    cat = sorted(
        [a for a in analogs if a.ok and a.orders_year > 0 and a.subject_id == subject_id],
        key=lambda a: a.orders_monthly_avg, reverse=True,
    )[:cap]
    if not cat:
        return None, None
    # тянем 455 дней: последние 365 — на сезонность, всё окно — на YoY-тренд
    d1, d2 = trend_window()
    season_cutoff = (date.fromisoformat(d2) - timedelta(days=365)).isoformat()
    months = [0.0] * 12
    trends: list[float] = []
    with ThreadPoolExecutor(max_workers=16) as pool:
        for rows in pool.map(lambda a: client.item_by_period(a.nm, d1, d2) or [], cat):
            season_rows = [r for r in rows if (r.get("data") or r.get("date") or "") >= season_cutoff]
            cm = calendar_monthly(season_rows)
            for i in range(12):
                months[i] += cm[i]
            t = series_trend(rows)
            if t is not None:
                trends.append(t)
    trend_ratio = round(statistics.median(trends), 3) if trends else None
    # сезонность ниши надёжна только на достаточной выборке; на 1–4 молодых карточках
    # это артефакт («все родились в мае») — не показываем
    season = _season_dict(months, len(cat)) if len(cat) >= 5 else None
    return season, trend_ratio


def _row_redeemed_month(r: dict) -> float:
    """Выкупы/мес строки категории: заказы(sales) × выкуп%(purchase) / 12."""
    orders = float(r.get("sales") or 0)
    buy = float(r.get("purchase") or 0)
    red = orders * (buy / 100.0) if buy > 0 else orders
    return red / 12.0


def _row_revenue_month(r: dict) -> float:
    """Выручка/мес строки категории по выкупам: выкупы/мес × цена."""
    return _row_redeemed_month(r) * float(r.get("final_price") or 0)


def _season_dict(months: list[float], based_on: int) -> dict | None:
    """Из 12 календарных сумм → словарь сезонности (пик, текущий месяц, фаза)."""
    import datetime
    if sum(months) <= 0:
        return None
    mx = max(months) or 1.0
    cur = datetime.date.today().month
    peak = max(range(12), key=lambda i: months[i])
    return {
        "monthly": [round(x, 1) for x in months],
        "peak_month": peak + 1, "peak_label": _MONTHS_RU[peak],
        "current_month": cur, "current_label": _MONTHS_RU[cur - 1],
        "current_ratio": round(months[cur - 1] / mx, 2),
        "based_on": based_on,
    }


def _seasonality_from_graphs(rows: list[dict]) -> dict | None:
    """Сезонность из массивов sales_graph (дневной ряд без дат). Привязываем КОНЕЦ
    графика к d2 окна — так корректно ложатся и короткие графики новых карточек."""
    from datetime import date, timedelta
    _, d2 = year_window()
    end = date.fromisoformat(d2)
    months = [0.0] * 12
    used = 0
    for r in rows:
        g = r.get("sales_graph") or []
        if not g:
            continue
        used += 1
        n = len(g)
        for i, v in enumerate(g):
            mon = (end - timedelta(days=(n - 1 - i))).month
            months[mon - 1] += float(v or 0)
    return _season_dict(months, used)

_VERDICT_RU = {
    "STRONG": "🟢🟢 РАСШИРЕННЫЙ ТЕСТ", "GREEN": "🟢 ТЕСТ", "YELLOW": "🟡 МАЛЫЙ ТЕСТ",
    "RED": "🔴 НЕ БРАТЬ", "UNKNOWN": "⚪ НЕТ ДАННЫХ",
}


def _D(d: dict) -> SimpleNamespace:
    return SimpleNamespace(**d)


def collect_analogs(client: MPStats, nms: list[int], limit: int) -> tuple[list[ItemMetrics], list[str]]:
    """Аналоги (визуально похожие артикулы) → ItemMetrics. Данные тянем из MPStats
    параллельно, без дневного графика (сезонность аналогам не нужна)."""
    notes: list[str] = []
    if not nms:
        return [], notes
    # дедуп с сохранением порядка релевантности
    seen: set[int] = set()
    ordered = [n for n in nms if not (n in seen or seen.add(n))]
    capped = ordered[:limit]
    if len(ordered) > limit:
        notes.append(f"похожих {len(ordered)} → беру {limit} ближайших по похожести")
    with ThreadPoolExecutor(max_workers=16) as pool:
        analogs = list(pool.map(lambda n: fetch_item_metrics(client, n, with_graph=False), capped))
    return analogs, notes


def _live_count(analogs: list[ItemMetrics]) -> int:
    return sum(1 for a in analogs if a.ok and a.in_stock and a.orders_year > 0)


def _top_live_nm(analogs: list[ItemMetrics]) -> int | None:
    """Топ-живой по заказам/год — надёжный якорь для similar (не дохлый nms[0])."""
    live = [a for a in analogs if a.ok and a.in_stock and a.orders_year > 0]
    return max(live, key=lambda a: a.orders_year).nm if live else None


def _identical_pool(client: MPStats, anchors: list[int], per_anchor: int = 30) -> list[ItemMetrics]:
    """ВИД через AI-`identical` MPStats, ПУЛ по нескольким якорям из фото-выдачи (идея
    Кристины): уникальные карточки именно ЭТОГО вида. identical по 1 якорю бывает жидким
    (когтеточка→2 живых), пул по топ-K живым добивает (→62), не теряя точности.
    Каталожный `similar` (бестселлеры сабджекта = «бревно/Стич») сюда НЕ подмешиваем."""
    seen: dict[int, ItemMetrics] = {}
    with ThreadPoolExecutor(max_workers=16) as pool:
        for rows in pool.map(
            lambda a: client.similar(int(a), limit=per_anchor, kind="identical") or [], anchors
        ):
            for r in rows:
                m = build_item_metrics_from_row(r)
                if m.ok and m.nm not in seen:
                    seen[m.nm] = m
    return list(seen.values())


def collect_niche(client: MPStats, seed_nm: int | None, nms: list[int], limit: int = 40,
                  min_visual: int = 5, vote_share: float = 0.5,
                  anchors_k: int = 5) -> tuple[list[ItemMetrics], list[str], str]:
    """ПРИОРИТЕТ «ВИД» через AI-`identical`, ПУЛ по нескольким якорям из фото-выдачи.
    Отвечает на «продаётся ли ИМЕННО ТАКОЙ товар» (медведь→медведи, не бревно/Стич).
    Якоря = seed (если есть, самый точный) + топ-живые визуальные похожие.
    Подстраховки (ОТДЕЛЬНО, НЕ в один пул): каталожный `similar` к топ-якорю (ТИП, грубее)
    → КАТЕГОРИЯ (subject_items, достраивается в analyze).

    Возвращает (ниша, заметки, scope), где scope — насколько оценка «про этот товар»:
    'vid' (узнан вид, узко/точно) | 'type' (вид НЕ распознан → широко по категории)."""
    va, notes = collect_analogs(client, nms or [], limit=limit)
    vlive = sorted([a for a in va if a.ok and a.in_stock and a.orders_year > 0],
                   key=lambda a: a.orders_year, reverse=True)

    # 0. РАЗВИЛКА ПО КАТЕГОРИИ: одежда/текстиль → «вид» = ПРИНТ, его держит ВИЗУАЛ (siglip2),
    #    а identical обобщает по крою (пантера→«оверсайз»). Для таких — ниша = сам визуал.
    vsid, vsname, _ = vote_category(vlive, vote_share)
    if vsid is not None and len(vlive) >= 3 and _is_print_category(vsname):
        notes.append(f"одежда/принт («{vsname}») → ниша из визуального поиска "
                     f"(принт важнее кроя): {len(vlive)} живых")
        return va, notes, "vid"

    # якоря для identical: seed первым (точнее всего), затем топ-живые визуальные
    anchors: list[int] = [int(seed_nm)] if seed_nm else []
    anchors += [a.nm for a in vlive[:anchors_k] if a.nm != (seed_nm or 0)]

    # 1. ВИД — пул AI-identical по якорям (главный сигнал, узко/точно)
    if anchors:
        pool = _identical_pool(client, anchors)
        plive = [a for a in pool if a.ok and a.in_stock and a.orders_year > 0]
        psid, psname, _ = vote_category(plive, vote_share)
        if len(plive) >= min_visual and psid is not None:
            notes.append(f"оценка по ВИДУ (AI-identical, пул по {len(anchors)} якорям): "
                         f"{len(plive)} живых, «{psname}»")
            return pool, notes, "vid"

    # 2. подстраховка — каталожный `similar` к топ-живому якорю (ТИП, ШИРОКО — вид не распознан)
    anchor = _top_live_nm(va) or (int(seed_nm) if seed_nm else None) or (nms[0] if nms else None)
    if anchor:
        rows = client.similar(int(anchor), limit=200, kind="similar")
        sim = [m for m in (build_item_metrics_from_row(r) for r in rows) if m.ok]
        if _live_count(sim) >= 3:
            notes.append(f"вид узнан слабо (живых якорей {len(anchors)}) → каталожные «похожие» "
                         f"к SKU {anchor}: {len(sim)} — ШИРОКО, не про этот товар")
            return sim, notes, "type"

    # 3. «КАТЕГОРИЯ»-резерв достраивается в analyze() из subject_items (200 карточек)
    return va, notes, "type"


def analyze(*, nms: list[int] | None = None, seed_nm: int | None = None,
            purchase_price: float = 0.0, settings: Settings | None = None, limit: int = 24,
            stores: int | None = None, query: str | None = None) -> dict:
    """Полный расчёт → словарь (для CLI и веб-интерфейса).

    nms     — артикулы похожих (из визуального поиска ВБ по фото/картинке товара);
    seed_nm — артикул самого товара (если известен) — показываем его спрос отдельно;
    stores  — число магазинов под категорию (если задано — партия считается «на сеть»).
    """
    s = settings or Settings()
    client = MPStats()
    analogs, notes, niche_scope = collect_niche(client, seed_nm, nms or [], limit=limit,
                                                min_visual=s.min_niche, vote_share=s.vote_share)
    if not analogs:
        return {"error": "Нет артикулов для оценки (визуальный поиск ничего не вернул)."}

    # ── популяция категории (один subject/items): денежный пол + сезонность + резервная ниша ──
    live = [a for a in analogs if a.ok and a.in_stock and a.orders_year > 0]
    sid, sname, _ = vote_category(live, s.vote_share)
    pop_rows = client.subject_items(sid, limit=200) if sid else []
    population_revenue = category_seasonality = None
    if pop_rows:
        population_revenue = [x for x in (_row_revenue_month(r) for r in pop_rows) if x > 0] or None
        category_seasonality = _seasonality_from_graphs(pop_rows)
        notes.append(f"срез категории «{sname}»: {len(pop_rows)} товаров (ден. пол + к-топам + сезонность)")
    elif sid:
        notes.append(f"срез категории «{sname}» (id={sid}) недоступен — пол/сезонность категории пропущены")

    # ── 3-й уровень: живых похожих всё ещё мало (1–2) → судим по КАТЕГОРИИ, а не по паре карточек ──
    if _live_count(analogs) < s.min_niche and pop_rows:
        cat_metrics = [m for m in (build_item_metrics_from_row(r) for r in pop_rows) if m.ok]
        if _live_count(cat_metrics) >= 3:
            notes.append(f"живых похожих мало ({_live_count(analogs)}) → оцениваю по категории "
                         f"«{sname}» ({_live_count(cat_metrics)} живых) — грубее, чем по виду")
            analogs = cat_metrics
            niche_scope = "type"

    # ── фильтр по размеру из «Уточнения» (+ всегда разброс размеров ниши) ──
    analogs, size_spread = _apply_size(analogs, query, notes, s)

    # категорию определяем по итоговой нише; тренд считаем по дневным рядам
    live = [a for a in analogs if a.ok and a.in_stock and a.orders_year > 0]
    sid, _, _ = vote_category(live, s.vote_share)
    seasonality, trend_ratio = _niche_seasonality(client, analogs, sid)

    v = score(analogs, purchase_price, settings=s, category_revenue=population_revenue,
              trend_ratio=trend_ratio, stores=stores)

    seed = None
    if seed_nm:
        sm = fetch_item_metrics(client, seed_nm)  # с графиком — для сезонности «твоего товара»
        if sm.ok:
            seed = {
                "nm": sm.nm, "name": sm.name, "image": sm.image_thumb, "price": sm.price,
                "orders_month": sm.orders_monthly_avg, "redeemed_month": sm.redeemed_monthly_avg,
                "orders_30d": round(sm.orders_30d),  # темп за 30 дней (заказы) — ловит свежий взлёт
                "buyout_pct": sm.buyout_pct, "in_stock": sm.in_stock,
                "monthly_orders": sm.monthly_orders, "season_index": sm.season_index,
            }

    result = {
        "verdict": v.verdict, "verdict_label": _VERDICT_RU.get(v.verdict, v.verdict),
        "provisional": v.provisional,
        "niche_scope": niche_scope,   # 'vid' (узнан вид) | 'type' (вид не распознан → широко)
        "score_100": v.score_100,
        "test_units": v.test_units, "test_units_per_store": v.test_units_per_store,
        "stores": v.stores,
        # спрос — абсолют в деньгах
        "redeemed_month_median": v.redeemed_month_median, "orders_month_median": v.orders_month_median,
        "lead_redeemed_month": v.lead_redeemed_month, "lead_orders_month": v.lead_orders_month,
        "lead_revenue_month": v.lead_revenue_month,
        "abs_demand": v.abs_demand, "ratio_to_top": v.ratio_to_top, "category_pct": v.category_pct,
        "demand_score": v.demand_score, "target_revenue": v.target_revenue,
        # маржа
        "market_price_median": v.market_price_median, "markup": v.markup,
        "margin_pct": v.margin_pct, "margin_gate": v.margin_gate,
        "buyout_pct_median": v.buyout_pct_median,
        # тренд + экономика
        "trend_ratio": v.trend_ratio, "trend_label": v.trend_label, "trend_coef": v.trend_coef,
        "niche_revenue_month": v.niche_revenue_month, "test_capital": v.test_capital,
        "potential_margin": v.potential_margin, "crit_fractile": v.crit_fractile,
        "price_segment": v.price_segment, "size_spread": size_spread,
        # категория
        "subject_name": v.subject_name, "subject_id": v.subject_id, "vote_share": v.vote_share,
        "analog_count": v.analog_count, "purchase_price": purchase_price,
        "reasons": v.reasons, "notes": notes, "examples": v.examples, "seed": seed,
        "seasonality": seasonality, "category_seasonality": category_seasonality,
    }
    _log_eval(seed_nm, nms, purchase_price, stores, result)
    return result


def _log_eval(seed_nm, nms, purchase_price, stores, r: dict) -> None:
    """Лог оценок в JSONL для будущей калибровки полос на исходах (best-effort).
    Поле `outcome` заполняется позже вручную/по факту распродажи теста."""
    import json
    from datetime import datetime, timezone
    path = Path(os.environ.get("SAOL2_EVAL_LOG", Path(__file__).resolve().parent / "eval_log.jsonl"))
    rec = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "seed_nm": seed_nm, "nms_n": len(nms or []), "purchase_price": purchase_price,
        "stores": stores, "subject_id": r.get("subject_id"), "subject_name": r.get("subject_name"),
        "verdict": r.get("verdict"), "score_100": r.get("score_100"),
        "abs_demand": r.get("abs_demand"), "ratio_to_top": r.get("ratio_to_top"),
        "category_pct": r.get("category_pct"), "markup": r.get("markup"),
        "trend_ratio": r.get("trend_ratio"), "niche_revenue_month": r.get("niche_revenue_month"),
        "redeemed_month_median": r.get("redeemed_month_median"),
        "test_units_per_store": r.get("test_units_per_store"),
        "outcome": None,   # ← вписать факт: sold_out / slow / dead (для калибровки)
    }
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def evaluate(*, nms: list[int] | None = None, seed_nm: int | None = None,
             purchase_price: float = 0.0, settings: Settings | None = None, limit: int = 40) -> None:
    r = analyze(nms=nms, seed_nm=seed_nm, purchase_price=purchase_price, settings=settings, limit=limit)
    if r.get("error"):
        print(r["error"]); return
    for n in r["notes"]:
        print(f"  · {n}")
    v = _D(r)
    if r.get("seed"):
        sd = r["seed"]
        print(f"\n  Твой товар: {sd['name'][:50]} — ~{sd['redeemed_month']:.0f} выкупов/мес")
    prov = "  (предварительно — полосы не калиброваны на исходах)" if r.get("provisional") else ""
    print(f"\n  {v.verdict_label}{prov}")
    if v.score_100 is not None:
        tr = f" · тренд {v.trend_label}" if v.trend_label else ""
        print(f"  Общий балл: {v.score_100}/100  "
              f"(скорость {v.demand_score:.2f} × шлюз-маржи {v.margin_gate:.2f} × тренд {v.trend_coef:.2f}){tr}")
    if v.subject_name:
        print(f"  Категория: {v.subject_name} (id={v.subject_id}, голоса {v.vote_share:.0%})")
    if v.niche_revenue_month is not None:
        print(f"  Выручка похожих (типичная карточка): ~{v.niche_revenue_month:.0f} ₽/мес  "
              f"(абс. {v.abs_demand:.2f} от пола {v.target_revenue:.0f})")
    if v.category_pct is not None:
        print(f"  Место в категории: обгоняет {v.category_pct:.0f} из 100 ({100 - v.category_pct:.0f} сильнее)")
    if v.redeemed_month_median:
        print(f"  В штуках: медиана {v.redeemed_month_median:.0f} выкупов/мес (заказов {v.orders_month_median:.0f})")
    if v.market_price_median:
        print(f"  Цена рынка (медиана ВБ): {v.market_price_median:.0f}₽; "
              f"наценка к закупу {v.markup}× — запас (шлюз {v.margin_gate})")
    seg = r.get("price_segment")
    if seg:
        print(f"  Цена похожих (где объём): {seg['low']}–{seg['high']}₽ ({seg['share']}% выкупов похожих)")
    if v.buyout_pct_median is not None:
        print(f"  Выкуп (медиана категории): {v.buyout_pct_median:.0f}%")
    if v.test_units_per_store is not None:
        line = f"  >>> ТЕСТОВАЯ ПАРТИЯ: {v.test_units_per_store} шт/точку"
        if v.stores:
            line += f"  →  {v.test_units} шт на сеть ({v.stores} точек)"
        print(line)
        if v.test_capital is not None:
            extra = f" · потенц. маржа ~{v.potential_margin:.0f}₽" if v.potential_margin is not None else ""
            print(f"      капитал в тесте ~{v.test_capital:.0f}₽{extra}  (CF {v.crit_fractile})")
    print(f"  Коды: {', '.join(v.reasons) or '—'}")
    for e in v.examples:
        print(f"    · {e['nm']}  {e['redeemed_month']:.0f} вык/мес ({e['orders_month']:.0f} зак)  "
              f"{e['price']:.0f}₽  выкуп {e['buyout_pct']}%  {e['name']}")
    print()


def main() -> int:
    try:  # консоль Windows по умолчанию cp1251 — даём UTF-8, иначе падаем на «→»/эмодзи
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description="SAOL v2 — оценка нового товара (MPStats)")
    ap.add_argument("--nm", type=int, help="артикул WB — берём его картинку и ищем визуально похожих")
    ap.add_argument("--nms", type=str, help="список артикулов через запятую (готовая выдача)")
    ap.add_argument("--price", type=float, required=True, help="закупочная цена, ₽")
    ap.add_argument("--target", type=float, help="порог заказов/мес (по умолч. 300)")
    args = ap.parse_args()

    settings = Settings()
    if args.target:
        settings.target_orders_month = args.target

    seed_nm = None
    if args.nms:
        nms = [int(x) for x in args.nms.split(",") if x.strip()]
    elif args.nm:
        from saol2 import visual
        seed_nm = args.nm
        nms = visual.search_similar_by_nm(MPStats(), args.nm)
        print(f"  · визуальный поиск ВБ по картинке nm={args.nm}: {len(nms)} похожих")
    else:
        ap.error("нужен --nm или --nms")
    evaluate(nms=nms, seed_nm=seed_nm, purchase_price=args.price, settings=settings)
    return 0


if __name__ == "__main__":
    sys.exit(main())
