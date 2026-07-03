from app.schemas import AnalogSku
from app.services.liquidity import build_advice, compute_score
from app.services.reason_codes import ReasonCode


def _mk(nm, sales, price=1000.0):
    return AnalogSku(
        nm_id=nm, name=f"sku-{nm}", brand="B", price=price, sale_price=price * 0.9,
        feedbacks=int(sales), rating=4.5, stocks=int(sales) * 3,
        sales_30d_est=float(sales),
        url=f"https://example/{nm}",
    )


def test_green_when_both_scores_high():
    # Аналоги по 200/мес каждый, топ по 100/мес каждый.
    analogs = [_mk(1, 200), _mk(2, 200), _mk(3, 200)]
    top = [_mk(10, 100), _mk(11, 100), _mk(12, 100)]
    s = compute_score(analogs, top)
    # sku_demand = median(200)/median(100) = 2.0
    # niche_volume = 600/300 = 2.0
    # rating = harmonic mean = 2.0
    assert s.sku_demand_score == 2.0
    assert s.niche_volume_score == 2.0
    assert s.verdict in ("GREEN", "STRONG")   # очень сильный спрос → STRONG
    assert ReasonCode.HIGH_SKU_DEMAND.value in s.reasons
    assert ReasonCode.HIGH_NICHE_VOLUME.value in s.reasons


def test_red_when_both_scores_low():
    # И SKU-demand низкий, и ниша мала по сравнению с топом
    analogs = [_mk(1, 10), _mk(2, 10)]
    top = [_mk(10, 1000), _mk(11, 1000), _mk(12, 1000)]
    s = compute_score(analogs, top)
    # sku_demand = 10/1000 = 0.01, niche = 20/3000 = 0.007
    assert s.sku_demand_score < 0.3
    assert s.niche_volume_score < 0.3
    assert s.verdict == "RED"
    assert ReasonCode.LOW_SKU_DEMAND.value in s.reasons


def test_yellow_one_score_high_other_low():
    # SKU-demand высокий относительно лидеров, ниша мала, и АБСОЛЮТНО спрос
    # невелик (80/мес < target 400) — поэтому YELLOW, не GREEN.
    analogs = [_mk(1, 80)]
    top = [_mk(10, 100), _mk(11, 100), _mk(12, 100)]
    s = compute_score(analogs, top)
    # sku_demand = 80/100 = 0.8 > green; niche = 80/300 = 0.27; abs = 80/400 = 0.2
    assert s.verdict == "YELLOW"


def test_absolute_demand_lifts_red_to_yellow_not_green():
    # Товар относительно лидеров слабый (0.25), но продаёт 500/мес → НЕ RED.
    # Абсолютный спрос вытягивает в YELLOW (на тест), но НЕ в GREEN: GREEN —
    # только хиты уровня лидеров (ищем хиты, не раздаём высокие баллы).
    analogs = [_mk(1, 500)]
    top = [_mk(10, 2000), _mk(11, 2000), _mk(12, 2000)]
    s = compute_score(analogs, top)
    assert s.sku_demand_score < 0.3      # относительно лидеров — слабо
    assert s.verdict == "YELLOW"         # не RED (абсолют вытянул), но и не GREEN


def test_green_only_for_niche_leaders():
    # GREEN только когда товар реально на уровне лидеров (хит), а не просто много продаёт.
    analogs = [_mk(1, 2200), _mk(2, 2000)]
    top = [_mk(10, 2000), _mk(11, 2000), _mk(12, 2000)]
    s = compute_score(analogs, top)
    assert s.sku_demand_score > 0.6      # на уровне лидеров
    assert s.verdict in ("GREEN", "STRONG")


def test_strong_advice_is_actionable_not_unknown_fallback():
    # Регресс: при добавлении STRONG в build_advice не было ветки STRONG, и лучший
    # товар получал fallback-совет «Не хватает данных, переснимите фото».
    analogs = [_mk(1, 2200), _mk(2, 2000), _mk(3, 2100)]
    top = [_mk(10, 1000), _mk(11, 1000), _mk(12, 1000)]
    s = compute_score(analogs, top, purchase_price=120.0)
    assert s.verdict == "STRONG"
    advice, reasons = build_advice(s, purchase_price=120.0)
    assert "Не хватает данных" not in advice
    assert "уверенно" in advice.lower()
    # дошли до блока наценки (а не ранний return как у UNKNOWN)
    assert any("наценка" in r for r in reasons)


def test_dead_niche_is_red():
    analogs = [_mk(1, 50)]
    top = [_mk(10, 0), _mk(11, 0)]  # топ есть, но продаж нет
    s = compute_score(analogs, top)
    assert s.verdict == "RED"
    assert ReasonCode.DEAD_NICHE.value in s.reasons


def test_no_benchmark_is_unknown():
    analogs = [_mk(1, 100)]
    s = compute_score(analogs, [])
    assert s.verdict == "UNKNOWN"
    assert ReasonCode.NO_BENCHMARK.value in s.reasons


def test_no_analogs_is_unknown():
    s = compute_score([], [_mk(10, 100)])
    assert s.verdict == "UNKNOWN"
    assert ReasonCode.LOW_FEEDBACKS.value in s.reasons


def test_advice_includes_markup():
    analogs = [_mk(1, 60), _mk(2, 80)]
    top = [_mk(10, 100), _mk(11, 100)]
    s = compute_score(analogs, top)
    advice, reasons = build_advice(s, purchase_price=100.0)
    # рынок = median(900, 900) = 900, наценка ×9 от закупа
    joined = " ".join(reasons)
    assert "×9" in joined or "×9.0" in joined


def test_low_margin_blocks_purchase():
    analogs = [_mk(1, 200, price=1000)]
    top = [_mk(10, 200, price=1000)]
    s = compute_score(analogs, top, purchase_price=850)
    assert s.verdict == "RED"
    assert ReasonCode.LOW_MARGIN.value in s.reasons


def test_snapshot_velocity_can_drive_demand_score():
    class M:
        matched_count = 2
        observation_days = 3.0
        trend_score = 0.85

        def __init__(self, median):
            self.median_feedback_sales_30d = median

    analogs = [_mk(1, 40), _mk(2, 40)]
    top = [_mk(10, 100), _mk(11, 100)]
    s = compute_score(
        analogs,
        top,
        analog_snapshot_metrics=M(200),
        top_snapshot_metrics=M(100),
    )
    assert s.sku_demand_score == 2.0
    assert s.trend_score == 0.85
    assert ReasonCode.SNAPSHOT_VELOCITY.value in s.reasons


def test_size_invariance_of_sku_demand():
    """Регресс-тест: визуальный поиск вернул 10 аналогов вместо 3 — sku_demand
    не должен подпрыгнуть, потому что считается по медиане, а не сумме."""
    base = compute_score([_mk(i, 100) for i in range(3)], [_mk(10, 100)])
    extended = compute_score([_mk(i, 100) for i in range(10)], [_mk(10, 100)])
    assert base.sku_demand_score == extended.sku_demand_score
    # А niche_volume — да, должен вырасти (это и есть его смысл).
    assert extended.niche_volume_score > base.niche_volume_score
