from __future__ import annotations

from typing import Protocol

from app.schemas import AnalogSku


class AnalyticsProvider(Protocol):
    """Контракт провайдера аналитики маркетплейса."""

    async def search_analogs(
        self,
        query: str,
        wb_category_path: str | None,
        price_min: float | None = None,
        price_max: float | None = None,
        limit: int = 30,
    ) -> list[AnalogSku]:
        ...

    async def top_n_in_category(
        self,
        wb_category_path: str,
        limit: int = 30,
    ) -> list[AnalogSku]:
        ...

    async def top_n_by_subject(
        self,
        subject_id: int,
        limit: int = 30,
        *,
        parent_name: str | None = None,
    ) -> list[AnalogSku]:
        ...
