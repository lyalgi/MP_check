"""Поиск внутренней истории продаж Разноторга.

WB отвечает на вопрос «есть ли внешний спрос на маркетплейсе». Эти функции
отвечают на отдельный полевой вопрос: продавал ли сам Разноторг похожий вид
без зависших остатков и отрицательной рентабельности.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models import RetailHistoryItem, TaxonomyItem
from app.services.reason_codes import ReasonCode


@dataclass(frozen=True)
class RetailCategoryProfile:
    match_kind: str
    matched_vid_count: int
    item_count: int
    sales_year: float
    sales_prev_year: float
    stock: float
    sell_through: float | None
    yoy_ratio: float | None
    median_price: float | None
    median_markup: float | None
    median_profitability: float | None
    source_files: list[str]


_STEM_LEN = 6


def lookup_retail_profile(
    db: Session,
    *,
    wb_path: str | None = None,
    wb_subject_name: str | None = None,
) -> RetailCategoryProfile | None:
    """Найти внутреннюю историю продаж для WB-категории.

    Идём через TaxonomyItem: это мост между WB-путями и собственной таксономией
    Разноторга (группа/подгруппа/вид). Сопоставление намеренно консервативное:
    сначала точный WB-путь, затем совпадение последнего сегмента, затем короткая
    основа слова для единственного/множественного числа русских категорий.
    """
    target = (wb_path or wb_subject_name or "").strip()
    if not target:
        return None
    target_last = _last_segment(target)
    if not target_last:
        return None

    rows = db.query(TaxonomyItem).all()
    buckets: dict[str, list[TaxonomyItem]] = {"exact": [], "suffix": [], "stem": []}
    for row in rows:
        kind = _match_kind(row.wb_paths or [], target, target_last)
        if kind:
            buckets[kind].append(row)

    match_kind = next((k for k in ("exact", "suffix", "stem") if buckets[k]), None)
    if match_kind is None:
        return _lookup_direct_retail_history(db, target_last)

    keys = sorted({(r.group, r.subgroup, r.vid) for r in buckets[match_kind]})
    history: list[RetailHistoryItem] = []
    for group, subgroup, vid in keys:
        history.extend(
            db.query(RetailHistoryItem)
            .filter_by(group=group, subgroup=subgroup, vid=vid)
            .all()
        )
    return _profile_from_history(match_kind, history, matched_vid_count=len(keys))


def retail_profile_reason_codes(profile: RetailCategoryProfile | None) -> list[str]:
    if profile is None:
        return [ReasonCode.NO_RETAIL_HISTORY.value]

    reasons: list[str] = []
    stock_months = None
    if profile.sales_year > 0:
        stock_months = profile.stock / (profile.sales_year / 12.0)
    if (
        profile.sell_through is not None
        and profile.sell_through < 0.2
        and profile.stock > max(10.0, profile.sales_year)
    ) or (
        stock_months is not None
        and stock_months > 18
        and profile.stock >= 50
    ) or (
        profile.sales_year <= 0
        and profile.stock >= 10
    ):
        reasons.append(ReasonCode.OFFLINE_OVERSTOCK.value)
    if profile.yoy_ratio is not None and profile.yoy_ratio < 0.55 and profile.sales_prev_year >= 10:
        reasons.append(ReasonCode.OFFLINE_DECLINING.value)
    if profile.median_profitability is not None and profile.median_profitability < 5:
        reasons.append(ReasonCode.LOW_OFFLINE_PROFITABILITY.value)
    if not reasons:
        reasons.append(ReasonCode.RETAIL_HISTORY_OK.value)
    return reasons


def _match_kind(paths: list[str], target: str, target_last: str) -> str | None:
    norm_target = _norm_path(target)
    norm_last = _norm_path(target_last)
    stem = _stem(norm_last)
    for path in paths:
        if _norm_path(path) == norm_target:
            return "exact"
    for path in paths:
        if _norm_path(_last_segment(path)) == norm_last:
            return "suffix"
    if stem:
        for path in paths:
            path_last = _norm_path(_last_segment(path))
            if path_last.startswith(stem) or stem.startswith(path_last[:_STEM_LEN]):
                return "stem"
    return None


def _lookup_direct_retail_history(db: Session, target_last: str) -> RetailCategoryProfile | None:
    """Резервный поиск, если у внутреннего вида в классификаторе нет WB-пути.

    В некоторых переданных реестрах уже есть полезные факты, но строка
    классификатора содержит только Ozon-пути или вообще не содержит пути
    маркетплейса. Чистый поиск по WB-пути тогда скрыл бы данные. Используем это
    только после провала exact/suffix/stem-сопоставления по таксономии и
    выставляем `match_kind=retail_*`, чтобы в ответе было видно: уверенность ниже.
    """
    norm_last = _norm_path(target_last)
    stem = _stem(norm_last)
    if not stem:
        return None

    rows = db.query(RetailHistoryItem).all()
    suffix = [r for r in rows if _norm_path(r.vid) == norm_last]
    if suffix:
        vids = {(r.group, r.subgroup, r.vid) for r in suffix}
        return _profile_from_history("retail_suffix", suffix, matched_vid_count=len(vids))

    stemmed = []
    for r in rows:
        vid = _norm_path(r.vid)
        if vid.startswith(stem) or stem.startswith(vid[:_STEM_LEN]):
            stemmed.append(r)
    if not stemmed:
        return None
    vids = {(r.group, r.subgroup, r.vid) for r in stemmed}
    return _profile_from_history("retail_stem", stemmed, matched_vid_count=len(vids))


def _profile_from_history(
    match_kind: str,
    history: list[RetailHistoryItem],
    *,
    matched_vid_count: int,
) -> RetailCategoryProfile | None:
    if not history:
        return None

    sales_year = float(sum(x.sales_current or 0.0 for x in history))
    sales_prev_year = float(sum(x.sales_prev or 0.0 for x in history))
    stock = float(sum(x.stock_current or 0.0 for x in history))
    sell_through = sales_year / (sales_year + stock) if sales_year + stock > 0 else None
    yoy_ratio = sales_year / sales_prev_year if sales_prev_year > 0 else None
    prices = [x.price for x in history if x.price is not None and x.price > 0]
    markups = [x.markup for x in history if x.markup is not None]
    profits = [x.profitability_current for x in history if x.profitability_current is not None]
    return RetailCategoryProfile(
        match_kind=match_kind,
        matched_vid_count=matched_vid_count,
        item_count=len(history),
        sales_year=round(sales_year, 2),
        sales_prev_year=round(sales_prev_year, 2),
        stock=round(stock, 2),
        sell_through=round(sell_through, 4) if sell_through is not None else None,
        yoy_ratio=round(yoy_ratio, 4) if yoy_ratio is not None else None,
        median_price=_median(prices),
        median_markup=_median(markups),
        median_profitability=_median(profits),
        source_files=sorted({x.source_file for x in history})[:5],
    )


def _last_segment(path: str) -> str:
    return (path or "").rstrip("/").split("/")[-1].strip()


def _norm_path(value: str) -> str:
    return " ".join((value or "").strip().casefold().split())


def _stem(value: str) -> str:
    return _norm_path(value)[:_STEM_LEN]


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    return round(float(statistics.median(values)), 4)
