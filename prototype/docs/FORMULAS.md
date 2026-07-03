# SAOL — все формулы (для проверки аналитиками)

Документ перечисляет **каждую расчётную формулу** в системе, по шагам пайплайна,
с указанием файла/функции в коде и пояснением переменных. Числа — ровно как в
коде на дату документа (2026-05-30). Пороговые константы вынесены в конец.

Обозначения: `clamp(x) = max(0, min(1, x))`. `median` — медиана. Все «продажи» —
оценка штук в месяц (точных продаж публичный WB не отдаёт).

---

## 0. Константы (app/settings.py + модули)

| Константа | Значение | Смысл |
|---|---|---|
| `min_feedbacks` | 10 | порог отзывов: карточка считается «живым аналогом» |
| `rating_green` (g) | 0.6 | порог «сильно» для спроса/объёма |
| `rating_yellow` (y) | 0.3 | порог «средне» |
| `absolute_demand_target_month` | 400 | шт/мес, при которых абсолютный спрос «полный» |
| `FEEDBACK_TO_SALES` | 20.0 | 1 отзыв ≈ 20 продаж (≈5% покупателей оставляют отзыв) |
| `_MAX_REASONABLE_SALES_PER_MONTH` | 5000 | кэп оценки продаж |
| `visual_max_cards` | 300 | сколько ближайших visual-результатов тянуть карточками |
| `snapshot_min_days_for_velocity` | 1.0 | мин. окно между снимками для расчёта скорости |

---

## 1. Оценка продаж по отзывам (velocity)
`app/services/providers/wb_public.py: estimate_monthly_sales / estimate_card_age_months`

Продаж публичный WB не отдаёт, поэтому оцениваем через накопленные отзывы с
поправкой на возраст карточки (иначе старая карточка с большими отзывами даёт
ложно высокий «спрос»).

```
возраст_мес(nm_id)   = (сегодня − дата_создания(nm_id)) / 30.4,  не меньше 1
дата_создания(nm_id) = линейная интерполяция по калибровочной таблице nm_id→дата
продажи_мес          = min( (отзывы / возраст_мес) × 20 ,  5000 )
                       = 0, если отзывов ≤ 0
```

Калибровка `nm_id → дата` (8 опорных точек, экстраполяция за последней по темпу
последнего сегмента):

| nm_id | дата |
|---|---|
| 1 | 2017-01 |
| 50 млн | 2019-01 |
| 100 млн | 2020-01 |
| 150 млн | 2021-06 |
| 200 млн | 2023-01 |
| 250 млн | 2024-06 |
| 300 млн | 2025-06 |
| 350 млн | 2026-04 |

**Переменные:** `nm_id` — артикул WB (сквозной счётчик); `отзывы` — feedbacks
карточки за всё время; `возраст_мес` — оценка возраста; `20` = `FEEDBACK_TO_SALES`.

> ⚠️ Слабое место для ревью: и темп отзывов, и коэффициент 20, и калибровка —
> эвристики. Для точности подключается MPStats/WBCON через `AnalyticsProvider`.

---

## 2. Скорость по снимкам (точнее, если есть история)
`app/services/snapshots.py: build_snapshot_metrics`

При повторной встрече того же `nm_id` спустя ≥1 день считаем скорость по **дельте**
отзывов/остатков, а не по lifetime:

```
для каждого nm с прошлым снимком (days = дней между, days ≥ 1):
    feedback_velocity = (Δотзывов / days) × 30.4 × 20      (если Δотзывов ≥ 0)
    stock_velocity    = (Δостатка_вниз / days) × 30.4      (если остаток убыл)

median_feedback_sales_30d = median(feedback_velocity по всем nm)
observation_days          = max(days)
```

Тренд (динамика скорости по снимкам против lifetime-оценки):
```
proxy = median(продажи_мес по аналогам)
ratio = median_feedback_sales_30d / proxy
trend = 0.85 если ratio ≥ 1.2;  0.65 если ≥ 0.8;  0.45 если ≥ 0.4;  иначе 0.2
trend = 0.5, если данных нет
```

## 2b. Эффективная скорость
`app/services/liquidity.py: _effective_velocity`

```
если у аналогов И у топа есть медиана по снимкам (matched > 0):
    a_med_eff, t_med_eff = медианы по снимкам        → код SNAPSHOT_VELOCITY
иначе:
    a_med_eff, t_med_eff = lifetime-оценки (§1)    → код SNAPSHOT_COLD_START
```

---

## 3. Категория — взвешенное голосование
`app/services/category_extractor.py: vote_subject_id`

```
вес(subjectId) = Σ отзывов карточек этого subjectId среди аналогов
доля_лидера    = вес(лидер) / Σ всех весов
если доля_лидера ≥ 0.6 → категория = subjectId лидера
иначе                  → категория НЕ определена (HETEROGENEOUS_SUBJECTS)
```
**Переменные:** карточки взвешиваются своими `feedbacks`; карточки с 0 отзывов
не голосуют. Порог 0.6 защищает от случайного лидера из смежной категории.

---

## 4. Сигналы спроса
`app/services/liquidity.py: compute_score`

```
a_med, t_med   = median(продажи аналогов), median(продажи топа)
a_sum, t_sum   = Σ продаж аналогов, Σ продаж топа
a_med_eff, t_med_eff = эффективные (§2b)

sku_demand     = a_med_eff / t_med_eff           (0, если t_med_eff ≤ 0)   ← относительно лидеров
niche_volume   = a_sum / t_sum                                            ← ёмкость ниши
abs_demand     = a_med_eff / 400                                          ← абсолютный спрос
demand_signal  = max(sku_demand, abs_demand)                              ← объединённый
```
**Смысл:** `sku_demand` — насколько типичный аналог сравним с лидером ниши;
`niche_volume` — есть ли в нише деньги; `abs_demand` — продаёт ли товар много в
штуках сам по себе (защита от ложного RED в «top-heavy» нишах).

---

## 5. Перцентиль / популярность на WB
`app/services/liquidity.py: percentile_rank`

```
популяция = продажи (топ ∪ аналоги), дедуп по nm_id
перцентиль = 100 × (кол-во_ниже + 0.5 × кол-во_равных) / N        (0, если value ≤ 0)
value      = a_med_eff
wb_popularity_score = перцентиль        (0–100, показывается как «Спрос на WB X/100»)
```

---

## 6. Под-баллы (каждый 0…1)
`app/services/liquidity.py`

```
demand_score      = clamp(demand_signal / 0.6)
niche_score       = clamp(niche_volume / 0.6)

# оборачиваемость (_sell_through): pressure = median(остатки) / max(1, продажи_мес)
sell_through      = 1.0 (pressure≤1); 0.8 (≤2); 0.55 (≤4); 0.35 (≤6); иначе 0.15
                    = 0.5, если остатков/спроса нет
stock_pressure_months = pressure (запас в месяцах спроса)

# маржа (_margin_score): markup = median_цена_рынка / закуп
margin            = 0.1 (markup<1.3); 0.35 (<1.5); 0.65 (<2); 0.85 (<3); 1.0 (≥3)
                    = 0.65, если цена закупа не задана
median_цена_рынка = median(sale_price|price по топу, иначе по аналогам)

# конкуренция (_competition_score):
если карточек топа < 10:  competition = 0.55×sell_through + 0.45×0.55
иначе: concentration = (продажи топ-5) / (продажи всего топа)
       competition  = clamp( 0.55×sell_through + 0.45×(1 − clamp((concentration−0.35)/0.45)) )
       (concentration ≥ 0.75 → флаг TOP_HEAVY_CATEGORY)

# тренд: §2 (снапшоты), по умолчанию 0.5

# качество данных (_data_quality_score):
data_quality = clamp( 1.0 − 0.25·[аналогов<3] − 0.2·[топа<10] − 0.15·[снимков=0] )
```

---

## 7. Итоговые баллы
`app/services/liquidity.py: compute_score`

```
коммерческий (0–100) = 100 × ( 0.33·demand_score + 0.14·niche_score
                             + 0.20·sell_through + 0.20·margin
                             + 0.05·competition + 0.08·trend )

балл_ликвидности (0–100) = 0.75 × коммерческий + 0.25 × перцентиль

rating (служебный 0–1) = 2·demand_signal·niche_volume / (demand_signal + niche_volume)   (гарм. среднее)
```
`data_quality` НЕ входит в коммерческий балл — он влияет на «достоверность» и коды.

---

## 8. Вердикт
`app/services/liquidity.py` (каскад) + `app/services/engine.py: _apply_decision_policy` (предохранители)

Каскад (первое сработавшее), g=0.6, y=0.3:
```
1) margin ≤ 0.25 (наценка < 1.3×)                                       → RED
2) stock_pressure_months > 9  и  demand_score < 0.75                    → RED
3) sku_demand > g  И  niche_volume > g  И  margin ≥ 0.55  И  sell_through ≥ 0.45 → GREEN
4) demand_signal > g  ИЛИ  niche_volume > g                             → YELLOW
5) demand_signal > y  И  niche_volume > y                               → YELLOW
6) demand_signal ≤ y  И  niche_volume ≤ y                               → RED
7) иначе                                                                → YELLOW
```
Ранний выход в UNKNOWN: нет аналогов (LOW_FEEDBACKS) / нет топа (NO_BENCHMARK) /
продажи топа = 0 (DEAD_NICHE → RED).

> GREEN использует **относительный** `sku_demand` (товар уровня лидеров — «хит»).
> YELLOW/RED используют `demand_signal` (относит. ИЛИ абсолют). То есть абсолютный
> спрос вытягивает середняка максимум в YELLOW, но не делает GREEN.

Предохранители (понижают итог):
```
HETEROGENEOUS_SUBJECTS | CATEGORY_UNRESOLVED               → UNKNOWN
GREEN + любой из {HEURISTIC_BENCHMARK, LOW_SAMPLE,
                  OFFLINE_DECLINING, LOW_OFFLINE_PROFITABILITY} → YELLOW
LOW_SAMPLE добавляется, если аналогов < 3 или топа < 10
```

## 8b. Чистый WB-спрос (показатель «Спрос на WB»)
`app/services/engine.py: _wb_demand_verdict`
```
analog_count ≤ 0 или top_count ≤ 0                                  → UNKNOWN
sku_demand ≥ 0.6  И  wb_popularity ≥ 45                             → GREEN
sku_demand ≥ 0.3  ИЛИ  wb_popularity ≥ 30  ИЛИ  продажи_мес ≥ 120   → YELLOW
иначе                                                               → RED
```

---

## 9. Сколько брать (закуп)
`app/services/engine.py` Шаг 7

Модель для НОВЫХ товаров: положительный вердикт означает размер теста, а не
массовую закупку как для проверенной позиции. История Разноторга и K показываются
справочно, но не входят в число закупа.

```
base_qty_by_purchase_price:
  закуп ≤ 100 ₽       → 50 шт
  100 < закуп ≤ 300 ₽ → 20 шт
  300 < закуп ≤ 1000 ₽→ 7 шт
  закуп > 1000 ₽      → 2 шт

multiplier_by_verdict:
  STRONG  → 1.5
  GREEN   → 1.0
  YELLOW  → 0.5
  RED     → 0
  UNKNOWN → нет числа

рекомендация = round(base_qty × multiplier), минимум 1 для положительного теста
```

`forecast_source = "test_quantity"`, если число выдано. `market_share_coefficient`,
`raznotorg_units_year`, `raznotorg_positions` и `baseline_units_per_position`
оставлены только как справка/диагностика покрытия классификаторов.

---

## 10. Доля рынка K (справочно, в закупе НЕ используется)
`scripts/build_category_coefficient.py`
```
K = выручка_Разноторга_в_категории / выручка_категории_на_WB
```
Считается двумя проходами: по WB-путям Разноторга (path) и по ИМЕНИ листа
категории (name-match, `app/services/name_match.py`, лёгкий стеммер + subset
корней). Имя-строки берутся только для крупных листов (выручка ≥ 50 млн) с
правдоподобной долей (K ≤ 3%). Показывается как «доля Разноторга», в формулу
закупа (§9) не входит.

---

## 11. Уточнение фото текстом (опционально)
`web/app.js` (телефон) + `app/services/engine.py` (сервер)
```
телефон: nm_id(visual) ∩ nm_id(WB-текст-поиск по тексту)   (если пересечений ≥ 3)
сервер:  оставить аналоги, чьё имя содержит ВСЕ значимые токены текста
         (если таких с отзывами ≥ 3; иначе вся выдача)
```
Сужает выборку до нужного варианта (вес/объём/шт), не добавляя новых товаров.

---

## Сводка зависимостей цены и спроса

- **Закупочная цена** входит только в `margin` → влияет на вердикт (наценка <1.3× = RED)
  и на балл (20% веса). На «Спрос на WB» и на `прогноз_сырой` НЕ влияет.
- **Спрос товара** (`sku_demand`/`abs_demand`) влияет на вердикт, балл и `wb_strength`
  (через него — на закуп).
