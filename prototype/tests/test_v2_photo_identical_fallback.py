from saol2.metrics import ItemMetrics
from saol2 import pipeline


def _live(nm: int) -> ItemMetrics:
    return ItemMetrics(nm=nm, subject_id=1, subject_name="Mice", price=500,
                       in_stock=True, orders_year=120, orders_monthly_avg=10, ok=True)


def test_new_photo_cards_try_identical_from_only_five_nearest(monkeypatch):
    seen = []
    new_card = ItemMetrics(nm=1, ok=True, in_stock=True, orders_year=0)
    monkeypatch.setattr(pipeline, "collect_analogs", lambda *_args, **_kwargs: ([new_card], []))

    def identical(_client, anchors):
        seen.extend(anchors)
        return [_live(100), _live(101), _live(102), _live(103), _live(104)]

    monkeypatch.setattr(pipeline, "_identical_pool", identical)

    pool, _notes, scope = pipeline.collect_niche(object(), None, [11, 12, 13, 14, 15, 16], anchors_k=5)

    assert seen == [11, 12, 13, 14, 15]
    assert scope == "vid"
    assert len(pool) == 5
