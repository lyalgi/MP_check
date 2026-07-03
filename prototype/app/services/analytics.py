"""Фабрика провайдера аналитики."""
from __future__ import annotations

from app.services.providers.base import AnalyticsProvider
from app.services.providers.mock import MockProvider
from app.services.providers.wb_public import WBPublicProvider
from app.settings import settings


def get_analytics_provider() -> AnalyticsProvider:
    if settings.analytics_provider == "mock":
        return MockProvider()
    return WBPublicProvider()
