from saol2.pipeline import _wb_subject_fallback


def test_uses_wb_subject_when_new_visual_cards_have_no_mpstats_history(monkeypatch):
    def fake_cards(_nms):
        return [{"subjectId": 788}, {"subjectId": 788}, {"subjectId": 788}, {"subjectId": 12}]

    import saol_core
    monkeypatch.setattr(saol_core, "fetch_cards", fake_cards)

    assert _wb_subject_fallback([1, 2, 3, 4]) == (788, 3, 4)
