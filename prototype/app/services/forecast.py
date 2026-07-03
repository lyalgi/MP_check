"""Справочный коэффициент «доля Разноторга от рынка».

Устаревшая формула, которую больше не используем для количества закупа новинок:
    K = выручка_Разноторга_в_категории_год / выручка_маркетплейса_в_категории_год
    forecast_units_year = median(analogs.sales_30d_est) * 12 * K
    recommended_units_year = round(forecast_units_year)

K тянем из таблицы
`category_coefficient`, которую заполняет scripts/build_category_coefficient.py
из (a) выручки Разноторга по xlsx-классификаторам и (b) ежегодной выручки
WB-категорий из xlsx 'ВБ для ИИ выручка год'.

В боевом пайплайне K, выручка и базовая скорость Разноторга показываются справочно:
новый товар получает только тестовую партию по цене входа и вердикту.
Никаких эвристик «средний K» — это бы дало ложные прогнозы.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models import CategoryCoefficient


@dataclass(frozen=True)
class CategoryForecast:
    wb_path: str
    coefficient: float                # K = razno_rub/market_rub (справочно)
    raznotorg_revenue_year: float
    marketplace_revenue_year: float
    match_kind: str                   # 'exact' | 'suffix' | 'stem'
    raznotorg_units_year: float = 0.0       # продажи Разноторга в категории, шт/год
    raznotorg_positions: int = 0            # число позиций (видов) Разноторга

    @property
    def baseline_units_per_position(self) -> float | None:
        """Средняя скорость продаж позиции в категории (справка, не закуп новинки)."""
        if self.raznotorg_positions > 0 and self.raznotorg_units_year > 0:
            return self.raznotorg_units_year / self.raznotorg_positions
        return None


_STEM_LEN = 6


def _stem(s: str) -> str:
    """Грубый корень слова: нижний регистр + первые 6 символов.
    «Конструкторы LEGO» → «констр», «Бейсболки» → «бейсбо», «Конструктор» → «констр».
    Достаточно чтобы матчить морфологические варианты без NLP."""
    return (s or "").strip().lower()[:_STEM_LEN]


def lookup_coefficient(db: Session, wb_path_or_subject: str | None) -> CategoryForecast | None:
    """Поиск K по WB-категории.

    Стратегии:
      1) точное совпадение по полному пути;
      2) последний сегмент запроса буквально равен последнему сегменту в БД;
      3) совпадение по корню слова (первые 6 символов в нижнем регистре).
         Это покрывает «Конструкторы LEGO» ↔ «Конструктор», «Бейсболки» ↔ «Бейсболка».

    Подъём к родительскому префиксу НЕ используем: «Аксессуары/Рюкзаки» не должен наследовать K
    «Аксессуары/Головные уборы/Бейсболка» — это семантически разные ниши.
    """
    if not wb_path_or_subject:
        return None

    # 1) exact
    row = (
        db.query(CategoryCoefficient)
        .filter(CategoryCoefficient.wb_path == wb_path_or_subject)
        .first()
    )
    if row:
        return _to_forecast(row, "exact")

    last_segment = wb_path_or_subject.rstrip("/").split("/")[-1].strip()
    if not last_segment:
        return None

    # 2) последний сегмент == последнему сегменту в БД (без учёта регистра)
    target_lower = last_segment.lower()
    all_rows = db.query(CategoryCoefficient).all()
    for r in all_rows:
        if r.wb_path.split("/")[-1].strip().lower() == target_lower:
            return _to_forecast(r, "suffix")

    # 3) совпадение по корню (первые 6 символов в нижнем регистре)
    stem = _stem(last_segment)
    if stem:
        candidates: list[tuple[CategoryCoefficient, int]] = []
        for r in all_rows:
            r_last = r.wb_path.split("/")[-1].strip().lower()
            if r_last.startswith(stem) or stem.startswith(r_last[:_STEM_LEN]):
                # длина пересечения как ранг (чем больше — тем точнее)
                common = 0
                for a, b in zip(r_last, last_segment.lower()):
                    if a == b:
                        common += 1
                    else:
                        break
                candidates.append((r, common))
        if candidates:
            # выбираем самый «совпадающий» по длине общего префикса,
            # при равенстве — по выручке Разноторга
            best = max(candidates, key=lambda x: (x[1], x[0].raznotorg_revenue_year))[0]
            return _to_forecast(best, "stem")

    return None


def _to_forecast(row: CategoryCoefficient, match_kind: str) -> CategoryForecast:
    return CategoryForecast(
        wb_path=row.wb_path,
        coefficient=row.coefficient,
        raznotorg_revenue_year=row.raznotorg_revenue_year,
        marketplace_revenue_year=row.marketplace_revenue_year,
        match_kind=match_kind,
        raznotorg_units_year=row.raznotorg_units_year or 0.0,
        raznotorg_positions=row.raznotorg_positions or 0,
    )


def forecast_units_year(median_sales_30d: float, coefficient: float) -> float:
    """Медиана продаж аналога в месяц на маркетплейсе × 12 мес × K = прогноз для Разноторга в штуках/год."""
    if median_sales_30d <= 0 or coefficient <= 0:
        return 0.0
    return median_sales_30d * 12.0 * coefficient
