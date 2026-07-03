"""Коды причин вердикта (для интерфейса, отладки и аналитики).

Каждый код объясняет, почему пайплайн пришёл к UNKNOWN/RED/YELLOW/GREEN.
Хранятся как обычные строки в LookupResponse.verdict_reasons.
"""
from __future__ import annotations

from enum import Enum


class ReasonCode(str, Enum):
    # шаги пайплайна не дали достаточно данных
    NO_VISUAL = "NO_VISUAL"                  # визуальный поиск вернул 0 SKU
    LOW_FEEDBACKS = "LOW_FEEDBACKS"          # все аналоги отфильтрованы по min_feedbacks
    WB_DETAIL_UNAVAILABLE = "WB_DETAIL_UNAVAILABLE"  # card.wb.ru не отдал детали
    NO_BENCHMARK = "NO_BENCHMARK"            # топ-30 пустой (нет данных по нише)
    DEAD_NICHE = "DEAD_NICHE"                # топ-30 есть, но продажи в нём 0
    CATEGORY_UNRESOLVED = "CATEGORY_UNRESOLVED"  # не удалось устойчиво определить WB subject
    HETEROGENEOUS_SUBJECTS = "HETEROGENEOUS_SUBJECTS"  # ни одна subject не доминирует ≥60% спроса
    WB_ONLY = "WB_ONLY"                      # OZON-аналитика недоступна (нет mapping)
    HEURISTIC_BENCHMARK = "HEURISTIC_BENCHMARK"  # топ ниши заменён эвристикой по визуальной выдаче
    VERDICT_CAPPED = "VERDICT_CAPPED"        # вердикт понижен из-за неполных данных
    SLOW_LOOKUP = "SLOW_LOOKUP"              # запрос вышел за полевой SLA
    SNAPSHOT_COLD_START = "SNAPSHOT_COLD_START"  # ещё нет истории для скорости по дельтам
    SNAPSHOT_VELOCITY = "SNAPSHOT_VELOCITY"  # скорость считалась по дельте снимков
    LOW_MARGIN = "LOW_MARGIN"                # закупочная цена слишком близка к рынку
    HIGH_STOCK_PRESSURE = "HIGH_STOCK_PRESSURE"  # у аналогов избыток остатков относительно спроса
    TOP_HEAVY_CATEGORY = "TOP_HEAVY_CATEGORY"    # спрос сконцентрирован у топовых карточек
    LOW_DATA_QUALITY = "LOW_DATA_QUALITY"    # мало аналогов/эталона для уверенного решения
    LOW_SAMPLE = "LOW_SAMPLE"                # выборка тонкая/усечённая — GREEN не даём (защита от смещения по размеру)
    DECLINING_TREND = "DECLINING_TREND"      # скорость по снимкам ниже базового прокси

    # положительные сигналы (для GREEN)
    HIGH_SKU_DEMAND = "HIGH_SKU_DEMAND"      # медианный аналог продаётся как лидер ниши
    HIGH_NICHE_VOLUME = "HIGH_NICHE_VOLUME"  # ниша достаточно большая

    # средние сигналы
    MODERATE_DEMAND = "MODERATE_DEMAND"
    LOW_SKU_DEMAND = "LOW_SKU_DEMAND"

    # прогноз закупа
    NO_RAZNOTORG_HISTORY = "NO_RAZNOTORG_HISTORY"  # нет данных по выручке Разноторга в этой категории
    FORECAST_OK = "FORECAST_OK"                    # прогноз посчитан
    NO_RETAIL_HISTORY = "NO_RETAIL_HISTORY"        # нет внутренних товарных строк по виду Разноторга
    RETAIL_HISTORY_OK = "RETAIL_HISTORY_OK"        # внутренняя история найдена и не конфликтует с WB
    OFFLINE_OVERSTOCK = "OFFLINE_OVERSTOCK"        # в Разноторге похожий вид лежит в остатках
    OFFLINE_DECLINING = "OFFLINE_DECLINING"        # продажи вида упали год к году
    LOW_OFFLINE_PROFITABILITY = "LOW_OFFLINE_PROFITABILITY"  # низкая/отрицательная рентабельность в сети

    def __str__(self) -> str:
        return self.value
