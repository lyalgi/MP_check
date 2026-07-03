from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

Verdict = Literal["STRONG", "GREEN", "YELLOW", "RED", "UNKNOWN"]


class AnalogSku(BaseModel):
    nm_id: int
    name: str
    brand: str | None = None
    image: str | None = None
    price: float
    sale_price: float | None = None
    feedbacks: int = 0
    rating: float | None = None
    stocks: int | None = None
    sales_30d_est: float = 0.0
    url: str


class PipelineStep(BaseModel):
    name: str
    detail: str | None = None
    count: int | None = None


class LookupResponse(BaseModel):
    verdict: Verdict
    liquidity_score: float = 0.0           # 0..100 — итоговый коммерческий балл
    wb_popularity_score: float = 0.0       # 0..100 — насколько найденный товар популярен на WB
    wb_demand_verdict: Verdict = "UNKNOWN" # чистый WB-ответ: популярен ли товар/аналоги на WB
    wb_demand_units_month: float = 0.0     # оценка продаж найденного товара/аналогов на WB, шт/мес
    rating: float                          # служебный главный балл (гарм. среднее двух) — для деталей
    demand_score: float = 0.0              # 0..1 — текущий спрос
    sell_through_score: float = 0.0        # 0..1 — спрос против остатков
    margin_score: float = 0.0              # 0..1 — запас наценки от закупа до рынка
    competition_score: float = 0.0         # 0..1 — риск перегрева/концентрации
    trend_score: float = 0.0               # 0..1 — динамика по снимкам
    data_quality_score: float = 0.0        # 0..1 — полнота данных
    sku_demand_score: float = 0.0          # median(analogs) / median(top)
    niche_volume_score: float = 0.0        # Σ(analogs) / Σ(top)
    decision_confidence: Literal["HIGH", "MEDIUM", "LOW"] = "LOW"
    rating_green_threshold: float
    rating_yellow_threshold: float

    description: str | None = None

    # шаги пайплайна (ТЗ 1-6)
    pipeline: list[PipelineStep] = Field(default_factory=list)

    # шаг 1: визуальный поиск
    visual_search_count: int = 0
    # шаг 2: фильтр по отзывам и совокупный спрос
    filtered_analog_count: int = 0
    analog_total_feedbacks: int = 0
    analog_total_sales_30d: float = 0.0
    analog_median_sales_30d: float = 0.0

    # шаг 3: целевая категория (взвешенное голосование по subjectId)
    top_seed_nm_id: int | None = None
    wb_subject_id: int | None = None
    wb_subject_name: str | None = None
    wb_parent_name: str | None = None
    subject_vote_share: float = 0.0        # доля доминирующей категории (0..1)

    # шаг 4: маппинг
    ozon_category: str | None = None
    mapping_kind: str | None = None

    # шаг 5: эталон рынка
    top_avg_sales_30d: float = 0.0
    top_total_sales_30d: float = 0.0
    top_median_sales_30d: float = 0.0
    top_count: int = 0
    top_source: str | None = None              # wb_catalog | visual_subset_in_subject | none
    top_is_heuristic: bool = False             # True если эталон — эвристика

    # Прогноз закупа (шаг 7 — тестовая партия для нового товара).
    # Поля Разноторга/K оставлены как справка, но не входят в recommended_units_year.
    market_share_coefficient: float | None = None     # K = razno/marketplace (справочно)
    coefficient_match_kind: str | None = None         # exact | suffix | stem
    raznotorg_revenue_year: float | None = None       # выручка Разноторга в категории, ₽
    marketplace_revenue_year: float | None = None     # выручка категории на маркетплейсе, ₽
    raznotorg_units_year: float | None = None          # продажи Разноторга в категории, шт/год
    raznotorg_positions: int | None = None             # число позиций (видов) Разноторга
    baseline_units_per_position: float | None = None   # устаревшее/справка: шт/год на позицию
    wb_strength: float | None = None                   # устаревшее/справка: спрос товара / лидеры ниши
    forecast_units_year: float | None = None          # размер теста в штуках (устаревшее имя)
    wb_forecast_units_year: float | None = None       # устаревшее (не используется в новой модели)
    forecast_source: str | None = None                # test_quantity (или None)
    recommended_units_year: int | None = None         # действие сейчас: STRONG/GREEN/YELLOW test, RED 0
    snapshot_match_count: int = 0                     # сколько SKU имели предыдущий снимок
    snapshot_observation_days: float = 0.0            # максимальное окно наблюдения
    stock_pressure_months: float | None = None        # запас аналогов в месяцах спроса
    market_price_median: float | None = None          # медианная цена эталона
    retail_history_match_kind: str | None = None      # exact | suffix | stem | retail_suffix | retail_stem
    retail_history_item_count: int = 0                # строк внутренней истории
    retail_history_sales_year: float | None = None    # продажи Разноторга в штуках/год
    retail_history_stock: float | None = None         # текущий остаток в штуках
    retail_history_sell_through: float | None = None  # продажи / (продажи + остаток)
    retail_history_yoy_ratio: float | None = None     # продажи текущего периода / прошлого
    retail_history_median_price: float | None = None
    retail_history_markup: float | None = None
    retail_history_profitability: float | None = None

    advice: str
    reasons: list[str] = Field(default_factory=list)
    verdict_reasons: list[str] = Field(default_factory=list)  # коды причин
    examples: list[AnalogSku] = Field(default_factory=list)

    duration_ms: int = 0


class TaxonomyGroupOut(BaseModel):
    group: str
    subgroup_count: int
    vid_count: int


class TaxonomySubgroupOut(BaseModel):
    subgroup: str
    vid_count: int


class TaxonomyVidOut(BaseModel):
    vid: str
    tovaroved: str | None = None
    wb_paths: list[str] = Field(default_factory=list)


class HistoryItemOut(BaseModel):
    id: int
    created_at: datetime
    purchase_price: float
    rating: float
    verdict: str
    decision_confidence: str | None = None
    verdict_reasons: list[str] = Field(default_factory=list)
    wb_subject_name: str | None = None
    advice: str | None
