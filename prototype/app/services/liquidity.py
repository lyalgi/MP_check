"""Расчёт рейтинга ликвидности (два независимых балла).

После фидбека ревьюверов:
  - sku_demand_score = median(analogs sales_30d) / median(top sales_30d)
      => «насколько типичный аналог нашей модели по спросу сравним с
         типичным лидером ниши». Устойчиво к размеру выдачи визуального поиска.
  - niche_volume_score = Σ(analogs sales_30d) / Σ(top sales_30d)
      => «есть ли в категории денежная емкость». Чтобы не давать GREEN
         на узкие нишевые товары, где даже лидеры мало продают.

Вердикт:
  GREEN   — оба балла > rating_green (по умолчанию 0.6).
  YELLOW  — хотя бы один балл между yellow и green; или один сильный,
            другой средний.
  RED     — оба балла ниже rating_yellow.
  UNKNOWN — не хватает данных (аналоги/топ пустые или эталон = 0).

Старая формула (Σ analogs / avg top) явно ломалась на крайних случаях:
  • 50 слабых аналогов суммарно > 1 средний лидер → ложный GREEN
  • 2 идеальных аналога < 1 средний лидер → ложный RED
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any

from app.schemas import AnalogSku
from app.services.reason_codes import ReasonCode
from app.settings import settings


@dataclass(frozen=True)
class LiquidityScore:
    rating: float                # главный служебный балл для интерфейса (взвешенное среднее двух)
    liquidity_score: float       # 0..100 — перцентиль median-аналога в нише (главный балл)
    wb_popularity_score: float   # 0..100 — чистая популярность на WB, без Разноторга/маржи
    demand_score: float          # 0..1 — спрос
    sell_through_score: float    # 0..1 — спрос против остатков
    margin_score: float          # 0..1 — запас наценки
    competition_score: float     # 0..1 — риск перегрева/концентрации
    trend_score: float           # 0..1 — динамика по снимкам
    data_quality_score: float    # 0..1 — полнота данных
    sku_demand_score: float      # median analogs / median top
    niche_volume_score: float    # Σ analogs / Σ top
    verdict: str                 # GREEN | YELLOW | RED | UNKNOWN
    reasons: list[str]           # коды из ReasonCode

    analog_count: int
    top_count: int
    analog_total_sales_30d: float
    top_total_sales_30d: float
    analog_median_sales_30d: float
    top_median_sales_30d: float
    snapshot_match_count: int = 0
    snapshot_observation_days: float = 0.0
    stock_pressure_months: float | None = None
    market_price_median: float | None = None


def _median_sales(items: list[AnalogSku]) -> float:
    if not items:
        return 0.0
    return float(statistics.median(a.sales_30d_est for a in items))


def _sum_sales(items: list[AnalogSku]) -> float:
    return float(sum(a.sales_30d_est for a in items))


def percentile_rank(value: float, population: list[float]) -> float:
    """Перцентиль value в population (метод среднего ранга).
    Возвращает 0..100 — какой % популяции value обгоняет.
    50 = медиана ниши, 80+ = верхушка, <20 = аутсайдер."""
    if not population or value <= 0:
        return 0.0
    below = sum(1 for x in population if x < value)
    equal = sum(1 for x in population if x == value)
    return round(100.0 * (below + 0.5 * equal) / len(population), 1)


def compute_score(
    analogs: list[AnalogSku],
    top: list[AnalogSku],
    *,
    purchase_price: float | None = None,
    analog_snapshot_metrics: Any | None = None,
    top_snapshot_metrics: Any | None = None,
    rating_green: float | None = None,
    rating_yellow: float | None = None,
) -> LiquidityScore:
    g = rating_green if rating_green is not None else settings.rating_green
    y = rating_yellow if rating_yellow is not None else settings.rating_yellow

    a_sum = _sum_sales(analogs)
    a_med = _median_sales(analogs)
    t_sum = _sum_sales(top)
    t_med = _median_sales(top)

    reasons: list[str] = []

    # 1) проверка наличия аналогов
    if not analogs:
        reasons.append(ReasonCode.LOW_FEEDBACKS.value)
        return _empty(reasons, analogs, top, a_sum, a_med, t_sum, t_med)

    # 2) проверка эталона
    if not top:
        reasons.append(ReasonCode.NO_BENCHMARK.value)
        return _empty(reasons, analogs, top, a_sum, a_med, t_sum, t_med)

    # 3) мёртвая ниша: эталон есть, но продажи там нулевые
    if t_sum <= 0:
        reasons.append(ReasonCode.DEAD_NICHE.value)
        return LiquidityScore(
            rating=0.0,
            liquidity_score=0.0,
            wb_popularity_score=0.0,
            demand_score=0.0,
            sell_through_score=0.0,
            margin_score=0.0,
            competition_score=0.0,
            trend_score=0.0,
            data_quality_score=0.0,
            sku_demand_score=0.0,
            niche_volume_score=0.0,
            verdict="RED",
            reasons=reasons,
            analog_count=len(analogs),
            top_count=len(top),
            analog_total_sales_30d=a_sum,
            top_total_sales_30d=t_sum,
            analog_median_sales_30d=a_med,
            top_median_sales_30d=t_med,
        )

    a_med_effective, t_med_effective, snapshot_count, observation_days, trend_score, snapshot_reason = (
        _effective_velocity(a_med, t_med, analog_snapshot_metrics, top_snapshot_metrics)
    )
    if snapshot_reason:
        reasons.append(snapshot_reason)

    sku_demand = a_med_effective / t_med_effective if t_med_effective > 0 else 0.0
    niche_volume = a_sum / t_sum  # знаменатель > 0 здесь гарантировано

    # Абсолютная компонента спроса: товар, продающий много штук/мес, ликвиден сам
    # по себе, даже если лидеры ниши продают в разы больше. Иначе средние товары
    # в «top-heavy» нишах получали ложный RED (товар на 480 шт/мес → RED только
    # потому, что лидеры продают 1850). Спрос = max(относительно лидеров; абсолютно).
    abs_demand = (a_med_effective / settings.absolute_demand_target_month
                  if settings.absolute_demand_target_month > 0 else 0.0)
    demand_signal = max(sku_demand, abs_demand)

    # Главный служебный rating — гармоническое среднее, чтобы плохой балл не маскировался.
    if demand_signal > 0 and niche_volume > 0:
        rating = 2 * demand_signal * niche_volume / (demand_signal + niche_volume)
    else:
        rating = 0.0

    # Главный наглядный балл 0..100: перцентиль медианного аналога
    # в распределении продаж ниши (топ-100 + сами аналоги, дедуп по nm_id).
    pop_map = {t.nm_id: t.sales_30d_est for t in top}
    for a in analogs:
        pop_map.setdefault(a.nm_id, a.sales_30d_est)
    percentile_score = percentile_rank(a_med_effective, list(pop_map.values()))

    demand_score = _clamp(demand_signal / max(0.01, g))
    niche_score = _clamp(niche_volume / max(0.01, g))
    stock_pressure_months, sell_through_score = _sell_through(analogs, a_med_effective)
    market_price_median, margin_score = _margin_score(purchase_price, top or analogs)
    competition_score = _competition_score(top, sell_through_score, reasons)
    data_quality_score = _data_quality_score(analogs, top, snapshot_count)

    if margin_score <= 0.25:
        reasons.append(ReasonCode.LOW_MARGIN.value)
    if stock_pressure_months is not None and stock_pressure_months > 6:
        reasons.append(ReasonCode.HIGH_STOCK_PRESSURE.value)
    if data_quality_score < 0.55:
        reasons.append(ReasonCode.LOW_DATA_QUALITY.value)
    if trend_score <= 0.25:
        reasons.append(ReasonCode.DECLINING_TREND.value)

    # Спрос-первичная модель (поиск НОВЫХ товаров): конкуренция WB — не поле боя
    # для офлайна, поэтому её вес снижен (0.12→0.05), освобождённое отдано спросу
    # (0.26→0.33). Конкуренция остаётся информативным сигналом ширины рынка.
    commercial_score = round(100.0 * (
        0.33 * demand_score +
        0.14 * niche_score +
        0.20 * sell_through_score +
        0.20 * margin_score +
        0.05 * competition_score +
        0.08 * trend_score
    ), 1)
    # Перцентиль оставляем как стабилизатор: если модель внизу распределения,
    # коммерческий балл не должен выглядеть чрезмерно бодрым.
    liquidity_score = round(0.75 * commercial_score + 0.25 * percentile_score, 1)

    # Вердикт через жёсткие условия: GREEN только если спрос, объём, маржа и оборачиваемость
    # одновременно нормальные. Один сильный сигнал не маскирует слабое место.
    # Ищем ХИТЫ, а не раздаём GREEN: GREEN — только товар уровня лидеров ниши
    # (sku_demand > g И niche_volume > g, строго относительно). Абсолютный спрос
    # НЕ даёт GREEN сам по себе — он лишь вытягивает середняка из RED в YELLOW
    # (товар на 400+/мес продаётся, но без относительной силы это «на тест», не хит).
    if margin_score <= 0.25:
        verdict = "RED"
    elif stock_pressure_months is not None and stock_pressure_months > 9 and demand_score < 0.75:
        verdict = "RED"
    elif sku_demand > g and niche_volume > g and margin_score >= 0.55 and sell_through_score >= 0.45:
        verdict = "GREEN"
        reasons += [ReasonCode.HIGH_SKU_DEMAND.value, ReasonCode.HIGH_NICHE_VOLUME.value]
    elif demand_signal > g or niche_volume > g:
        verdict = "YELLOW"
        reasons.append(ReasonCode.MODERATE_DEMAND.value)
    elif demand_signal > y and niche_volume > y:
        verdict = "YELLOW"
        reasons.append(ReasonCode.MODERATE_DEMAND.value)
    elif demand_signal <= y and niche_volume <= y:
        verdict = "RED"
        reasons.append(ReasonCode.LOW_SKU_DEMAND.value)
    else:
        verdict = "YELLOW"
        reasons.append(ReasonCode.MODERATE_DEMAND.value)

    # STRONG — исключительно сильный спрос: расширенный тест (но всё ещё тест).
    # GREEN-условия + верх перцентиля ниши или очень высокий абсолютный спрос.
    if verdict == "GREEN" and (percentile_score >= 80 or demand_signal >= 1.5):
        verdict = "STRONG"

    return LiquidityScore(
        rating=round(rating, 3),
        liquidity_score=liquidity_score,
        wb_popularity_score=percentile_score,
        demand_score=round(demand_score, 3),
        sell_through_score=round(sell_through_score, 3),
        margin_score=round(margin_score, 3),
        competition_score=round(competition_score, 3),
        trend_score=round(trend_score, 3),
        data_quality_score=round(data_quality_score, 3),
        sku_demand_score=round(sku_demand, 3),
        niche_volume_score=round(niche_volume, 3),
        verdict=verdict,
        reasons=reasons,
        analog_count=len(analogs),
        top_count=len(top),
        analog_total_sales_30d=round(a_sum, 1),
        top_total_sales_30d=round(t_sum, 1),
        analog_median_sales_30d=round(a_med_effective, 1),
        top_median_sales_30d=round(t_med_effective, 1),
        snapshot_match_count=snapshot_count,
        snapshot_observation_days=observation_days,
        stock_pressure_months=round(stock_pressure_months, 2) if stock_pressure_months is not None else None,
        market_price_median=round(market_price_median, 2) if market_price_median is not None else None,
    )


def _empty(reasons, analogs, top, a_sum, a_med, t_sum, t_med) -> LiquidityScore:
    return LiquidityScore(
        rating=0.0,
        liquidity_score=0.0,
        wb_popularity_score=0.0,
        demand_score=0.0,
        sell_through_score=0.0,
        margin_score=0.0,
        competition_score=0.0,
        trend_score=0.0,
        data_quality_score=0.0,
        sku_demand_score=0.0,
        niche_volume_score=0.0,
        verdict="UNKNOWN",
        reasons=reasons,
        analog_count=len(analogs),
        top_count=len(top),
        analog_total_sales_30d=a_sum,
        top_total_sales_30d=t_sum,
        analog_median_sales_30d=a_med,
        top_median_sales_30d=t_med,
    )


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def _effective_velocity(a_med: float, t_med: float, analog_metrics: Any | None, top_metrics: Any | None):
    a_snap = getattr(analog_metrics, "median_feedback_sales_30d", None) if analog_metrics else None
    t_snap = getattr(top_metrics, "median_feedback_sales_30d", None) if top_metrics else None
    a_count = int(getattr(analog_metrics, "matched_count", 0) or 0) if analog_metrics else 0
    t_count = int(getattr(top_metrics, "matched_count", 0) or 0) if top_metrics else 0
    observation_days = max(
        float(getattr(analog_metrics, "observation_days", 0.0) or 0.0) if analog_metrics else 0.0,
        float(getattr(top_metrics, "observation_days", 0.0) or 0.0) if top_metrics else 0.0,
    )
    trend = float(getattr(analog_metrics, "trend_score", 0.5) or 0.5) if analog_metrics else 0.5

    if a_snap is not None and t_snap is not None and a_count > 0 and t_count > 0:
        return float(a_snap), float(t_snap), a_count + t_count, round(observation_days, 2), trend, ReasonCode.SNAPSHOT_VELOCITY.value
    return a_med, t_med, a_count + t_count, round(observation_days, 2), trend, ReasonCode.SNAPSHOT_COLD_START.value


def _sell_through(analogs: list[AnalogSku], monthly_sales: float) -> tuple[float | None, float]:
    stocks = [a.stocks for a in analogs if a.stocks is not None and a.stocks >= 0]
    if not stocks or monthly_sales <= 0:
        return None, 0.5
    pressure = statistics.median(stocks) / max(1.0, monthly_sales)
    if pressure <= 1:
        return float(pressure), 1.0
    if pressure <= 2:
        return float(pressure), 0.8
    if pressure <= 4:
        return float(pressure), 0.55
    if pressure <= 6:
        return float(pressure), 0.35
    return float(pressure), 0.15


def _margin_score(purchase_price: float | None, market_items: list[AnalogSku]) -> tuple[float | None, float]:
    prices = [i.sale_price or i.price for i in market_items if (i.sale_price or i.price) > 0]
    if not prices:
        return None, 0.5
    market_median = float(statistics.median(prices))
    if not purchase_price or purchase_price <= 0:
        return market_median, 0.65
    markup = market_median / purchase_price
    if markup < 1.3:
        return market_median, 0.1
    if markup < 1.5:
        return market_median, 0.35
    if markup < 2.0:
        return market_median, 0.65
    if markup < 3.0:
        return market_median, 0.85
    return market_median, 1.0


def _competition_score(top: list[AnalogSku], sell_through_score: float, reasons: list[str]) -> float:
    if len(top) < 10:
        return 0.55 * sell_through_score + 0.45 * 0.55
    total = sum(t.sales_30d_est for t in top)
    if total <= 0:
        return 0.5
    top5 = sum(t.sales_30d_est for t in sorted(top, key=lambda x: x.sales_30d_est, reverse=True)[:5])
    concentration = top5 / total
    if concentration >= 0.75:
        reasons.append(ReasonCode.TOP_HEAVY_CATEGORY.value)
    concentration_score = 1.0 - _clamp((concentration - 0.35) / 0.45)
    return _clamp(0.55 * sell_through_score + 0.45 * concentration_score)


def _data_quality_score(analogs: list[AnalogSku], top: list[AnalogSku], snapshot_count: int) -> float:
    score = 1.0
    if len(analogs) < 3:
        score -= 0.25
    if len(top) < 10:
        score -= 0.2
    if snapshot_count == 0:
        score -= 0.15
    return _clamp(score)


def build_advice(score: LiquidityScore, purchase_price: float) -> tuple[str, list[str]]:
    """Человеческий совет + список причин для UI."""
    reasons: list[str] = []
    if score.verdict == "STRONG":
        advice = "Брать уверенно — товар уровня лидеров ниши. Можно расширенный тест."
        reasons.append(
            f"коммерческий балл {score.liquidity_score:.0f}/100: спрос {score.demand_score:.2f}, "
            f"маржа {score.margin_score:.2f}, оборачиваемость {score.sell_through_score:.2f}"
        )
    elif score.verdict == "GREEN":
        advice = "Брать смело. Высокая ликвидность и денежная ниша."
        reasons.append(
            f"коммерческий балл {score.liquidity_score:.0f}/100: спрос {score.demand_score:.2f}, "
            f"маржа {score.margin_score:.2f}, оборачиваемость {score.sell_through_score:.2f}"
        )
    elif score.verdict == "YELLOW":
        advice = "Тестовая закупка. Возьми ограниченную партию и проверь оборачиваемость."
        reasons.append(
            f"средний риск: спрос {score.demand_score:.2f}, маржа {score.margin_score:.2f}, "
            f"остатки/продажи {score.sell_through_score:.2f}"
        )
    elif score.verdict == "RED":
        if ReasonCode.DEAD_NICHE.value in score.reasons:
            advice = "Не брать — ниша мёртвая. В топ-30 нет продаж."
            reasons.append("в топ-30 категории все продажи нулевые")
        else:
            advice = "Не брать. Низкий спрос на модель и/или малая ниша."
            reasons.append(
                f"низкий балл {score.liquidity_score:.0f}/100: спрос {score.demand_score:.2f}, "
                f"маржа {score.margin_score:.2f}, оборачиваемость {score.sell_through_score:.2f}"
            )
    else:  # UNKNOWN
        if ReasonCode.NO_BENCHMARK.value in score.reasons:
            advice = "Нет данных по топу ниши — категорию не определили или WB не отдаёт данные."
            reasons.append("эталон топ-30 пустой")
        elif ReasonCode.LOW_FEEDBACKS.value in score.reasons:
            advice = "Все найденные аналоги ниже порога отзывов — это новые карточки или нишевый товар."
            reasons.append(f"нет ни одного аналога с feedbacks ≥ {settings.min_feedbacks}")
        else:
            advice = "Не хватает данных WB. Попробуй другую фотографию или вставь ссылку."
            reasons.append("пайплайн остановлен — данных недостаточно для вердикта")
        return advice, reasons

    # Сравнение цены закупа с топом ниши — берём ТУ ЖЕ медиану, что и margin
    # балл (полный топ-100), а не пере-считываем по 5 примерам. Иначе в одном
    # ответе появлялись две разные «медианы рынка» (по топ-100 и по 5 карточкам).
    avg_retail = score.market_price_median
    if avg_retail and purchase_price > 0:
        markup = avg_retail / purchase_price
        if markup < 1.5:
            reasons.append(
                f"наценка к топу ниши ×{markup:.1f} (медиана рынка {avg_retail:.0f}₽ vs закуп {purchase_price:.0f}₽) — "
                "юнит-экономика будет тонкой"
            )
        else:
            reasons.append(
                f"наценка к рынку ×{markup:.1f} (медиана рынка {avg_retail:.0f}₽ vs закуп {purchase_price:.0f}₽)"
            )
    return advice, reasons
