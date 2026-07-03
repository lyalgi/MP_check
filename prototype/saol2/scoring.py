"""Скоринг и вердикт (методология v2 — экономически заземлённая).

Решения (docs/МЕТОДОЛОГИЯ_v2.md):
  • спрос меряем по ВЫКУПАМ (заказы × выкуп%) — реальные продажи, само чинит одежду;
  • порог спроса = МЕДИАНА живых карточек категории (не магическая константа);
  • балл = 100 · скорость · шлюз_маржи · тренд, где
        скорость = 0.65·абсолют(к порогу) + 0.35·к топ-10 категории;
        шлюз_маржи — мягкий множитель «запас до рынка ВБ» (не жёсткий RED);
        тренд — растёт/ровно/падает за год;
  • тест-партия — newsvendor (критический фрактиль из маржи и доли уценки);
  • «надёжность» убрана; выкуп% входит в спрос, не отдельный столб.
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass, field

from saol2.metrics import ItemMetrics


@dataclass
class Settings:
    min_analogs: int = 3                 # меньше — LOW_SAMPLE
    min_niche: int = 5                   # меньше живых похожих → ниша достраивается из категории (subject_items)
    vote_share: float = 0.5              # доля голосов категории-лидера (similar даёт 1–2 смежных субъекта)
    # абсолют меряем в ДЕНЬГАХ (выручка типичной карточки ниши ₽/мес). Ниша строится из MPStats
    # similar (≈ категория), поэтому пол — АБСОЛЮТНЫЙ (относительный к категории был бы цикличен).
    money_floor: float = 50000.0         # выручка типичной карточки ТИПА ниже → кандидат «не везти»
    unit_floor: float = 200.0            # ИЛИ выкупов/мес типичной карточки — спасает дешёвые ходовые
    # веса спроса
    w_abs: float = 0.65                  # абсолют (выручка к полу категории)
    w_top: float = 0.35                  # отношение к топ-10 категории (по выручке)
    top_k: int = 10
    # тренд
    trend_up: float = 1.15               # ≥ — растёт
    trend_down: float = 0.85             # ≤ — падает
    trend_coef_up: float = 1.1
    trend_coef_down: float = 0.85
    # фильтр по размеру (из «Уточнения»): держим похожих в полосе target×[lo..hi]
    size_band_lo: float = 0.7
    size_band_hi: float = 1.4
    # newsvendor
    markdown_fraction: float = 0.40      # доля себестоимости, теряемая на неликвиде (настраиваемо)
    # полосы вердикта по баллу — ПРЕДВАРИТЕЛЬНЫЕ, анкер «≈ типичный бестселлер категории = GREEN».
    # Калибруются на исходах (история Разноторга / накопленные тесты), а не на глаз.
    score_strong: float = 75.0
    score_green: float = 55.0
    score_yellow: float = 30.0
    abs_floor: float = 0.15              # ниже — спроса фактически нет → RED


@dataclass
class Verdict:
    verdict: str = "UNKNOWN"
    score_100: int | None = None
    provisional: bool = True             # полосы вердикта ещё не откалиброваны на исходах
    # спрос — абсолют в ДЕНЬГАХ
    redeemed_month_median: float = 0.0   # выкупы/мес типичной (медиана)
    orders_month_median: float = 0.0     # заказы/мес типичной (медиана)
    lead_redeemed_month: float = 0.0     # выкупы/мес ЛИДЕРОВ типа (топ-четверть) — гибрид-спрос
    lead_orders_month: float = 0.0       # заказы/мес лидеров типа
    lead_revenue_month: float | None = None  # выручка/мес лидеров типа
    abs_demand: float = 0.0              # выручка_ниши / пол категории (деньги)
    ratio_to_top: float | None = None    # выручка_ниши / медиана топ-10 (деньги)
    category_pct: float | None = None    # позиция ниши в категории по выручке, 0–100
    demand_score: float = 0.0            # скорость 0–1
    target_revenue: float | None = None  # пол = медиана выручки карточек категории
    # маржа
    market_price_median: float | None = None
    markup: float | None = None
    margin_pct: float | None = None      # валовая маржа % = 1 − 1/наценка
    margin_gate: float = 0.0             # мягкий шлюз 0.45–1.0
    buyout_pct_median: float | None = None
    # тренд
    trend_ratio: float | None = None
    trend_coef: float = 1.0
    trend_label: str | None = None
    # экономика
    niche_revenue_month: float | None = None  # ₽/мес типичной карточки ниши
    price_segment: dict | None = None         # где по цене сидит объём ПОХОЖИХ (ориентир ценника)
    test_capital: float | None = None         # партия × закуп (риск)
    potential_margin: float | None = None     # партия × (рынок − закуп)
    crit_fractile: float | None = None        # newsvendor CF
    # тест-партия
    test_units_per_store: int | None = None
    test_units: int | None = None
    stores: int | None = None
    # категория
    subject_id: int | None = None
    subject_name: str | None = None
    vote_share: float = 0.0
    analog_count: int = 0
    reasons: list[str] = field(default_factory=list)
    examples: list[dict] = field(default_factory=list)


def _median(xs: list[float]) -> float:
    return float(statistics.median(xs)) if xs else 0.0


def _redeemed_month(a: ItemMetrics) -> float:
    """Выкупы/мес аналога (заказы × выкуп%); если выкуп% неизвестен — заказы."""
    return a.redeemed_monthly_avg if a.redeemed_monthly_avg > 0 else a.orders_monthly_avg


def _position_pct(value: float, population: list[float]) -> float | None:
    """Позиция значения в распределении (0–100): доля карточек категории НИЖЕ value."""
    pop = [x for x in population if x > 0]
    if not pop or value <= 0:
        return None
    below = sum(1 for x in pop if x < value)
    return round(100.0 * below / len(pop))


def _price_band(points: list[tuple[float, float]], bins: int = 5) -> dict | None:
    """Ценовой диапазон, где сидит объём выкупов ПОХОЖИХ (а не всей категории) —
    ориентир для ценника. points = [(цена, выкупы/мес), ...]."""
    pts = [(p, w) for p, w in points if p > 0 and w > 0]
    if len(pts) < 3:
        return None
    lo, hi = min(p for p, _ in pts), max(p for p, _ in pts)
    if hi <= lo:
        return {"low": round(lo), "high": round(hi), "share": 100}
    width = (hi - lo) / bins
    buckets = [0.0] * bins
    for p, w in pts:
        buckets[min(bins - 1, int((p - lo) / width))] += w
    total = sum(buckets) or 1.0
    bi = max(range(bins), key=lambda i: buckets[i])
    return {"low": round(lo + bi * width), "high": round(lo + (bi + 1) * width),
            "share": round(100.0 * buckets[bi] / total)}


def vote_category(analogs: list[ItemMetrics], share: float) -> tuple[int | None, str | None, float]:
    """Взвешенное голосование по subject_id (вес = заказы/год)."""
    weights: dict[int, float] = defaultdict(float)
    names: dict[int, str | None] = {}
    for a in analogs:
        if a.subject_id and a.orders_year > 0:
            weights[a.subject_id] += a.orders_year
            names[a.subject_id] = a.subject_name
    if not weights:
        return None, None, 0.0
    total = sum(weights.values())
    sid, w = max(weights.items(), key=lambda kv: kv[1])
    frac = w / total if total else 0.0
    return (sid, names.get(sid), frac) if frac >= share else (None, None, frac)


def _margin_gate(markup: float | None) -> float:
    """Мягкий шлюз «запас до рынка ВБ». Не убивает (офлайн продаёт и дороже ВБ),
    только гасит, когда рыночная цена близка/ниже закупа."""
    if not markup:
        return 0.65                      # цена рынка неизвестна — нейтрально
    if markup >= 1.3:
        return 1.0
    if markup >= 1.1:
        return 0.85
    if markup >= 0.9:
        return 0.65
    return 0.45                          # рынок ≈ или ниже закупа


def _trend(ratio: float | None, s: Settings) -> tuple[float, str | None]:
    if ratio is None:
        return 1.0, None
    if ratio >= s.trend_up:
        return s.trend_coef_up, "растёт"
    if ratio <= s.trend_down:
        return s.trend_coef_down, "падает"
    return 1.0, "ровно"


def _test_per_store(abs_demand: float, crit_fractile: float | None, verdict: str) -> int | None:
    """Тест-партия НА ТОЧКУ по newsvendor-логике: базовый охват по силе спроса,
    скорректированный критическим фрактилем (маржа vs потеря на неликвиде)."""
    if verdict == "RED":
        return 0
    if verdict == "UNKNOWN":
        return None
    base = 6 if abs_demand >= 1.0 else 4 if abs_demand >= 0.5 else 2 if abs_demand >= 0.2 else 1
    cf = crit_fractile if crit_fractile is not None else 0.5
    return max(1, round(base * (0.5 + cf)))   # фактор 0.5…1.5


def score(
    analogs_all: list[ItemMetrics],
    purchase_price: float,
    *,
    settings: Settings | None = None,
    category_revenue: list[float] | None = None,
    trend_ratio: float | None = None,
    stores: int | None = None,
) -> Verdict:
    """Главный расчёт. category_revenue — выручка/мес товаров категории (срез
    subject/items) для денежного пола и отношения-к-топу; trend_ratio — тренд ниши за год."""
    s = settings or Settings()
    v = Verdict()

    live = [a for a in analogs_all if a.ok and a.in_stock and a.orders_year > 0]
    if not live:
        v.reasons.append("NO_LIVE_ANALOGS")
        return v

    sid, sname, vote = vote_category(live, s.vote_share)
    v.subject_id, v.subject_name, v.vote_share = sid, sname, round(vote, 3)
    if sid is None:
        v.reasons.append("HETEROGENEOUS_SUBJECTS")
        return v

    cat = [a for a in live if a.subject_id == sid]
    v.analog_count = len(cat)
    if len(cat) < s.min_analogs:
        v.reasons.append("LOW_SAMPLE")

    # ── медианы аналогов (выкупы/заказы/цена) ──
    redeemed = [_redeemed_month(a) for a in cat]
    r_med = _median(redeemed)
    v.redeemed_month_median = round(r_med, 1)
    v.orders_month_median = round(_median([a.orders_monthly_avg for a in cat]), 1)

    # ЛИДЕРЫ типа (топ-четверть по выкупам) — для ГИБРИД-спроса: офлайн-магазин не
    # конкурирует за выдачу WB, мёртвый хвост = конкуренция продавцов, не отсутствие спроса.
    leaders = sorted(cat, key=_redeemed_month, reverse=True)[:max(3, len(cat) // 4)]
    r_lead = _median([_redeemed_month(a) for a in leaders])
    v.lead_redeemed_month = round(r_lead, 1)
    v.lead_orders_month = round(_median([a.orders_monthly_avg for a in leaders]), 1)

    prices = [a.price for a in cat if a.price > 0]
    v.market_price_median = round(_median(prices), 0) if prices else None
    if v.market_price_median and purchase_price > 0:
        v.markup = round(v.market_price_median / purchase_price, 2)
        v.margin_pct = round(100.0 * (1.0 - 1.0 / v.markup), 1)
    v.margin_gate = round(_margin_gate(v.markup), 2)

    buyouts = [a.buyout_pct for a in cat if a.buyout_pct]
    v.buyout_pct_median = round(_median(buyouts), 1) if buyouts else None

    # ── СПРОС В ДЕНЬГАХ: выручка типичной карточки ниши vs пол категории ──
    mp = v.market_price_median
    v.niche_revenue_month = round(r_med * mp) if mp else None
    nrev = v.niche_revenue_month or 0.0
    lead_rev = round(r_lead * mp) if mp else 0.0
    v.lead_revenue_month = lead_rev or None
    # где по цене сидит объём ПОХОЖИХ (ориентир ценника) — по аналогам, не по категории
    v.price_segment = _price_band([(a.price, _redeemed_month(a)) for a in cat])
    # ГИБРИД-абсолют = ИЛИ деньги, ИЛИ штуки, по МАКС(типичная, лидеры): спрос на тип есть,
    # если ЛИБО типичная карточка жива, ЛИБО лидеры типа сильны (выше абсолютного пола).
    v.target_revenue = round(s.money_floor)
    money_score = max(nrev, lead_rev) / s.money_floor if s.money_floor > 0 else 0.0
    unit_score = max(r_med, r_lead) / s.unit_floor if s.unit_floor > 0 else 0.0
    v.abs_demand = round(max(money_score, unit_score), 3)
    # пометка ТОЛЬКО когда спрос вытянули ЛИДЕРЫ, а типичная сама по себе СЛАБА
    # (на здоровой типичной — как медведь — не шумим)
    typical_abs = max(nrev / s.money_floor, r_med / s.unit_floor) if (s.money_floor and s.unit_floor) else 0.0
    if v.abs_demand >= s.abs_floor and typical_abs < 0.5 and v.abs_demand > typical_abs * 1.5:
        v.reasons.append("LEADERS_CARRY")

    # отношение к топ-10 категории + позиция (контекст, не основа вердикта)
    pop = sorted([x for x in (category_revenue or []) if x > 0], reverse=True)
    if len(pop) >= 3:
        top_med = statistics.median(pop[:s.top_k])
        if top_med > 0:
            v.ratio_to_top = round(nrev / top_med, 3)
        v.category_pct = _position_pct(nrev, pop)
    elif not pop:
        v.reasons.append("NO_CATEGORY_SLICE")

    abs_c = min(1.0, v.abs_demand)
    top_c = min(1.0, v.ratio_to_top) if v.ratio_to_top is not None else abs_c
    v.demand_score = round(s.w_abs * abs_c + s.w_top * top_c, 3)

    # ── тренд ──
    v.trend_ratio = round(trend_ratio, 2) if trend_ratio else None
    v.trend_coef, v.trend_label = _trend(trend_ratio, s)

    # ── общий балл ──
    v.score_100 = round(100 * v.demand_score * v.margin_gate * v.trend_coef)

    # ── экономика (newsvendor) ──
    cu = (v.market_price_median - purchase_price) if (v.market_price_median and purchase_price > 0) else None
    co = purchase_price * s.markdown_fraction if purchase_price > 0 else None
    if cu and cu > 0 and co and co > 0:
        v.crit_fractile = round(cu / (cu + co), 3)

    # ── вердикт (из балла + отсечки) ──
    if (v.markup or 99) < 0.9:
        v.reasons.append("MARKET_BELOW_COST")
    good_margin = v.margin_gate >= 0.85
    strong_demand = v.abs_demand >= 1.0

    if v.abs_demand < s.abs_floor:
        v.verdict = "RED"; v.reasons.append("LOW_DEMAND")
    elif v.score_100 >= s.score_strong and strong_demand and good_margin:
        v.verdict = "STRONG"; v.reasons.append("STRONG_DEMAND")
    elif v.score_100 >= s.score_green:
        v.verdict = "GREEN"; v.reasons.append("GOOD_DEMAND")
    elif v.score_100 >= s.score_yellow:
        v.verdict = "YELLOW"; v.reasons.append("MODERATE_DEMAND")
    else:
        v.verdict = "RED"; v.reasons.append("WEAK_OVERALL")

    # тонкая выборка гасит GREEN/STRONG до YELLOW — НО не когда ниша подтверждена внешним
    # срезом категории (высокая позиция): пара аналогов в топ-20% категории — это надёжно.
    corroborated = (v.category_pct or 0) >= 80
    if "LOW_SAMPLE" in v.reasons and v.verdict in ("GREEN", "STRONG") and not corroborated:
        v.verdict = "YELLOW"; v.reasons.append("CAPPED_LOW_SAMPLE")

    # балл не противоречит вердикту: жёсткий RED держим в красной зоне
    if v.verdict == "RED" and v.score_100 is not None and v.score_100 >= s.score_yellow:
        v.score_100 = int(s.score_yellow) - 1

    # ── тест-партия (newsvendor) ──
    v.stores = stores if (stores and stores > 0) else None
    v.test_units_per_store = _test_per_store(v.abs_demand, v.crit_fractile, v.verdict)
    if v.test_units_per_store is not None and v.stores:
        v.test_units = v.test_units_per_store * v.stores
    else:
        v.test_units = v.test_units_per_store
    if v.test_units and purchase_price > 0:
        v.test_capital = round(v.test_units * purchase_price)
        if cu and cu > 0:
            v.potential_margin = round(v.test_units * cu)

    # ── примеры для закупщика: топ-5 по выкупам ──
    for a in sorted(cat, key=_redeemed_month, reverse=True)[:5]:
        v.examples.append({
            "nm": a.nm, "name": a.name[:50], "price": a.price,
            "orders_month": a.orders_monthly_avg, "redeemed_month": round(_redeemed_month(a), 1),
            "buyout_pct": a.buyout_pct, "image": a.image_thumb,
        })
    return v
