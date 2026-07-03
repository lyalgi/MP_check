# Рабочая карта проекта для дальнейшей работы

Дата просмотра: 2026-06-02.

Обновление по текущему UI v2: экран `prototype/web/` сейчас может обслуживаться
`prototype/saol2/web_app.py` через `python -m saol2.serve` на порту 8765. В этой
ветке итоговый вердикт считается в `prototype/saol2/scoring.py`, а не в старой
FastAPI-модели `prototype/app/services/liquidity.py`.

Для v2 важные отличия:

- `seed_nm`/"ЭТОТ ТОВАР НА WB" показывается отдельно как справка о конкретной
  карточке, но итоговый verdict считается по визуально похожим товарам.
- Спрос считается в деньгах: медианные выкупы похожих в месяц * медианная цена,
  затем сравнение с денежным полом категории и топ-10 категории.
- `category_pct` - место типичной похожей карточки в срезе категории по выручке.
- `score_100 = 100 * demand_score * margin_gate * trend_coef`.
- Жесткая отсечка `abs_floor = 0.15`: если выручка похожих ниже 15% от пола
  категории, verdict становится `RED` независимо от большой наценки.
- Все оценки v2 пока помечены `provisional=True`: полосы вердикта еще не
  калиброваны на фактических исходах тестовых закупок.

## Общий смысл

Это handoff-пакет по SAOL: системе оценки нового товара для офлайн-закупки Разноторга.
Пользовательский сценарий: закупщик фотографирует товар, вводит закупочную цену и за
10-30 секунд получает вердикт `STRONG/GREEN/YELLOW/RED/UNKNOWN`, размер тестовой
партии и причины решения.

Ключевой принцип из документации: WB/Ozon используются как датчик внешнего спроса,
а не как прямой конкурент офлайн-магазина. Для нового товара положительный вердикт
означает тестовую партию, не массовую закупку. Финальное решение не должно опираться
на AI/LLM/ML, все деградации должны быть явными через reason codes.

## Корень

- `handoff/` - документация передачи и переносимые CSV/SQL данные для новой команды.
- `prototype/` - рабочий прототип FastAPI + мобильный web UI + SQLite + тесты.
- `API.md` - короткая справка/заглушка по API.
- `QUICK_TEST_RU.txt`, `БЫСТРЫЙ_ТЕСТ.txt` - быстрые ссылки/инструкции теста.
- `.claude/` - локальные настройки среды, не часть приложения.

Git в текущей shell-среде не доступен из PATH, состояние репозитория проверить не удалось.

## Главные документы

- `handoff/README.md` - зачем пакет и как его читать.
- `handoff/TECH_SPEC.md` - основное ТЗ: пользовательский поток, пайплайн, формулы,
  архитектура боевого режима, SLA, риски, тестирование.
- `handoff/PROTOTYPE_NOTES.md` - что полезно брать из прототипа и что нельзя копировать
  бездумно.
- `handoff/TESTING_INSTRUCTIONS.md` - локальный запуск, UI/API проверки, режимы провайдеров.
- `prototype/README.md` - запуск и текущая логика прототипа.
- `prototype/docs/FORMULAS.md` - текущие формулы прототипа на 2026-05-30.
- `prototype/docs/МЕТОДОЛОГИЯ_v2.md` - целевая методология v2 под MPStats.
- `prototype/reports/field_qa_1688_2026-05-29.md` - полевые QA-наблюдения и слабые места.

## Prototype: стек и запуск

Стек: Python 3.11+, FastAPI, SQLAlchemy 2.x, SQLite, Pydantic v2, httpx/requests,
Pillow/pillow-heif, vanilla HTML/CSS/JS.

Запуск из `prototype/`:

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8088 --reload
```

Основные URL:

- UI: `http://127.0.0.1:8088/`
- Swagger: `http://127.0.0.1:8088/docs`
- health: `http://127.0.0.1:8088/healthz`

Переменные окружения читаются с префиксом `SAOL_`.
Важные настройки в `prototype/app/settings.py`:

- `analytics_provider`: `wb_public` или `mock`
- `visual_search_provider`: `wb_photo`, `mock` или `http`
- `visual_search_url`
- `db_path`: по умолчанию `./data/saol.db`
- `min_feedbacks`: 10
- `visual_max_cards`: 300
- `rating_green`: 0.6
- `rating_yellow`: 0.3
- `absolute_demand_target_month`: 400
- `slow_lookup_ms`: 25000
- `benchmark_cache_max_age_hours`: 168

## Prototype: API и web

Точка входа FastAPI:

- `prototype/app/main.py`
  - подключает CORS;
  - подключает роутеры `health`, `lookup`, `taxonomy`, `history`;
  - монтирует `prototype/web` как static;
  - на startup вызывает `init_db()`.

Роутеры:

- `prototype/app/routers/lookup.py`
  - `POST /api/v1/lookup`;
  - принимает `purchase_price`, фото, `seed_url`, текстовое уточнение `query`,
    список `nm_ids`;
  - нормализует PNG/WebP/HEIC в JPEG;
  - ограничивает фото 8 МБ;
  - ставит общий timeout 70 секунд.
- `prototype/app/routers/health.py`
  - `GET /healthz`.
- `prototype/app/routers/taxonomy.py`
  - группы, подгруппы, виды, поиск по таксономии.
- `prototype/app/routers/history.py`
  - `GET /api/v1/history`.

Web UI:

- `prototype/web/index.html`
- `prototype/web/app.js`
- `prototype/web/styles.css`
- `prototype/web/manifest.json`, `sw.js`, иконки.

UI mobile-first. Документация предупреждает, что service worker/кэш может мешать
при разработке, но файл `sw.js` сейчас есть.

## Prototype: основная бизнес-логика

Главный оркестратор:

- `prototype/app/services/engine.py`
  - `LookupInput`;
  - `LiquidityEngine.evaluate()`;
  - `engine = LiquidityEngine()`.

Пайплайн `evaluate()`:

1. Получает `nm_id` через device-side список, фото-поиск WB или fallback по seed nm.
2. Подтягивает карточки WB и фильтрует аналоги по `feedbacks >= min_feedbacks`.
3. Сужает выдачу по `query`, если есть минимум 3 точных совпадения с отзывами.
4. Голосует категорию по `subjectId`, вес = отзывы, лидер должен набрать >=60%.
5. Ищет WB/Ozon mapping.
6. Строит top benchmark из кэша, WB catalog/subject catalog или эвристики visual subset.
7. Строит snapshot metrics, считает score, записывает новые market snapshots.
8. Подключает коэффициенты/историю Разноторга как диагностику и предохранители.
9. Применяет decision policy: mixed category -> `UNKNOWN`, слабые данные могут понизить
   `GREEN/STRONG` до `YELLOW`.
10. Считает тестовую партию по закупочной цене и вердикту.
11. Пишет `LookupHistory`.

Расчет формул:

- `prototype/app/services/liquidity.py`
  - `compute_score()`;
  - `LiquidityScore`;
  - `build_advice()`;
  - спрос: `sku_demand = median(analogs)/median(top)`;
  - объем ниши: `niche_volume = sum(analogs)/sum(top)`;
  - абсолютный спрос: `a_med / absolute_demand_target_month`;
  - коммерческий балл: demand, niche, sell-through, margin, competition, trend;
  - verdict cascade: маржа/остатки могут блокировать, `GREEN` только при сильных
    относительном спросе, объеме, марже и оборачиваемости;
  - `STRONG` - только поверх `GREEN` при высоком перцентиле или очень высоком спросе.

Категория и карточки:

- `prototype/app/services/category_extractor.py`
  - загрузка raw карточек;
  - `vote_subject_id()`;
  - `extract_category()`.

Провайдеры данных:

- `prototype/app/services/providers/wb_public.py` - публичные WB catalog/cards/subject
  и эвристика продаж через `feedbacks * 20 / age_months`.
- `prototype/app/services/wb_visual_search.py` - visual search WB, mock/http providers,
  парсинг WB nm из URL, нормализация изображения для upload.
- `prototype/app/services/wb_http.py` - HTTP-слой к WB.
- `prototype/app/services/providers/wb_proxy.py` - выбор/маркировка proxy port.
- `prototype/app/services/providers/mock.py` - mock provider для разработки/тестов.
- `prototype/app/services/analytics.py` - выбор analytics provider.

Данные и предохранители:

- `prototype/app/services/mapping.py` - WB/Ozon mapping.
- `prototype/app/services/forecast.py` - коэффициент K, сейчас справочно.
- `prototype/app/services/retail_history.py` - история Разноторга как risk guard.
- `prototype/app/services/snapshots.py` - market snapshots и velocity по дельтам.
- `prototype/app/services/benchmark_cache.py` - кэш top benchmark категории.
- `prototype/app/services/reason_codes.py` - enum кодов причин.
- `prototype/app/services/name_match.py` - нормализация/стемминг для matching.
- `prototype/app/services/taxonomy.py`, `subjects.py` - справочники.

## Prototype: БД и данные

Файл SQLite:

- `prototype/data/saol.db` - примерно 206 МБ.

Переносимый handoff-экспорт:

- `handoff/db_tables/schema.sql`
- `handoff/db_tables/table_counts.csv`
- `handoff/db_tables/*.csv`

Количество строк из `table_counts.csv`:

- `category_coefficient`: 85
- `lookup_history`: 47
- `market_snapshots`: 2599
- `marketplace_category_revenue`: 196894
- `retail_history_items`: 103174
- `taxonomy_items`: 8924
- `wb_ozon_mapping`: 210
- `wb_subjects`: 8150

Основные таблицы SQLAlchemy в `prototype/app/models.py`:

- `TaxonomyItem` - классификатор Разноторга + WB/Ozon paths.
- `WbSubject` - WB subjectId справочник.
- `MarketSnapshot` - метрики карточки на момент запроса.
- `MarketplaceCategoryRevenue` - годовая выручка категорий WB/Ozon.
- `CategoryCoefficient` - K = выручка Разноторга / выручка маркетплейса.
- `CategoryBenchmark` - кэш top-N ниши.
- `RetailHistoryItem` - внутренняя история продаж/остатков/рентабельности.
- `WbOzonMapping` - статический mapping WB <-> Ozon.
- `LookupHistory` - история запросов закупщиков.

Миграций Alembic нет. `prototype/app/db.py` делает `create_all()` и ручную SQLite
миграцию колонок `lookup_history`.

## Prototype: scripts

- `scripts/import_classifier.py` - импорт xlsx-классификаторов Разноторга.
- `scripts/import_wb_subjects.py` - импорт WB subjects.
- `scripts/build_marketplace_mapping.py` - построение WB/Ozon mapping.
- `scripts/import_marketplace_revenue.py` - импорт годовой выручки WB/Ozon.
- `scripts/import_retail_history.py` - импорт реестров Разноторга.
- `scripts/build_category_coefficient.py` - расчет K.
- `scripts/build_category_benchmark.py` - предрасчет top benchmark.
- `scripts/field_regression.py` - полевая регрессия.

Исходные xlsx:

- `prototype/classifiers/` - классификаторы Разноторга/WB/Ozon.
- `prototype/registries/` - реестры "для ИИ".
- `prototype/revenue/` - WB/Ozon revenue xlsx.

## Prototype: tests

Тесты лежат в `prototype/tests/`. В `handoff/TESTING_INSTRUCTIONS.md` указано, что на
момент упаковки было `62 passed`.

Покрытые области по именам тестов:

- benchmark cache и фильтр subject;
- полный engine pipeline;
- heterogeneous subjects, low feedbacks, WB_ONLY, caps verdict;
- seed URL fallback и device-side `nm_ids`;
- query refinement;
- action units by verdict;
- liquidity formulas, low margin, snapshot velocity, size invariance;
- mapping exact/by-subject/prefix/missing;
- name matching/stemming;
- retail history import и risk flags;
- taxonomy groups/subgroups/search;
- visual search parsing/extract/normalization.

## `saol_core.py`

`prototype/saol_core.py` - автономный старый файл алгоритма без БД и web.
Документация прямо говорит: не считать его актуальной архитектурой. Актуальная логика
живет в `prototype/app/services/engine.py` и `liquidity.py`.

## `saol2`

`prototype/saol2/` - экспериментальная/целевая ветка методологии v2 с MPStats.

Ключевые файлы:

- `saol2/pipeline.py` - CLI/web pipeline через MPStats, `analyze()` и `evaluate()`.
- `saol2/scoring.py` - scoring v2.
- `saol2/metrics.py` - item metrics, дневные графики, месячные агрегаты.
- `saol2/mpstats.py` - клиент MPStats и кэш.
- `saol2/visual.py` - визуальный поиск по фото/nm.
- `saol2/web_app.py`, `serve.py` - отдельное web-приложение/запуск.

Методология v2 отличается от текущего FastAPI-прототипа:

- источник спроса планируется MPStats, не отзывы WB * 20;
- спрос оценивается заказами, абсолютным порогом и перцентилем категории;
- снапшоты, концентрация, сырая история Разноторга и часть старых факторов осознанно
  убраны/отложены;
- история Разноторга планируется как следующий этап после очистки данных.

## Важные продуктовые инварианты

- Не использовать AI/LLM/ML для финального решения.
- Не скрывать деградации: `WB_ONLY`, `HEURISTIC_BENCHMARK`, `LOW_SAMPLE`,
  `SNAPSHOT_COLD_START`, `SLOW_LOOKUP`, `HETEROGENEOUS_SUBJECTS` и т.п. должны быть
  видны в API/UI.
- Категорию нельзя выбирать по одной топ-карточке; нужно weighted vote по subjectId.
- Эталон из visual subset опасен и обязан снижать доверие/капать verdict.
- Ozon mapping не должен ломать WB-first ответ, но отсутствие mapping должно быть явно.
- История Разноторга - предохранитель/диагностика, не источник массовой закупки новинки.
- Для новинок выдавать тестовую партию, а не годовой прогноз.
- Формулы и веса должны быть конфигурируемыми, версионируемыми и покрытыми тестами.

## Известные слабые места из field QA

- Infographic-heavy фото могут давать нестабильный visual search; нужен понятный UX
  "перефотографируй/обрежь товар".
- Каталожный benchmark может быть слишком строгим для совместимых товаров, например
  LEGO-compatible против официальных LEGO топов.
- Некоторые subject не имеют надежного WB catalog benchmark и падают в эвристику.
- Ozon gaps остаются, их надо показывать как риск покрытия рынка.
- Retail fallback может быть слишком широким и не должен становиться точным количеством
  закупки без проверки качества match.

## Как быстро ориентироваться при следующей задаче

Если задача про API/UI:

1. Начать с `prototype/app/routers/lookup.py`.
2. Затем `prototype/app/services/engine.py`.
3. Проверить поля ответа в `prototype/app/schemas.py`.
4. Для фронта смотреть `prototype/web/app.js`, `index.html`, `styles.css`.

Если задача про формулы/вердикт:

1. `prototype/docs/FORMULAS.md`.
2. `prototype/app/services/liquidity.py`.
3. `prototype/app/services/engine.py` methods `_apply_decision_policy()`,
   `_test_quantity()`, `_wb_demand_verdict()`, `_decision_confidence()`.
4. Тесты: `prototype/tests/test_liquidity.py`, `prototype/tests/test_engine.py`.

Если задача про данные:

1. `prototype/app/models.py`.
2. `handoff/db_tables/schema.sql` и `table_counts.csv`.
3. Импортные скрипты в `prototype/scripts/`.
4. CSV handoff или SQLite `prototype/data/saol.db`.

Если задача про новую целевую методологию:

1. `prototype/docs/МЕТОДОЛОГИЯ_v2.md`.
2. `prototype/saol2/pipeline.py`.
3. `prototype/saol2/scoring.py`.
4. Сравнить с текущими формулами в `prototype/docs/FORMULAS.md`.

## Команды проверки

Из `prototype/`:

```bash
pytest -q
python -m py_compile app/main.py
uvicorn app.main:app --host 0.0.0.0 --port 8088 --reload
```

В этой среде команды с интернетом/WB могут быть нестабильны или требовать отдельного
разрешения сети.
