"""Фоновый индексатор бенчмарка: топ-N по WB-категориям → таблица category_benchmark.

Зачем: живой lookup читает кэш вместо live-скрапа catalog.wb.ru. Это (а) быстрее
и (б) делает выборку топа стабильной и полной — рейтинг перестаёт зависеть от
того, сколько карточек успели собрать за бюджет времени.

⚠️ Это массовый обход WB. Для --all нужны прокси (PROXY_URL в .env), иначе WB
быстро начнёт возвращать 429/403. Небольшой --from-history можно гонять и без прокси.

    python scripts/build_category_benchmark.py                 # из истории запросов (default)
    python scripts/build_category_benchmark.py --subjects 1859,629 --sleep 1.5
    python scripts/build_category_benchmark.py --all --sleep 2  # все WB-категории (нужны прокси)
"""
from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import SessionLocal, init_db  # noqa: E402
from app.models import LookupHistory, WbSubject  # noqa: E402
from app.services.analytics import get_analytics_provider  # noqa: E402
from app.services.benchmark_cache import store_benchmark  # noqa: E402


def _resolve_subjects(db, args) -> list[tuple[int, str, str | None, str]]:
    subs = db.query(WbSubject).all()
    name_by_id = {s.id: s.name for s in subs}
    parent_by_id = {s.id: s.parent_id for s in subs}

    if args.subjects:
        ids = [int(x) for x in args.subjects.split(",") if x.strip()]
    elif args.all:
        ids = list(name_by_id.keys())
    else:  # из реальной истории запросов (по умолчанию)
        rows = (
            db.query(LookupHistory.wb_subject_id)
            .filter(LookupHistory.wb_subject_id.isnot(None))
            .distinct()
            .all()
        )
        ids = [r[0] for r in rows if r[0]]

    if args.limit:
        ids = ids[: args.limit]

    out: list[tuple[int, str, str | None, str]] = []
    for sid in ids:
        name = name_by_id.get(sid)
        if not name:
            continue
        pname = name_by_id.get(parent_by_id.get(sid))
        path = f"{pname}/{name}" if pname else name
        out.append((sid, name, pname, path))
    return out


async def _run(db, subjects: list[tuple[int, str, str | None, str]], sleep: float) -> None:
    provider = get_analytics_provider()
    ok = empty = err = 0
    n = len(subjects)
    for i, (sid, name, pname, path) in enumerate(subjects, 1):
        try:
            top = await provider.top_n_by_subject(sid, limit=100, parent_name=pname)
            top = [t for t in top if t.feedbacks > 0]
        except Exception as e:  # noqa: BLE001
            err += 1
            print(f"  [{i}/{n}] {sid} {name}: ERR {type(e).__name__}: {e}")
            await asyncio.sleep(sleep)
            continue
        if top:
            store_benchmark(db, sid, path, top)
            med = statistics.median(t.sales_30d_est for t in top)
            ok += 1
            print(f"  [{i}/{n}] {sid} {name}: {len(top)} карточек, медиана {med:.0f}/мес")
        else:
            empty += 1
            print(f"  [{i}/{n}] {sid} {name}: пусто")
        await asyncio.sleep(sleep)
    print(f"\nИтого: записано {ok}, пусто {empty}, ошибок {err}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Индексатор бенчмарка топ-N по WB-категориям")
    ap.add_argument("--from-history", action="store_true", help="категории из истории запросов (default)")
    ap.add_argument("--all", action="store_true", help="ВСЕ WB-категории (bulk, нужны прокси)")
    ap.add_argument("--subjects", default="", help="явный список subject_id через запятую")
    ap.add_argument("--limit", type=int, default=0, help="ограничить число категорий")
    ap.add_argument("--sleep", type=float, default=1.0, help="пауза между категориями, сек")
    args = ap.parse_args()

    init_db()
    with SessionLocal() as db:
        subjects = _resolve_subjects(db, args)
        print(f"Категорий к индексации: {len(subjects)} · пауза {args.sleep}s")
        if not subjects:
            print("Нечего индексировать (пустая история? укажи --subjects или --all)")
            return 0
        asyncio.run(_run(db, subjects, args.sleep))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
