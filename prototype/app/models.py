from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, Float, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TaxonomyItem(Base):
    """Запись из xlsx-классификатора Разноторга: Вид + мэппинг на WB/OZON."""

    __tablename__ = "taxonomy_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    group: Mapped[str] = mapped_column(String(255), index=True)
    subgroup: Mapped[str] = mapped_column(String(255), index=True)
    vid: Mapped[str] = mapped_column(String(255), index=True)
    tovaroved: Mapped[str | None] = mapped_column(String(255), nullable=True)

    sold_qty: Mapped[float] = mapped_column(Float, default=0.0)
    stock_qty: Mapped[float] = mapped_column(Float, default=0.0)
    sold_rub: Mapped[float] = mapped_column(Float, default=0.0)
    stock_rub: Mapped[float] = mapped_column(Float, default=0.0)
    cost_sold: Mapped[float] = mapped_column(Float, default=0.0)
    cost_stock: Mapped[float] = mapped_column(Float, default=0.0)

    wb_paths: Mapped[list] = mapped_column(JSON, default=list)
    ozon_paths: Mapped[list] = mapped_column(JSON, default=list)

    source_file: Mapped[str] = mapped_column(String(255))

    __table_args__ = (
        UniqueConstraint("group", "subgroup", "vid", name="uq_taxonomy_gsv"),
        Index("ix_taxonomy_search", "group", "subgroup", "vid"),
    )


class WbSubject(Base):
    """Справочник subjectId → name (загружается из static-basket-01.wbbasket.ru)."""

    __tablename__ = "wb_subjects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    parent_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    ru_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
    single_name: Mapped[str | None] = mapped_column(String(255), nullable=True)


class MarketSnapshot(Base):
    """Снимок публичных метрик карточки WB на момент запроса.

    Нужен, чтобы перейти от отзывов за всё время к реальной скорости: через 1-7
    дней можно считать дельту отзывов/остатков по тем же nm_id.
    """

    __tablename__ = "market_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)

    nm_id: Mapped[int] = mapped_column(Integer, index=True)
    source: Mapped[str] = mapped_column(String(32), index=True)  # визуальный поиск | эталон

    subject_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    subject_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    parent_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    name: Mapped[str] = mapped_column(String(512), default="")
    brand: Mapped[str | None] = mapped_column(String(255), nullable=True)
    price: Mapped[float] = mapped_column(Float, default=0.0)
    sale_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    feedbacks: Mapped[int] = mapped_column(Integer, default=0)
    rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    stocks: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sales_30d_est: Mapped[float] = mapped_column(Float, default=0.0)

    __table_args__ = (
        Index("ix_market_snapshot_nm_time", "nm_id", "captured_at"),
        Index("ix_market_snapshot_subject_time", "subject_id", "captured_at"),
    )


class MarketplaceCategoryRevenue(Base):
    """Годовая выручка категории на маркетплейсе (WB или OZON).
    Загружается из xlsx 'ВБ для ИИ выручка год.xlsx' и 'ОЗОН для ИИ выручка год.xlsx'.
    Это знаменатель коэффициента «доля Разноторга от рынка»."""

    __tablename__ = "marketplace_category_revenue"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    marketplace: Mapped[str] = mapped_column(String(8), index=True)  # 'wb' | 'ozon'
    path: Mapped[str] = mapped_column(String(512), index=True)
    revenue_year: Mapped[float] = mapped_column(Float, default=0.0)

    __table_args__ = (
        UniqueConstraint("marketplace", "path", name="uq_mp_revenue_path"),
        Index("ix_mp_revenue_path_last", "marketplace", "path"),
    )


class CategoryCoefficient(Base):
    """Коэффициент «доля Разноторга в категории».

    K = выручка_Разноторга_в_категории / выручка_категории_на_маркетплейсе
    Применяется так: прогноз_продаж_единиц_Разноторга_год = продажи_аналога_год_на_маркетплейсе * K.

    Пересчитывается scripts/build_category_coefficient.py.
    """

    __tablename__ = "category_coefficient"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    wb_path: Mapped[str] = mapped_column(String(512), index=True, unique=True)
    raznotorg_revenue_year: Mapped[float] = mapped_column(Float, default=0.0)
    marketplace_revenue_year: Mapped[float] = mapped_column(Float, default=0.0)
    coefficient: Mapped[float] = mapped_column(Float, default=0.0)
    # для модели закупа «позиция × WB-сила»: реальная скорость продаж позиции
    raznotorg_units_year: Mapped[float] = mapped_column(Float, default=0.0)
    raznotorg_positions: Mapped[int] = mapped_column(Integer, default=0)


class CategoryBenchmark(Base):
    """Кэш топ-N ниши по WB-категории (фоновый предрасчёт).

    Живой запрос читает отсюда вместо скрапинга catalog.wb.ru в реальном времени: быстрее и,
    главное, выборка топа стабильная и ПОЛНАЯ — не зависит от того, сколько
    успели собрать за бюджет времени (иначе рейтинг плавал бы от полноты скрапа).
    Заполняется scripts/build_category_benchmark.py (фоновый индексатор).
    """

    __tablename__ = "category_benchmark"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    wb_subject_id: Mapped[int] = mapped_column(Integer, index=True, unique=True)
    wb_path: Mapped[str] = mapped_column(String(512), default="")
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    item_count: Mapped[int] = mapped_column(Integer, default=0)
    items_json: Mapped[list] = mapped_column(JSON, default=list)


class RetailHistoryItem(Base):
    """Строка из внутренних реестров Разноторга «для ИИ».

    Это не маркетплейс и не классификатор, а фактическая история офлайн-сети:
    продажи/остатки/рентабельность/наценка по товару и виду. Нужна как
    предохранитель: WB может показывать спрос, но если в Разноторге похожий вид уже
    лежит в остатках или продаётся с минусовой маржей, GREEN должен стать
    хотя бы YELLOW.
    """

    __tablename__ = "retail_history_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    group: Mapped[str] = mapped_column(String(255), index=True)
    subgroup: Mapped[str] = mapped_column(String(255), index=True)
    vid: Mapped[str] = mapped_column(String(255), index=True)
    price_band: Mapped[str | None] = mapped_column(String(128), nullable=True)
    product_name: Mapped[str] = mapped_column(String(512), index=True)

    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    markup: Mapped[float | None] = mapped_column(Float, nullable=True)
    profitability_current: Mapped[float | None] = mapped_column(Float, nullable=True)
    profitability_prev: Mapped[float | None] = mapped_column(Float, nullable=True)
    sales_current: Mapped[float] = mapped_column(Float, default=0.0)
    sales_prev: Mapped[float] = mapped_column(Float, default=0.0)
    stock_current: Mapped[float] = mapped_column(Float, default=0.0)
    model_count_current: Mapped[float | None] = mapped_column(Float, nullable=True)
    model_count_prev: Mapped[float | None] = mapped_column(Float, nullable=True)

    period_current: Mapped[str | None] = mapped_column(String(64), nullable=True)
    period_prev: Mapped[str | None] = mapped_column(String(64), nullable=True)
    stock_date: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_file: Mapped[str] = mapped_column(String(255), index=True)
    source_sheet: Mapped[str | None] = mapped_column(String(255), nullable=True)

    __table_args__ = (
        Index("ix_retail_history_gsv", "group", "subgroup", "vid"),
        Index("ix_retail_history_source", "source_file", "source_sheet"),
    )


class WbOzonMapping(Base):
    """Статический маппинг категорий WB ↔ OZON (см. ТЗ шаг 4)."""

    __tablename__ = "wb_ozon_mapping"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    wb_path: Mapped[str] = mapped_column(String(512), index=True)
    ozon_path: Mapped[str] = mapped_column(String(512), index=True)
    source: Mapped[str] = mapped_column(String(64), default="taxonomy_xlsx")

    __table_args__ = (
        UniqueConstraint("wb_path", "ozon_path", name="uq_wb_ozon"),
    )


class LookupHistory(Base):
    """История запросов закупщиков — для аудита и обучения."""

    __tablename__ = "lookup_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, index=True)

    buyer_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    image_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    purchase_price: Mapped[float] = mapped_column(Float)
    seed_nm_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    visual_search_count: Mapped[int] = mapped_column(Integer, default=0)
    filtered_analog_count: Mapped[int] = mapped_column(Integer, default=0)
    analog_total_feedbacks: Mapped[int] = mapped_column(Integer, default=0)
    analog_total_sales_30d: Mapped[float] = mapped_column(Float, default=0.0)

    top_seed_nm_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    wb_subject_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    wb_subject_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    wb_parent_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ozon_category: Mapped[str | None] = mapped_column(String(512), nullable=True)

    top_avg_sales_30d: Mapped[float] = mapped_column(Float, default=0.0)
    rating: Mapped[float] = mapped_column(Float, default=0.0)
    liquidity_score: Mapped[float] = mapped_column(Float, default=0.0)
    wb_popularity_score: Mapped[float] = mapped_column(Float, default=0.0)
    wb_demand_verdict: Mapped[str] = mapped_column(String(16), default="UNKNOWN")
    wb_demand_units_month: Mapped[float] = mapped_column(Float, default=0.0)
    demand_score: Mapped[float] = mapped_column(Float, default=0.0)
    sell_through_score: Mapped[float] = mapped_column(Float, default=0.0)
    margin_score: Mapped[float] = mapped_column(Float, default=0.0)
    competition_score: Mapped[float] = mapped_column(Float, default=0.0)
    trend_score: Mapped[float] = mapped_column(Float, default=0.0)
    data_quality_score: Mapped[float] = mapped_column(Float, default=0.0)
    sku_demand_score: Mapped[float] = mapped_column(Float, default=0.0)
    niche_volume_score: Mapped[float] = mapped_column(Float, default=0.0)
    verdict: Mapped[str] = mapped_column(String(16))
    decision_confidence: Mapped[str] = mapped_column(String(16), default="LOW")
    verdict_reasons: Mapped[list] = mapped_column(JSON, default=list)
    subject_vote_share: Mapped[float] = mapped_column(Float, default=0.0)

    market_share_coefficient: Mapped[float | None] = mapped_column(Float, nullable=True)
    raznotorg_revenue_year: Mapped[float | None] = mapped_column(Float, nullable=True)
    marketplace_revenue_year: Mapped[float | None] = mapped_column(Float, nullable=True)
    forecast_units_year: Mapped[float | None] = mapped_column(Float, nullable=True)
    wb_forecast_units_year: Mapped[float | None] = mapped_column(Float, nullable=True)
    forecast_source: Mapped[str | None] = mapped_column(String(48), nullable=True)
    recommended_units_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    snapshot_match_count: Mapped[int] = mapped_column(Integer, default=0)
    snapshot_observation_days: Mapped[float] = mapped_column(Float, default=0.0)
    stock_pressure_months: Mapped[float | None] = mapped_column(Float, nullable=True)
    market_price_median: Mapped[float | None] = mapped_column(Float, nullable=True)
    retail_history_match_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    retail_history_item_count: Mapped[int] = mapped_column(Integer, default=0)
    retail_history_sales_year: Mapped[float | None] = mapped_column(Float, nullable=True)
    retail_history_stock: Mapped[float | None] = mapped_column(Float, nullable=True)
    retail_history_sell_through: Mapped[float | None] = mapped_column(Float, nullable=True)
    retail_history_yoy_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    retail_history_median_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    retail_history_markup: Mapped[float | None] = mapped_column(Float, nullable=True)
    retail_history_profitability: Mapped[float | None] = mapped_column(Float, nullable=True)

    advice: Mapped[str | None] = mapped_column(String, nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
