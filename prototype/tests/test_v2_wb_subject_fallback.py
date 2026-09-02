from saol2.metrics import build_item_metrics_from_row
from saol2.metrics import ItemMetrics
from saol2.pipeline import _item_in_category, _mpstats_category, _top_live_sample, _wb_subject_fallback


def test_uses_wb_subject_when_new_visual_cards_have_no_mpstats_history(monkeypatch):
    def fake_cards(_nms):
        return [{"subjectId": 788}, {"subjectId": 788}, {"subjectId": 788}, {"subjectId": 12}]

    import saol_core
    monkeypatch.setattr(saol_core, "fetch_cards", fake_cards)

    assert _wb_subject_fallback([1, 2, 3, 4]) == (788, 3, 4)


def test_subject_row_counts_fbs_stock_as_available():
    item = build_item_metrics_from_row({"id": 1, "balance": 0, "balance_fbs": 7, "sales": 10})

    assert item.balance == 7
    assert item.in_stock is True


def test_mpstats_category_uses_its_own_path_for_the_exact_wb_name():
    class Catalog:
        def category_list(self):
            return [
                {"name": "Счетный материал", "path": "mpstats-counter-material"},
                {"name": "Калькуляторы", "path": "mpstats-calculators"},
            ]

    assert _mpstats_category(Catalog(), "Калькуляторы") == {
        "name": "Калькуляторы", "path": "mpstats-calculators"
    }


def test_exact_category_check_rejects_a_neighbouring_subject():
    calculator = ItemMetrics(nm=1, subject_name="Калькуляторы")
    counters = ItemMetrics(nm=2, subject_name="Канцелярия / Счетный материал")

    assert _item_in_category(calculator, "Калькуляторы") is True
    assert _item_in_category(counters, "Калькуляторы") is False


def test_category_fallback_uses_the_same_top_40_for_score_and_chart():
    rows = [
        ItemMetrics(nm=i, ok=True, in_stock=True, orders_year=i * 12,
                    orders_monthly_avg=float(i))
        for i in range(1, 51)
    ]

    sample = _top_live_sample(rows)

    assert len(sample) == 40
    assert [item.nm for item in sample[:5]] == [50, 49, 48, 47, 46]
    assert sample[-1].nm == 11
