"""E2E пайплайна по шагам ТЗ 1-6, на моках."""
from __future__ import annotations

import asyncio

from app.models import (
    CategoryCoefficient,
    MarketSnapshot,
    RetailHistoryItem,
    TaxonomyItem,
    WbOzonMapping,
)
from app.services import category_extractor as ce
from app.services import engine as engine_mod
from app.services.engine import LookupInput
from app.services.providers.mock import MockProvider
from app.services.reason_codes import ReasonCode
from app.services.wb_visual_search import MockVisualSearchProvider


def _raw_card(nm_id, name, feedbacks, subject_id=145, subject_name="Трусы детские",
              parent_name="Детям", price_rub=1000.0):
    return {
        "id": nm_id,
        "name": name,
        "brand": "TestBrand",
        "feedbacks": feedbacks,
        "reviewRating": 4.7,
        "subjectId": subject_id,
        "subjectName": subject_name,
        "subjectParentId": 100,
        "subjectParent": parent_name,
        "priceU": int(price_rub * 100),
        "salePriceU": int(price_rub * 0.9 * 100),
        "totalQuantity": feedbacks * 2,
    }


def _patch_pipeline(monkeypatch, nm_ids, raw_cards, top_sales=None):
    monkeypatch.setattr(engine_mod, "get_visual_search_provider",
                        lambda: MockVisualSearchProvider(nm_ids))

    async def _fetch(nm_list):
        return [c for c in raw_cards if int(c["id"]) in set(int(x) for x in nm_list)]

    monkeypatch.setattr(engine_mod.ce, "fetch_cards_raw", _fetch)
    mp = MockProvider(top_sales=top_sales or [100, 100, 100])
    monkeypatch.setattr(engine_mod, "get_analytics_provider", lambda: mp)

    async def _top_subj(self, subject_id, limit=30):
        return await self.top_n_in_category("any", limit)
    MockProvider.top_n_by_subject = _top_subj  # type: ignore[attr-defined]


def test_green_full_pipeline(db, monkeypatch):
    db.add(WbOzonMapping(wb_path="Детям/Трусы детские", ozon_path="Одежда/Белье"))
    db.commit()
    # Все аналоги в одной subject_id=145, поэтому weighted vote сходится
    raw = [
        _raw_card(350000101, "A", 200),
        _raw_card(350000102, "B", 150),
        _raw_card(350000103, "C", 50),
        _raw_card(350000104, "D", 9),     # отсекается по min_feedbacks
        _raw_card(350000105, "E", 1500),
    ]
    _patch_pipeline(
        monkeypatch,
        [350000101, 350000102, 350000103, 350000104, 350000105],
        raw,
        top_sales=[15] * 10,   # ≥10 лидеров (иначе LOW_SAMPLE), Σ=150 как раньше
    )

    eng = engine_mod.LiquidityEngine()
    r = asyncio.run(eng.evaluate(db, LookupInput(image_bytes=b"x", purchase_price=200.0)))

    assert r.visual_search_count == 5
    assert r.filtered_analog_count == 4
    assert r.top_seed_nm_id == 350000105       # max-feedbacks внутри dominant subject
    assert r.wb_subject_name == "Трусы детские"
    assert r.wb_parent_name == "Детям"
    assert r.subject_vote_share == 1.0   # все из одной subject
    assert r.verdict in ("GREEN", "STRONG")
    assert r.decision_confidence == "MEDIUM"
    assert ReasonCode.SNAPSHOT_COLD_START.value in r.verdict_reasons
    assert db.query(MarketSnapshot).count() > 0


def test_heterogeneous_subjects_flagged(db, monkeypatch):
    # 50/50 split между двумя subject — никто не побеждает с порогом 60%
    raw = [
        _raw_card(201, "A", 100, subject_id=145, subject_name="Трусы"),
        _raw_card(202, "B", 100, subject_id=999, subject_name="Чашки", parent_name="Дом"),
    ]
    _patch_pipeline(monkeypatch, [201, 202], raw)
    eng = engine_mod.LiquidityEngine()
    r = asyncio.run(eng.evaluate(db, LookupInput(image_bytes=b"x", purchase_price=100.0)))
    assert r.verdict == "UNKNOWN"
    assert r.decision_confidence == "LOW"
    assert ReasonCode.HETEROGENEOUS_SUBJECTS.value in r.verdict_reasons
    assert ReasonCode.CATEGORY_UNRESOLVED.value in r.verdict_reasons
    assert r.subject_vote_share == 0.5


def test_filter_removes_low_feedback(db, monkeypatch):
    raw = [_raw_card(301, "A", 5), _raw_card(302, "B", 7)]
    _patch_pipeline(monkeypatch, [301, 302], raw)
    eng = engine_mod.LiquidityEngine()
    r = asyncio.run(eng.evaluate(db, LookupInput(image_bytes=b"x", purchase_price=100.0)))
    assert r.filtered_analog_count == 0
    assert r.verdict == "UNKNOWN"
    assert ReasonCode.LOW_FEEDBACKS.value in r.verdict_reasons


def test_wb_ozon_mapping_resolved(db, monkeypatch):
    db.add(WbOzonMapping(wb_path="Детям/Трусы детские", ozon_path="Одежда/Белье"))
    db.commit()
    raw = [_raw_card(401, "X", 100)]
    _patch_pipeline(monkeypatch, [401], raw)
    eng = engine_mod.LiquidityEngine()
    r = asyncio.run(eng.evaluate(db, LookupInput(image_bytes=b"x", purchase_price=100.0)))
    assert r.ozon_category == "Одежда/Белье"
    assert r.mapping_kind == "exact"
    assert ReasonCode.WB_ONLY.value not in r.verdict_reasons


def test_wb_only_flag_when_no_ozon(db, monkeypatch):
    raw = [_raw_card(350000501, "X", 1500)]
    _patch_pipeline(monkeypatch, [350000501], raw, top_sales=[1])
    eng = engine_mod.LiquidityEngine()
    r = asyncio.run(eng.evaluate(db, LookupInput(image_bytes=b"x", purchase_price=100.0)))
    assert r.ozon_category is None
    assert r.verdict == "YELLOW"
    assert r.decision_confidence == "MEDIUM"
    assert ReasonCode.WB_ONLY.value in r.verdict_reasons
    assert ReasonCode.VERDICT_CAPPED.value in r.verdict_reasons


def test_wb_only_does_not_cap_green(db, monkeypatch):
    """WB_ONLY (нет Ozon-маппинга) НЕ режет GREEN при здоровой выборке — WB-first."""
    raw = [
        _raw_card(350000601, "A", 200),
        _raw_card(350000602, "B", 150),
        _raw_card(350000603, "C", 50),
        _raw_card(350000605, "E", 1500),
    ]
    _patch_pipeline(monkeypatch, [350000601, 350000602, 350000603, 350000605], raw, top_sales=[15] * 10)
    eng = engine_mod.LiquidityEngine()
    r = asyncio.run(eng.evaluate(db, LookupInput(image_bytes=b"x", purchase_price=200.0)))
    assert ReasonCode.WB_ONLY.value in r.verdict_reasons       # Ozon нет
    assert r.verdict in ("GREEN", "STRONG")                     # GREEN/STRONG достижим
    assert ReasonCode.VERDICT_CAPPED.value not in r.verdict_reasons


def test_low_sample_caps_green(db, monkeypatch):
    """Тонкий benchmark (<10 лидеров) не даёт уверенный GREEN — защита от bias по выборке."""
    db.add(WbOzonMapping(wb_path="Детям/Трусы детские", ozon_path="Одежда/Белье"))
    db.commit()
    raw = [
        _raw_card(350000701, "A", 200),
        _raw_card(350000702, "B", 150),
        _raw_card(350000703, "C", 1500),
    ]
    _patch_pipeline(monkeypatch, [350000701, 350000702, 350000703], raw, top_sales=[50, 50, 50])
    eng = engine_mod.LiquidityEngine()
    r = asyncio.run(eng.evaluate(db, LookupInput(image_bytes=b"x", purchase_price=200.0)))
    assert ReasonCode.LOW_SAMPLE.value in r.verdict_reasons    # top_count=3 < 10
    assert r.verdict != "GREEN"


def test_heuristic_benchmark_caps_green(db, monkeypatch):
    raw = [_raw_card(350000551, "X", 1500)]
    _patch_pipeline(monkeypatch, [350000551], raw)

    class EmptyTopProvider:
        async def top_n_in_category(self, wb_category_path, limit=30):
            return []

    monkeypatch.setattr(engine_mod, "get_analytics_provider", lambda: EmptyTopProvider())
    eng = engine_mod.LiquidityEngine()
    r = asyncio.run(eng.evaluate(db, LookupInput(image_bytes=b"x", purchase_price=100.0)))
    assert r.top_is_heuristic is True
    assert r.verdict == "YELLOW"
    assert r.decision_confidence == "MEDIUM"
    assert ReasonCode.HEURISTIC_BENCHMARK.value in r.verdict_reasons
    assert ReasonCode.VERDICT_CAPPED.value in r.verdict_reasons


def test_retail_history_risk_caps_green(db, monkeypatch):
    db.add(TaxonomyItem(
        group="Детям",
        subgroup="Белье",
        vid="Трусы детские",
        sold_qty=0,
        stock_qty=0,
        sold_rub=0,
        stock_rub=0,
        cost_sold=0,
        cost_stock=0,
        wb_paths=["Детям/Трусы детские"],
        ozon_paths=[],
        source_file="taxonomy.xlsx",
    ))
    db.add(RetailHistoryItem(
        group="Детям",
        subgroup="Белье",
        vid="Трусы детские",
        price_band="0 - 999",
        product_name="ТРУСЫ",
        price=199,
        markup=1.1,
        profitability_current=-2,
        profitability_prev=20,
        sales_current=5,
        sales_prev=80,
        stock_current=100,
        source_file="registry.xlsx",
        source_sheet="Лист1",
    ))
    db.commit()
    raw = [_raw_card(350000701, "X", 1500)]
    _patch_pipeline(monkeypatch, [350000701], raw, top_sales=[50, 50, 50])

    eng = engine_mod.LiquidityEngine()
    r = asyncio.run(eng.evaluate(db, LookupInput(image_bytes=b"x", purchase_price=100.0)))

    assert r.verdict == "YELLOW"
    assert ReasonCode.OFFLINE_OVERSTOCK.value in r.verdict_reasons
    assert ReasonCode.VERDICT_CAPPED.value in r.verdict_reasons


def test_seed_url_fallback(db, monkeypatch):
    raw = [_raw_card(5012345, "Z", 1000)]
    _patch_pipeline(monkeypatch, [5012345], raw)
    eng = engine_mod.LiquidityEngine()
    r = asyncio.run(eng.evaluate(
        db,
        LookupInput(
            image_bytes=None,
            purchase_price=100.0,
            seed_url="https://www.wildberries.ru/catalog/5012345/detail.aspx",
        ),
    ))
    assert r.visual_search_count >= 1
    assert r.top_seed_nm_id == 5012345


def test_no_visual_returns_no_visual_reason(db, monkeypatch):
    _patch_pipeline(monkeypatch, [], [])
    eng = engine_mod.LiquidityEngine()
    r = asyncio.run(eng.evaluate(db, LookupInput(image_bytes=b"x", purchase_price=100.0)))
    assert r.verdict == "UNKNOWN"
    assert ReasonCode.NO_VISUAL.value in r.verdict_reasons


def test_pipeline_steps_recorded(db, monkeypatch):
    raw = [_raw_card(601, "A", 100)]
    _patch_pipeline(monkeypatch, [601], raw)
    eng = engine_mod.LiquidityEngine()
    r = asyncio.run(eng.evaluate(db, LookupInput(image_bytes=b"x", purchase_price=100.0)))
    names = [s.name for s in r.pipeline]
    assert "visual_search" in names
    assert "filter_feedbacks" in names
    assert "aggregate_demand" in names
    assert "category_extract" in names
    assert "map_wb_to_ozon" in names
    assert "benchmark_top30" in names
    assert "market_snapshots" in names
    assert "rating" in names


def test_no_coefficient_never_uses_raw_retail_fact(db, monkeypatch):
    """Регрессия «лампа Xiaomi → 2350 шт».

    При наличии истории Разноторга рекомендация к закупу не должна становиться
    сырым годовым объёмом продаж всей категории (раньше выдавал 2350 = продажи
    всех ламп за год). Новая модель для новинок даёт только тест-партию по
    вердикту; RED = 0.
    """
    db.add(TaxonomyItem(
        group="Электроника",
        subgroup="Освещение",
        vid="Лампа настольная",
        sold_qty=0, stock_qty=0, sold_rub=0, stock_rub=0,
        cost_sold=0, cost_stock=0,
        wb_paths=["Электроника/Лампа настольная"],
        ozon_paths=[],
        source_file="taxonomy.xlsx",
    ))
    db.add(RetailHistoryItem(
        group="Электроника",
        subgroup="Освещение",
        vid="Лампа настольная",
        product_name="ЛАМПА XIAOMI",
        price=1500, markup=1.3,
        profitability_current=20, profitability_prev=18,
        sales_current=2350,        # годовой объём по ВСЕЙ категории ламп
        sales_prev=2000,
        stock_current=100,
        source_file="registry.xlsx",
    ))
    db.commit()
    raw = [_raw_card(350000801, "Lamp", 1500,
                     subject_id=145, subject_name="Лампа настольная",
                     parent_name="Электроника")]
    _patch_pipeline(monkeypatch, [350000801], raw, top_sales=[50, 50, 50])

    eng = engine_mod.LiquidityEngine()
    r = asyncio.run(eng.evaluate(db, LookupInput(image_bytes=b"x", purchase_price=4000.0)))

    assert r.recommended_units_year == 0         # RED → не брать
    assert r.recommended_units_year != 2350      # и точно не сырой факт категории
    assert r.forecast_source == "test_quantity"
    assert r.baseline_units_per_position is None
    assert r.wb_strength is None


def test_test_quantity_ignores_baseline_wb_strength_for_new_goods(db, monkeypatch):
    """Закуп для новинки = тестовая партия, а не baseline Разноторга × WB-сила."""
    db.add(WbOzonMapping(wb_path="Детям/Трусы детские", ozon_path="Одежда/Белье"))
    db.add(CategoryCoefficient(
        wb_path="Детям/Трусы детские",
        raznotorg_revenue_year=2_000_000.0,
        marketplace_revenue_year=100_000_000.0,
        coefficient=0.02,
        raznotorg_units_year=1000.0,   # baseline = 1000 / 10 = 100 шт/позиция
        raznotorg_positions=10,
    ))
    db.commit()
    raw = [
        _raw_card(350000901, "A", 200),
        _raw_card(350000902, "B", 150),
        _raw_card(350000903, "C", 50),
        _raw_card(350000905, "E", 1500),
    ]
    _patch_pipeline(
        monkeypatch,
        [350000901, 350000902, 350000903, 350000905],
        raw,
        top_sales=[15] * 10,   # ≥10 лидеров (иначе LOW_SAMPLE), Σ=150 как раньше
    )

    eng = engine_mod.LiquidityEngine()
    r = asyncio.run(eng.evaluate(db, LookupInput(image_bytes=b"x", purchase_price=200.0)))

    assert r.verdict in ("GREEN", "STRONG")
    assert r.forecast_source == "test_quantity"
    assert r.baseline_units_per_position is None
    assert r.wb_strength is None
    assert r.market_share_coefficient == 0.02       # справочно осталось в ответе
    assert r.raznotorg_units_year == 1000.0         # справочно осталось в ответе
    # STRONG/GREEN → расширенный/обычный тест по цене входа, не полный объём категории
    assert r.recommended_units_year == eng._test_quantity(200.0, r.verdict)
    assert r.recommended_units_year != 1000
    assert ReasonCode.FORECAST_OK.value in r.verdict_reasons


def test_device_nm_ids_skip_visual_search(db, monkeypatch):
    """nm_id с телефона закупщика → серверный visual search пропускается."""
    raw = [_raw_card(350000901, "A", 200), _raw_card(350000902, "B", 150),
           _raw_card(350000903, "C", 1500)]
    _patch_pipeline(monkeypatch, [350000901, 350000902, 350000903], raw, top_sales=[15] * 10)

    class Boom:
        async def search_by_image(self, b):
            raise AssertionError("серверный visual search не должен вызываться")
        async def search_by_seed_nm(self, n):
            raise AssertionError("seed search не должен вызываться")
    monkeypatch.setattr(engine_mod, "get_visual_search_provider", lambda: Boom())

    eng = engine_mod.LiquidityEngine()
    r = asyncio.run(eng.evaluate(db, LookupInput(
        image_bytes=None, purchase_price=200.0,
        seed_nm_ids=[350000901, 350000902, 350000903])))
    assert r.visual_search_count == 3
    assert any(s.name == "visual_search_device" for s in r.pipeline)


def test_query_tokens_and_match():
    from app.services.engine import _card_matches_query, _query_tokens
    assert _query_tokens("500 мл синий") == ["500", "мл", "синий"]
    assert _query_tokens("2 шт") == ["2"]            # «шт» — стоп-слово
    toks = _query_tokens("500 мл")
    assert _card_matches_query({"name": "Бутылка 500 мл синяя"}, toks) is True
    assert _card_matches_query({"name": "Бутылка 1 л"}, toks) is False
    assert _card_matches_query({"name": "что угодно"}, []) is False


def test_query_refines_analogs(db, monkeypatch):
    """Текст-уточнение сужает визуальную выдачу до подходящих карточек."""
    raw = [
        _raw_card(350000801, "Бутылка 500 мл", 100),
        _raw_card(350000802, "Бутылка 500 мл синяя", 100),
        _raw_card(350000803, "Бутылка 500 мл красная", 100),
        _raw_card(350000804, "Бутылка 1 литр", 100),
        _raw_card(350000805, "Термос 1 л", 100),
    ]
    _patch_pipeline(monkeypatch, [350000801, 350000802, 350000803, 350000804, 350000805],
                    raw, top_sales=[15] * 10)
    eng = engine_mod.LiquidityEngine()
    r = asyncio.run(eng.evaluate(db, LookupInput(
        image_bytes=b"x", purchase_price=200.0, query="500 мл")))
    assert r.filtered_analog_count == 3          # остались только «500 мл»
    assert any(s.name == "refine_query" for s in r.pipeline)


def test_wb_analog_includes_image_url():
    from app.services.providers.wb_public import _product_to_analog_safe, _wb_image_url

    assert _wb_image_url(350000101) == (
        "https://basket-21.wbbasket.ru/vol3500/part350000/350000101/images/c516x688/1.webp"
    )

    analog = _product_to_analog_safe(_raw_card(350000101, "A", 200))
    assert analog is not None
    assert analog.image == _wb_image_url(350000101)
    assert analog.model_dump()["image"].endswith("/images/c516x688/1.webp")


def test_action_units_follow_verdict():
    eng = engine_mod.LiquidityEngine()

    assert eng._action_units_for_verdict("GREEN", 1301) == 1301
    assert eng._action_units_for_verdict("YELLOW", 1301) == 50
    assert eng._action_units_for_verdict("YELLOW", 12) == 1
    assert eng._action_units_for_verdict("RED", 1301) == 0
    assert eng._action_units_for_verdict("UNKNOWN", 1301) is None
