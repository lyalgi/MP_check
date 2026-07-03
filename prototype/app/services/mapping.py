"""Маппинг категорий WB ↔ OZON (см. ТЗ, шаг 4).

Статическая таблица. Источник: пары WB-путь / OZON-путь, которые товароведы
Разноторга уже проставили в xlsx-классификаторах. Скрипт
`scripts/build_marketplace_mapping.py` импортирует пары в таблицу
`wb_ozon_mapping`.

Lookup-стратегия:
    1. Точное совпадение WB-пути (case-insensitive).
    2. Совпадение subjectName: ищем wb_path, у которого ПОСЛЕДНИЙ сегмент
       (часть после последнего «/») = subjectName. Это покрывает случай, когда
       мы пришли с card.wb.ru и знаем только subjectName, но не полный путь.
    3. Префиксный резервный путь: если есть «Детям/Для мальчиков/Белье», берём
       любое OZON-совпадение с самым длинным общим префиксом.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import WbOzonMapping

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MappingResult:
    wb_path: str
    ozon_path: str | None
    match_kind: str  # exact | by_subject | by_subject_stem | prefix | none


def _norm(s: str) -> str:
    return (s or "").strip().lower()


def _last_segment(path: str) -> str:
    return (path or "").rstrip("/").split("/")[-1].strip()


def _stem(s: str) -> str:
    s = _norm(s)
    stem_len = min(6, max(4, len(s) - 1))
    return s[:stem_len]


def _best_row(rows: list[WbOzonMapping]) -> WbOzonMapping | None:
    if not rows:
        return None
    # В классификаторах рядом с точным OZON-путём часто лежат родительские
    # значения ("Детям", "Канцтовары"). Для решения закупщика нужен самый
    # конкретный путь, иначе система выглядит как будто OZON не знает категорию.
    return max(rows, key=lambda r: (r.ozon_path.count("/"), len(r.ozon_path), len(r.wb_path)))


def _best_subject_row(rows: list[WbOzonMapping], subject_name: str) -> WbOzonMapping | None:
    if not rows:
        return None
    target = _norm(subject_name)
    target_stem = _stem(subject_name)

    def rank(row: WbOzonMapping) -> tuple:
        last = _norm(_last_segment(row.wb_path))
        word_count = len([w for w in last.split() if w])
        exact_last = int(last == target)
        stem_ok = int(last.startswith(target_stem) or target_stem.startswith(_stem(last)))
        return (
            exact_last,
            stem_ok,
            -word_count,  # "Конструктор" лучше, чем "Конструктор LEGO"
            row.ozon_path.count("/"),
            len(row.ozon_path),
        )

    return max(rows, key=rank)


def find_mapping(db: Session, wb_path: str | None, subject_name: str | None = None) -> MappingResult:
    """Lookup case-insensitive — SQLite lower() переопределён в app/db.py
    как Python str.lower(), чтобы корректно работать с кириллицей."""
    if not wb_path and not subject_name:
        return MappingResult(wb_path=wb_path or "", ozon_path=None, match_kind="none")

    # 1) точное совпадение (без учёта регистра через unicode-aware lower)
    if wb_path:
        target = _norm(wb_path)
        rows = (
            db.query(WbOzonMapping)
            .filter(func.lower(WbOzonMapping.wb_path) == target)
            .all()
        )
        row = _best_row(rows)
        if row:
            return MappingResult(wb_path=row.wb_path, ozon_path=row.ozon_path, match_kind="exact")

    # 2) совпадение по subjectName (последний сегмент пути)
    if subject_name:
        like = f"%/{_norm(subject_name)}"
        rows = (
            db.query(WbOzonMapping)
            .filter(func.lower(WbOzonMapping.wb_path).like(like))
            .all()
        )
        row = _best_subject_row(rows, subject_name)
        if row:
            return MappingResult(wb_path=row.wb_path, ozon_path=row.ozon_path, match_kind="by_subject")

        # Единственное/множественное число: "Ручки" в card.wb.ru, но "Ручка"
        # в классификаторе. SQLite LIKE тут не поможет.
        stem = _stem(subject_name)
        if stem:
            all_rows = db.query(WbOzonMapping).all()
            rows = [
                r for r in all_rows
                if (
                    _stem(_last_segment(r.wb_path)).startswith(stem)
                    or stem.startswith(_stem(_last_segment(r.wb_path)))
                )
            ]
            row = _best_subject_row(rows, subject_name)
            if row:
                return MappingResult(wb_path=row.wb_path, ozon_path=row.ozon_path, match_kind="by_subject_stem")

    # 3) префиксный резервный путь (постепенно урезаем хвост пути)
    if wb_path:
        path_parts = [p.strip() for p in wb_path.split("/") if p.strip()]
        while len(path_parts) > 2:
            path_parts.pop()
            prefix = "/".join(path_parts)
            rows = (
                db.query(WbOzonMapping)
                .filter(func.lower(WbOzonMapping.wb_path).like(_norm(prefix) + "/%"))
                .all()
            )
            row = _best_row(rows)
            if row:
                return MappingResult(wb_path=row.wb_path, ozon_path=row.ozon_path, match_kind="prefix")

    return MappingResult(wb_path=wb_path or "", ozon_path=None, match_kind="none")


def list_examples(db: Session, limit: int = 30) -> list[WbOzonMapping]:
    return db.query(WbOzonMapping).order_by(WbOzonMapping.wb_path).limit(limit).all()


def count(db: Session) -> int:
    return db.query(func.count(WbOzonMapping.id)).scalar() or 0
