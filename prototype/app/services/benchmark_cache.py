"""Кэш эталона ниши (топ-N по WB-категории).

Живой запрос читает топ отсюда вместо скрапинга catalog.wb.ru в реальном времени:
  • быстрее (топ — самый дорогой сетевой шаг после визуального поиска);
  • выборка стабильная и ПОЛНАЯ — не зависит от того, сколько карточек успели
    собрать за бюджет времени (иначе рейтинг плавал бы от полноты скрапа).

Заполняется фоновым индексатором scripts/build_category_benchmark.py.
Движок только ЧИТАЕТ — кэш-промах безопасно падает в живой скрапинг.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models import CategoryBenchmark
from app.schemas import AnalogSku


def get_cached_benchmark(
    db: Session, subject_id: int | None, max_age_hours: float
) -> list[AnalogSku] | None:
    """Свежий кэшированный топ ниши, или None (промах/протух/битый)."""
    if not subject_id:
        return None
    row = db.query(CategoryBenchmark).filter_by(wb_subject_id=int(subject_id)).first()
    if not row or not row.items_json:
        return None
    captured = row.captured_at
    if captured.tzinfo is None:
        captured = captured.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) - captured > timedelta(hours=max_age_hours):
        return None
    try:
        from app.services.providers.wb_public import _wb_image_url

        items = [AnalogSku(**d) for d in row.items_json]
        for item in items:
            if not item.image:
                item.image = _wb_image_url(item.nm_id)
        return items
    except Exception:  # noqa: BLE001 — битый кэш не должен ронять lookup
        return None


def store_benchmark(
    db: Session, subject_id: int | None, wb_path: str | None, items: list[AnalogSku]
) -> int:
    """Перезаписать кэш топа для WB-категории. Коммитит сам (вызывается индексатором)."""
    if not subject_id:
        return 0
    db.query(CategoryBenchmark).filter_by(wb_subject_id=int(subject_id)).delete()
    db.add(CategoryBenchmark(
        wb_subject_id=int(subject_id),
        wb_path=(wb_path or "")[:512],
        item_count=len(items),
        items_json=[a.model_dump() for a in items],
    ))
    db.commit()
    return len(items)
