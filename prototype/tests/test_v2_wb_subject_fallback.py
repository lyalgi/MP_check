from saol2.metrics import build_item_metrics_from_row
from saol2.pipeline import _wb_subject_fallback


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
