"""Построить статическую таблицу WB↔OZON из классификаторов Разноторга.

Берём все TaxonomyItem с непустыми wb_paths И непустыми ozon_paths;
картезианское произведение даёт кандидатов в пары WB↔OZON.
Уникальный индекс (wb_path, ozon_path) гарантирует дедуп.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import SessionLocal, init_db  # noqa: E402
from app.models import TaxonomyItem, WbOzonMapping  # noqa: E402


def main():
    init_db()
    pairs: set[tuple[str, str]] = set()

    with SessionLocal() as db:
        items = (
            db.query(TaxonomyItem)
            .filter(TaxonomyItem.wb_paths != [], TaxonomyItem.ozon_paths != [])
            .all()
        )
        for it in items:
            for w in it.wb_paths or []:
                for o in it.ozon_paths or []:
                    if not isinstance(w, str) or not isinstance(o, str):
                        continue
                    w_s, o_s = w.strip(), o.strip()
                    if not w_s or not o_s or w_s == "0" or o_s == "0":
                        continue
                    pairs.add((w_s, o_s))

        inserted = 0
        skipped = 0
        for wb_p, oz_p in sorted(pairs):
            exists = (
                db.query(WbOzonMapping)
                .filter(WbOzonMapping.wb_path == wb_p, WbOzonMapping.ozon_path == oz_p)
                .one_or_none()
            )
            if exists:
                skipped += 1
                continue
            db.add(WbOzonMapping(wb_path=wb_p, ozon_path=oz_p, source="taxonomy_xlsx"))
            inserted += 1
        db.commit()

    print(f"пары собрано (unique): {len(pairs)}")
    print(f"вставлено: {inserted}, уже было: {skipped}")


if __name__ == "__main__":
    main()
