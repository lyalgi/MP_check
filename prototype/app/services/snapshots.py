"""Снимки маркетплейса для уточнения скорости продаж.

Первый запрос — холодный старт. Повторные встречи того же nm_id позволяют
использовать дельты отзывов/остатков вместо оценки по отзывам за всё время.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy.orm import Session

from app.models import MarketSnapshot
from app.schemas import AnalogSku
from app.settings import settings

FEEDBACK_TO_SALES = 20.0


@dataclass(frozen=True)
class SnapshotMetrics:
    matched_count: int = 0
    observation_days: float = 0.0
    median_feedback_sales_30d: float | None = None
    median_stock_delta_30d: float | None = None
    trend_score: float = 0.5
    velocity_source: str = "cold_start"  # холодный старт | дельта снимков


def build_snapshot_metrics(db: Session, items: list[AnalogSku], now: datetime | None = None) -> SnapshotMetrics:
    now = now or datetime.now(timezone.utc)
    if not items:
        return SnapshotMetrics()

    feedback_velocities: list[float] = []
    stock_velocities: list[float] = []
    observation_days: list[float] = []

    for item in _dedupe(items):
        prev = (
            db.query(MarketSnapshot)
            .filter(MarketSnapshot.nm_id == item.nm_id, MarketSnapshot.captured_at < now)
            .order_by(MarketSnapshot.captured_at.desc())
            .first()
        )
        if prev is None:
            continue
        days = _days_between(now, prev.captured_at)
        if days < settings.snapshot_min_days_for_velocity:
            continue
        observation_days.append(days)

        feedback_delta = item.feedbacks - int(prev.feedbacks or 0)
        if feedback_delta >= 0:
            feedback_velocities.append((feedback_delta / days) * 30.4 * FEEDBACK_TO_SALES)

        if item.stocks is not None and prev.stocks is not None:
            stock_delta = int(prev.stocks) - int(item.stocks)
            if stock_delta > 0:
                stock_velocities.append((stock_delta / days) * 30.4)

    if not observation_days:
        return SnapshotMetrics()

    median_feedback = statistics.median(feedback_velocities) if feedback_velocities else None
    median_stock = statistics.median(stock_velocities) if stock_velocities else None
    proxy_median = statistics.median(i.sales_30d_est for i in items if i.sales_30d_est > 0) if items else 0.0
    trend = _trend_score(median_feedback, proxy_median)

    return SnapshotMetrics(
        matched_count=len(observation_days),
        observation_days=round(max(observation_days), 2),
        median_feedback_sales_30d=round(float(median_feedback), 1) if median_feedback is not None else None,
        median_stock_delta_30d=round(float(median_stock), 1) if median_stock is not None else None,
        trend_score=trend,
        velocity_source="snapshot_delta",
    )


def record_market_snapshots(
    db: Session,
    items: Iterable[AnalogSku],
    source: str,
    *,
    subject_id: int | None = None,
    subject_name: str | None = None,
    parent_name: str | None = None,
    now: datetime | None = None,
) -> int:
    now = now or datetime.now(timezone.utc)
    min_gap = timedelta(hours=settings.snapshot_min_interval_hours)
    inserted = 0

    for item in _dedupe(list(items)):
        latest = (
            db.query(MarketSnapshot)
            .filter(MarketSnapshot.nm_id == item.nm_id, MarketSnapshot.source == source)
            .order_by(MarketSnapshot.captured_at.desc())
            .first()
        )
        if latest is not None and now - _as_utc(latest.captured_at) < min_gap:
            continue
        db.add(MarketSnapshot(
            captured_at=now,
            nm_id=item.nm_id,
            source=source,
            subject_id=subject_id,
            subject_name=subject_name,
            parent_name=parent_name,
            name=item.name,
            brand=item.brand,
            price=item.price,
            sale_price=item.sale_price,
            feedbacks=item.feedbacks,
            rating=item.rating,
            stocks=item.stocks,
            sales_30d_est=item.sales_30d_est,
        ))
        inserted += 1
    return inserted


def _dedupe(items: list[AnalogSku]) -> list[AnalogSku]:
    out: dict[int, AnalogSku] = {}
    for item in items:
        out[item.nm_id] = item
    return list(out.values())


def _days_between(now: datetime, then: datetime) -> float:
    return max(0.0, (now - _as_utc(then)).total_seconds() / 86_400)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _trend_score(snapshot_velocity: float | None, proxy_velocity: float) -> float:
    if snapshot_velocity is None or proxy_velocity <= 0:
        return 0.5
    ratio = snapshot_velocity / proxy_velocity
    if ratio >= 1.2:
        return 0.85
    if ratio >= 0.8:
        return 0.65
    if ratio >= 0.4:
        return 0.45
    return 0.2
