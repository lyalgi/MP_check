"""Сервис справочника Разноторга (Группа→Подгруппа→Вид + WB/OZON-маппинг)."""
from __future__ import annotations

from typing import Iterable

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import TaxonomyItem


def list_groups(db: Session) -> list[dict]:
    rows = (
        db.query(
            TaxonomyItem.group,
            func.count(func.distinct(TaxonomyItem.subgroup)).label("subg"),
            func.count(TaxonomyItem.id).label("vids"),
        )
        .group_by(TaxonomyItem.group)
        .order_by(TaxonomyItem.group)
        .all()
    )
    return [{"group": g, "subgroup_count": s, "vid_count": v} for g, s, v in rows]


def list_subgroups(db: Session, group: str) -> list[dict]:
    rows = (
        db.query(TaxonomyItem.subgroup, func.count(TaxonomyItem.id))
        .filter(TaxonomyItem.group == group)
        .group_by(TaxonomyItem.subgroup)
        .order_by(TaxonomyItem.subgroup)
        .all()
    )
    return [{"subgroup": s, "vid_count": v} for s, v in rows]


def list_vids(db: Session, group: str, subgroup: str) -> list[dict]:
    rows = (
        db.query(TaxonomyItem)
        .filter(TaxonomyItem.group == group, TaxonomyItem.subgroup == subgroup)
        .order_by(TaxonomyItem.vid)
        .all()
    )
    return [
        {"vid": r.vid, "tovaroved": r.tovaroved, "wb_paths": r.wb_paths or []}
        for r in rows
    ]


def find_item(db: Session, group: str, subgroup: str, vid: str) -> TaxonomyItem | None:
    return (
        db.query(TaxonomyItem)
        .filter(
            TaxonomyItem.group == group,
            TaxonomyItem.subgroup == subgroup,
            TaxonomyItem.vid == vid,
        )
        .one_or_none()
    )


def search_vids(db: Session, q: str, limit: int = 20) -> list[TaxonomyItem]:
    q = (q or "").strip().lower()
    if not q:
        return []
    pattern = f"%{q}%"
    return (
        db.query(TaxonomyItem)
        .filter(
            (func.lower(TaxonomyItem.vid).like(pattern))
            | (func.lower(TaxonomyItem.subgroup).like(pattern))
            | (func.lower(TaxonomyItem.group).like(pattern))
        )
        .order_by(TaxonomyItem.vid)
        .limit(limit)
        .all()
    )


def all_items_iter(db: Session) -> Iterable[TaxonomyItem]:
    return db.query(TaxonomyItem).yield_per(500)
