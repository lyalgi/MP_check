"""Мок-провайдер для офлайн-тестов."""
from __future__ import annotations

from app.schemas import AnalogSku


def _mk(nm: int, name: str, price: float, feedbacks: int) -> AnalogSku:
    return AnalogSku(
        nm_id=nm,
        name=name,
        brand="MockBrand",
        price=price,
        sale_price=price * 0.9,
        feedbacks=feedbacks,
        rating=4.6,
        stocks=feedbacks * 3,
        sales_30d_est=float(feedbacks),
        url=f"https://www.wildberries.ru/catalog/{nm}/detail.aspx",
    )


class MockProvider:
    """Возвращает детерминированные «продажи» для проверки рейтинга.

    По договорённости в тестах:
      - top-30 для категории X дают суммарно sales = 3000 / cnt → средняя 100
      - аналоги для query Y дают суммарно sales = 600 → 600 за всех

    => rating = 600 / 100 = 6.0 → GREEN (но это нормированная сравнительная схема).
    Сам тест проверит формулу с конкретными числами через DI.
    """

    def __init__(
        self,
        analogs_sales: list[float] | None = None,
        top_sales: list[float] | None = None,
    ):
        self.analogs_sales = analogs_sales or [10, 20, 30]
        self.top_sales = top_sales or [100, 100, 100]

    async def search_analogs(self, query, wb_category_path=None, price_min=None, price_max=None, limit=30):
        return [_mk(1000 + i, f"analog {i}", 999.0, int(s)) for i, s in enumerate(self.analogs_sales[:limit])]

    async def top_n_in_category(self, wb_category_path, limit=30, *, subject_id=None):
        return [_mk(9000 + i, f"top {i}", 1999.0, int(s)) for i, s in enumerate(self.top_sales[:limit])]

    async def top_n_by_subject(self, subject_id, limit=30, *, parent_name=None):
        return await self.top_n_in_category(str(subject_id), limit=limit)
