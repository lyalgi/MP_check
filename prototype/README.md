# SAOL — Система автоматизированной оценки ликвидности

Закупщик в опте → фото товара + закупочная цена → за 10–30 сек получает вердикт:
STRONG / GREEN / YELLOW / RED / UNKNOWN + размер тестовой партии + причины.

## Стек
- Python 3.11+, FastAPI, SQLite (SQLAlchemy 2.x)
- HTTP к WB: requests (через ротацию residential-прокси) + httpx (для menu)
- Публичные эндпойнты WB (catalog/cards/subjects) — без платных подписок
- Никаких ML/AI: классификация — алгоритмическая (взвешенное голосование по subjectId)
- Mobile-first веб (vanilla HTML/JS, без service worker — чтобы не залипал кэш)

## Автономный файл алгоритма — `saol_core.py`

Один файл, без БД и веб-приложения. Для аналитиков/разработчиков: запустил — получил
быструю WB-оценку по фото или nm_id. Это устаревшая утилита: актуальная логика живёт в
`app/services/engine.py` и содержит более свежую логику тестовых партий,
снимков, уровня доверия и предохранителей.

```bash
pip install requests cryptography
python saol_core.py photo.jpg --price 350          # по фото товара
python saol_core.py --nm 143489486 --price 350     # по артикулу WB
python saol_core.py photo.jpg --price 350 --json   # машинный вывод
```

Рядом может лежать `data/coefficient.json` (доля Разноторга K по категориям),
но в боевой модели K используется справочно, а не как источник количества закупа.

## Прогноз закупа (сколько брать)
Главный вопрос — востребованность конкретного товара/аналогов на WB. Для новинки
положительный вердикт означает **тест**, а не массовую закупку:
```
STRONG  → расширенный тест
GREEN   → обычный тест
YELLOW  → малый тест
RED     → 0
UNKNOWN → нет числа
```
Размер теста сейчас считается по цене входа: до 100 ₽ — база 50 шт, до 300 ₽ —
20 шт, до 1000 ₽ — 7 шт, дороже — 2 шт; далее применяется множитель вердикта.
История Разноторга, K и остатки похожих видов показываются как диагностика и
могут понизить доверие/вердикт, но не превращаются в годовой объём закупа.

## Алгоритм (ТЗ САОЛ, шаги 1-6)

1. **Визуальный поиск** WB → массив nm_id (провайдер `wb_photo`, `mock` для разработки или `http` с конфигурируемым URL).
2. **Фильтр аналогов** по `feedbacks ≥ min_feedbacks` (по умолчанию 10).
3. **Категория** — взвешенное голосование по `subjectId` среди всех аналогов
   (вес = `feedbacks`). Если ни одна subject не набирает ≥60% — `HETEROGENEOUS_SUBJECTS`.
4. **WB → OZON** — статическая таблица `wb_ozon_mapping` (210+ пар из классификаторов
   Разноторга). Lookup: exact → by-subject → prefix.
5. **Top-30 в категории** — catalog.wb.ru по WB-пути. Если каталог не отдал данные,
   используется резервный путь `visual_subset_in_subject`, но ответ помечается `HEURISTIC_BENCHMARK`.
6. **Два балла**:
   - `sku_demand_score = median(analogs)/median(top)` — насколько типичный аналог
     модели похож по спросу на типичного лидера ниши.
   - `niche_volume_score = Σ(analogs)/Σ(top)` — есть ли в нише денежная ёмкость.
   - `liquidity_score` (0–100) = коммерческий балл из спроса, остатков, маржи,
     конкуренции, тренда и качества данных.

## Запуск локально
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # PROXY_URL/SAOL_VISUAL_SEARCH_URL — опционально
python scripts/import_classifier.py        # ~9k видов Разноторга
python scripts/import_wb_subjects.py        # 8150 WB-категорий
python scripts/build_marketplace_mapping.py # 210+ пар WB↔OZON
python scripts/import_marketplace_revenue.py # ВБ/ОЗОН выручка год
python scripts/import_retail_history.py "/path/to/Gmail (1).zip" # реестры «для ИИ»
uvicorn app.main:app --host 0.0.0.0 --port 8088 --reload
```

Открой `http://localhost:8088/` с телефона (в той же Wi-Fi).

## Velocity-оценка продаж
Поскольку публичный API WB не отдаёт продажи за 30 дней, используется
эвристика: `feedbacks_lifetime × 20 / max(1, age_months)`, где возраст карточки
оценивается по nm_id (последовательный counter WB, см. калибровку в
`app/services/providers/wb_public.py`). Для боевой точности команда
подключает MPStats/WBCON через адаптер `AnalyticsProvider`.

Каждый запрос оценки сохраняет `market_snapshots`: цена, отзывы, остатки, рейтинг и
оценка продаж по `nm_id`. Когда один и тот же SKU встречается повторно через
1+ день, система считает velocity по дельтам снимков и помечает ответ
`SNAPSHOT_VELOCITY`; до накопления истории ставится `SNAPSHOT_COLD_START`.

## Внутренняя история Разноторга
Файлы вида `Реестр ... для ИИ.xlsx` импортируются в `retail_history_items`.
Это отдельный предохранитель поверх WB: если маркетплейс показывает спрос, но в
Разноторге похожий вид залеживается, падает год к году или имеет слабую
рентабельность, `GREEN` понижается до `YELLOW`, а ответ получает явные коды
`OFFLINE_OVERSTOCK`, `OFFLINE_DECLINING` или `LOW_OFFLINE_PROFITABILITY`.

## Коды причин
В каждом ответе `verdict_reasons` содержит enum-коды:
`NO_VISUAL` / `LOW_FEEDBACKS` / `WB_DETAIL_UNAVAILABLE` / `CATEGORY_UNRESOLVED` /
`HETEROGENEOUS_SUBJECTS` / `NO_BENCHMARK` / `DEAD_NICHE` / `WB_ONLY` /
`HEURISTIC_BENCHMARK` / `VERDICT_CAPPED` / `SLOW_LOOKUP` / `SNAPSHOT_COLD_START` /
`SNAPSHOT_VELOCITY` / `LOW_MARGIN` / `HIGH_STOCK_PRESSURE` / `TOP_HEAVY_CATEGORY` /
`LOW_DATA_QUALITY` / `DECLINING_TREND` / `HIGH_SKU_DEMAND` /
`HIGH_NICHE_VOLUME` / `MODERATE_DEMAND` / `LOW_SKU_DEMAND` /
`NO_RETAIL_HISTORY` / `RETAIL_HISTORY_OK` / `OFFLINE_OVERSTOCK` /
`OFFLINE_DECLINING` / `LOW_OFFLINE_PROFITABILITY`.
Если данные неполные, `GREEN` автоматически понижается до `YELLOW`, а при
неустойчивой категории — до `UNKNOWN`. Подробности — `app/services/reason_codes.py`.

## Развёртывание
Инфраструктурные инструкции в этот пакет не включены. Новая команда должна
разворачивать систему в своей среде по требованиям из `handoff/TECH_SPEC.md`.
