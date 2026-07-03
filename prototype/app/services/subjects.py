"""Справочник WB subjectId → name + parent.

Источник: https://static-basket-01.wbbasket.ru/vol0/data/subjects.json
Возвращает gzip-сжатый JSON (вне зависимости от Accept-Encoding).
Грузим, распаковываем, сохраняем в таблицу wb_subjects. Полная пересинхронизация
— `scripts/import_wb_subjects.py`.
"""
from __future__ import annotations

import gzip
import logging
from dataclasses import dataclass

import requests
from sqlalchemy.orm import Session

from app.models import WbSubject

logger = logging.getLogger(__name__)

SUBJECTS_URL = "https://static-basket-01.wbbasket.ru/vol0/data/subjects.json"


@dataclass(frozen=True)
class SubjectInfo:
    id: int
    name: str
    parent_id: int | None
    parent_name: str | None

    def full_path(self) -> str:
        if self.parent_name:
            return f"{self.parent_name}/{self.name}"
        return self.name


def fetch_subjects_dict() -> list[dict]:
    """Скачать и распаковать subjects.json."""
    import json as _json
    r = requests.get(SUBJECTS_URL, timeout=20)
    r.raise_for_status()
    try:
        return _json.loads(r.content)
    except ValueError:
        return _json.loads(gzip.decompress(r.content))


def lookup(db: Session, subject_id: int | None) -> SubjectInfo | None:
    if not subject_id:
        return None
    s = db.get(WbSubject, subject_id)
    if not s:
        return None
    parent_name = None
    if s.parent_id:
        parent = db.get(WbSubject, s.parent_id)
        parent_name = parent.name if parent else None
    return SubjectInfo(id=s.id, name=s.name, parent_id=s.parent_id, parent_name=parent_name)
