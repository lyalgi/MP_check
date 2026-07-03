"""Категорийная статистика Разноторга по WB-категориям → `category_coefficient`.

На каждую WB-категорию (лист) считаем:
    raznotorg_revenue_year   — Σ sold_rub видов Разноторга в категории
    raznotorg_units_year     — Σ sold_qty (штуки) — для модели «позиция × WB-сила»
    raznotorg_positions      — число видов (позиций) Разноторга в категории
    marketplace_revenue_year — выручка WB по категории (доминирующий путь)
    coefficient              — K = razno_rub / market_rub (для справки/совместимости)

Два прохода матчинга Razno↔WB:
  1) PATH-based: по `taxonomy_items.wb_paths` (там, где вид привязан к WB-пути).
  2) NAME-based: по ИМЕНИ листа (для пустых wb_paths — всё освещение и пр.).
     WB-выручка = доминирующий (max-revenue) путь с этим листом; razno-статистика
     = виды, чьи корни ⊇ корней листа. Строка пишется с доминирующим полным путём,
     чтобы suffix/stem-матч в lookup_coefficient её находил.

Имя-матч доверяем только крупным каноничным категориям (market ≥ FLOOR) с
правдоподобной долей (K ≤ CEIL): высокий K на мелком/криво-названном листе — шум.
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models import (  # noqa: E402
    CategoryCoefficient,
    MarketplaceCategoryRevenue,
    TaxonomyItem,
)
from app.db import SessionLocal, init_db  # noqa: E402
from app.services.name_match import (  # noqa: E402
    build_index,
    leaf_of,
    matched_indices,
    normalize,
    stems,
)

_BAD_TOP = {"общий итог", ""}
MARKET_FLOOR = 50_000_000.0
K_CEIL = 0.03


def _all_prefixes(path: str) -> list[str]:
    parts = [p.strip() for p in path.split("/") if p.strip()]
    return ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]


def _wb_revenue(db) -> dict[str, float]:
    out: dict[str, float] = {}
    for r in db.query(MarketplaceCategoryRevenue).filter_by(marketplace="wb").all():
        out[r.path] = float(r.revenue_year or 0.0)
    return out


def build_path_rows(db, wb_rev: dict[str, float]) -> list[dict]:
    agg: dict[str, dict] = defaultdict(lambda: {"rub": 0.0, "qty": 0.0, "pos": 0})
    for it in db.query(TaxonomyItem).all():
        rub, qty = float(it.sold_rub or 0.0), float(it.sold_qty or 0.0)
        for p in it.wb_paths or []:
            if isinstance(p, str) and p.strip() and p.strip() != "0":
                a = agg[p.strip()]
                a["rub"] += rub
                a["qty"] += qty
                a["pos"] += 1

    rows: list[dict] = []
    exact = prefix = unmatched = 0
    for path, a in agg.items():
        mp = wb_rev.get(path)
        kind = "exact" if mp is not None else None
        if mp is None:
            for pref in reversed(_all_prefixes(path)[:-1]):
                if pref in wb_rev:
                    mp, kind = wb_rev[pref], "prefix"
                    break
        if mp is None or mp <= 0:
            unmatched += 1
            continue
        exact += kind == "exact"
        prefix += kind == "prefix"
        rows.append({
            "wb_path": path,
            "raznotorg_revenue_year": a["rub"],
            "raznotorg_units_year": a["qty"],
            "raznotorg_positions": a["pos"],
            "marketplace_revenue_year": mp,
            "coefficient": a["rub"] / mp,
        })
    print(f"PATH: razno-путей {len(agg)}, WB-путей {len(wb_rev)} → "
          f"exact={exact}, prefix={prefix}, unmatched={unmatched}, rows={len(rows)}")
    return rows


def build_name_rows(db, wb_rev: dict[str, float],
                    taken_leaves: set[str], taken_paths: set[str]) -> list[dict]:
    # доминирующий WB-путь на каждый нормализованный лист
    leaf_best: dict[str, tuple[str, float]] = {}
    for path, rev in wb_rev.items():
        if rev <= 0 or normalize(path.split("/")[0]) in _BAD_TOP:
            continue
        nl = normalize(leaf_of(path))
        if not nl:
            continue
        cur = leaf_best.get(nl)
        if cur is None or rev > cur[1]:
            leaf_best[nl] = (path, rev)

    # индекс видов Разноторга по корням (subgroup + vid), + параллельные rub/qty
    item_stems: list[frozenset[str]] = []
    rub: list[float] = []
    qty: list[float] = []
    for it in db.query(TaxonomyItem).all():
        r = float(it.sold_rub or 0.0)
        if r <= 0:
            continue
        st = stems(it.subgroup) | stems(it.vid)
        if st:
            item_stems.append(st)
            rub.append(r)
            qty.append(float(it.sold_qty or 0.0))
    posting, _ = build_index([(s, 0.0) for s in item_stems])

    rows: list[dict] = []
    rej_small = rej_high = 0
    for nl, (full_path, market_rev) in leaf_best.items():
        if nl in taken_leaves or full_path in taken_paths:
            continue
        if market_rev < MARKET_FLOOR:
            rej_small += 1
            continue
        lst = stems(leaf_of(full_path))
        ids = matched_indices(lst, posting)
        if not ids:
            continue
        razno_rub = sum(rub[i] for i in ids)
        if razno_rub <= 0:
            continue
        k = razno_rub / market_rev
        if k > K_CEIL:
            rej_high += 1
            continue
        rows.append({
            "wb_path": full_path,
            "raznotorg_revenue_year": razno_rub,
            "raznotorg_units_year": sum(qty[i] for i in ids),
            "raznotorg_positions": len(ids),
            "marketplace_revenue_year": market_rev,
            "coefficient": k,
        })
        taken_paths.add(full_path)
    print(f"NAME: WB-листов {len(leaf_best)}, razno-видов {len(item_stems)} → "
          f"rows={len(rows)}; отброшено market<{MARKET_FLOOR/1e6:.0f}млн: {rej_small}, "
          f"K>{K_CEIL*100:.0f}%: {rej_high}")
    return rows


def main():
    init_db()
    with SessionLocal() as db:
        wb_rev = _wb_revenue(db)
        path_rows = build_path_rows(db, wb_rev)
        taken_leaves = {normalize(leaf_of(r["wb_path"])) for r in path_rows}
        taken_paths = {r["wb_path"] for r in path_rows}
        name_rows = build_name_rows(db, wb_rev, taken_leaves, taken_paths)

        all_rows = path_rows + name_rows
        db.query(CategoryCoefficient).delete()
        for row in all_rows:
            db.add(CategoryCoefficient(**row))
        db.commit()
        print(f"\nИТОГО: {len(all_rows)} (path={len(path_rows)}, name={len(name_rows)})")

        light = [r for r in all_rows
                 if any(w in r["wb_path"].lower() for w in ("свет", "люстр", "ламп", "ночник", "освещ"))]
        print(f"\nОсвещение ({len(light)}): baseline = units/позиции")
        for r in sorted(light, key=lambda r: r["raznotorg_units_year"], reverse=True)[:12]:
            pos = r["raznotorg_positions"] or 1
            print(f"  {r['raznotorg_units_year']:>8,.0f}шт /{pos:>3}поз = "
                  f"{r['raznotorg_units_year']/pos:>6.0f}шт/поз  {r['wb_path']}")


if __name__ == "__main__":
    main()
