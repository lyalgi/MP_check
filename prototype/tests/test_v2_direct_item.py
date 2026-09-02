from saol2.metrics import ItemMetrics
from saol2.scoring import score


def test_linked_card_is_scored_as_direct_evidence_not_low_sample():
    """A known WB card is a direct measurement, not a one-item analogue sample."""
    item = ItemMetrics(
        nm=1,
        name="Known product",
        subject_id=10,
        subject_name="Calculators",
        price=500,
        in_stock=True,
        orders_year=3600,
        orders_monthly_avg=300,
        redeemed_monthly_avg=270,
        buyout_pct=90,
        ok=True,
    )

    verdict = score([item], 250, category_revenue=[50_000, 75_000, 100_000], direct_item=True)

    assert verdict.analog_count == 1
    assert "LOW_SAMPLE" not in verdict.reasons
    assert verdict.orders_month_median == 300
    assert verdict.market_price_median == 500
    assert verdict.verdict in {"GREEN", "STRONG"}
