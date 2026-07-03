from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.settings import settings


class Base(DeclarativeBase):
    pass


Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    settings.db_url,
    echo=False,
    future=True,
    connect_args={"check_same_thread": False},
)


@event.listens_for(engine, "connect")
def _register_unicode_lower(dbapi_connection, _connection_record):
    """SQLite по умолчанию даёт ASCII-only lower(); это ломает поиск по
    кириллице (например 'Детям' != 'детям'). Регистрируем Python-функцию."""
    dbapi_connection.create_function(
        "lower", 1,
        lambda s: s.lower() if isinstance(s, str) else s,
    )
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, future=True)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _migrate_sqlite(engine)


def _migrate_sqlite(db_engine: Engine) -> None:
    """Миграция SQLite-базы прототипа на месте.

    `create_all()` создаёт отсутствующие таблицы, но не добавляет колонки в уже
    существующие. SQLite-файл может жить между запусками приложения, поэтому
    новые поля истории добавляем явно, пока в проекте нет Alembic.
    """
    if db_engine.dialect.name != "sqlite":
        return

    additions = {
        "liquidity_score": "FLOAT DEFAULT 0.0",
        "wb_popularity_score": "FLOAT DEFAULT 0.0",
        "wb_demand_verdict": "VARCHAR(16) DEFAULT 'UNKNOWN'",
        "wb_demand_units_month": "FLOAT DEFAULT 0.0",
        "demand_score": "FLOAT DEFAULT 0.0",
        "sell_through_score": "FLOAT DEFAULT 0.0",
        "margin_score": "FLOAT DEFAULT 0.0",
        "competition_score": "FLOAT DEFAULT 0.0",
        "trend_score": "FLOAT DEFAULT 0.0",
        "data_quality_score": "FLOAT DEFAULT 0.0",
        "sku_demand_score": "FLOAT DEFAULT 0.0",
        "niche_volume_score": "FLOAT DEFAULT 0.0",
        "decision_confidence": "VARCHAR(16) DEFAULT 'LOW'",
        "verdict_reasons": "JSON DEFAULT '[]'",
        "subject_vote_share": "FLOAT DEFAULT 0.0",
        "market_share_coefficient": "FLOAT",
        "raznotorg_revenue_year": "FLOAT",
        "marketplace_revenue_year": "FLOAT",
        "forecast_units_year": "FLOAT",
        "wb_forecast_units_year": "FLOAT",
        "forecast_source": "VARCHAR(48)",
        "recommended_units_year": "INTEGER",
        "snapshot_match_count": "INTEGER DEFAULT 0",
        "snapshot_observation_days": "FLOAT DEFAULT 0.0",
        "stock_pressure_months": "FLOAT",
        "market_price_median": "FLOAT",
        "retail_history_match_kind": "VARCHAR(32)",
        "retail_history_item_count": "INTEGER DEFAULT 0",
        "retail_history_sales_year": "FLOAT",
        "retail_history_stock": "FLOAT",
        "retail_history_sell_through": "FLOAT",
        "retail_history_yoy_ratio": "FLOAT",
        "retail_history_median_price": "FLOAT",
        "retail_history_markup": "FLOAT",
        "retail_history_profitability": "FLOAT",
    }
    with db_engine.begin() as conn:
        existing = {
            row[1]
            for row in conn.exec_driver_sql("PRAGMA table_info(lookup_history)").fetchall()
        }
        if not existing:
            return
        for column, ddl in additions.items():
            if column not in existing:
                conn.exec_driver_sql(f"ALTER TABLE lookup_history ADD COLUMN {column} {ddl}")
