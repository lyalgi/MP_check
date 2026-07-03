"""Тесты кэша бенчмарка ниши (round-trip + протухание по TTL)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.models import CategoryBenchmark
from app.schemas import AnalogSku
from app.services.benchmark_cache import get_cached_benchmark, store_benchmark


def _sku(nm, sales):
    return AnalogSku(nm_id=nm, name=f"x{nm}", price=1000.0, feedbacks=50,
                     sales_30d_est=sales, url=f"https://wb/{nm}")


def test_store_and_get_roundtrip(db):
    items = [_sku(1, 100.0), _sku(2, 50.0), _sku(3, 25.0)]
    n = store_benchmark(db, 1859, "Дом/Освещение/Светильники", items)
    assert n == 3
    got = get_cached_benchmark(db, 1859, max_age_hours=168)
    assert got is not None
    assert [a.nm_id for a in got] == [1, 2, 3]
    assert got[0].sales_30d_est == 100.0


def test_store_overwrites_previous(db):
    store_benchmark(db, 1859, "p", [_sku(1, 10.0)])
    store_benchmark(db, 1859, "p", [_sku(2, 20.0), _sku(3, 30.0)])
    got = get_cached_benchmark(db, 1859, max_age_hours=168)
    assert [a.nm_id for a in got] == [2, 3]
    assert db.query(CategoryBenchmark).filter_by(wb_subject_id=1859).count() == 1


def test_miss_returns_none(db):
    assert get_cached_benchmark(db, 99999, max_age_hours=168) is None
    assert get_cached_benchmark(db, None, max_age_hours=168) is None


def test_stale_returns_none(db):
    store_benchmark(db, 1859, "p", [_sku(1, 10.0)])
    row = db.query(CategoryBenchmark).filter_by(wb_subject_id=1859).first()
    row.captured_at = datetime.now(timezone.utc) - timedelta(hours=200)
    db.commit()
    assert get_cached_benchmark(db, 1859, max_age_hours=168) is None      # протух
    assert get_cached_benchmark(db, 1859, max_age_hours=300) is not None  # ещё свеж
