"""Загрузить справочник WB subjects в таблицу wb_subjects."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import SessionLocal, init_db  # noqa: E402
from app.models import WbSubject  # noqa: E402
from app.services.subjects import fetch_subjects_dict  # noqa: E402


def main():
    init_db()
    items = fetch_subjects_dict()
    print(f"скачано записей: {len(items)}")
    inserted = 0
    updated = 0
    with SessionLocal() as db:
        for it in items:
            sid = it.get("id")
            if not sid:
                continue
            existing = db.get(WbSubject, sid)
            if existing is None:
                db.add(WbSubject(
                    id=sid,
                    name=it.get("name") or "",
                    parent_id=it.get("parentId"),
                    ru_url=it.get("ruUrl"),
                    single_name=it.get("singleName"),
                ))
                inserted += 1
            else:
                existing.name = it.get("name") or existing.name
                existing.parent_id = it.get("parentId") if it.get("parentId") is not None else existing.parent_id
                existing.ru_url = it.get("ruUrl") or existing.ru_url
                existing.single_name = it.get("singleName") or existing.single_name
                updated += 1
        db.commit()
    print(f"insert: {inserted}, update: {updated}")


if __name__ == "__main__":
    main()
