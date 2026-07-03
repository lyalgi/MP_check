"""Извлечение показателей товара из ответов MPStats (см. методологию v2, шаг 3).

Из `items/{nm}/full` берём цену, категорию, % выкупа, дату создания, фото и заказы
за период. Из `items/{nm}/by_period` (дневной ряд) считаем помесячную сезонность,
последние 30 дней и среднемесячное.

МЕТРИКА СПРОСА: поле MPStats `sales` — это ЗАКАЗЫ в штуках (заказано). Это и есть
наша главная метрика спроса. % выкупа лежит отдельно в `subject.purchase.purchase`;
при желании выкупы = заказы × выкуп% (отдельный сигнал качества, не главный).
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass
class ItemMetrics:
    nm: int
    name: str = ""
    brand: str = ""
    link: str = ""
    subject_id: int | None = None
    subject_name: str | None = None
    price: float = 0.0                  # final_price (со скидкой)
    base_price: float = 0.0             # price (без скидки)
    balance: int = 0                    # текущий остаток
    in_stock: bool = False
    first_date: str | None = None
    age_months: float | None = None
    commission_fbo: float | None = None
    buyout_pct: float | None = None     # % выкупа (subject.purchase.purchase)
    image_thumb: str | None = None      # миниатюра (photo.list[0].t)

    orders_year: float = 0.0            # ЗАКАЗЫ за период (поле API `sales`), шт
    revenue_year: float = 0.0
    orders_monthly_avg: float = 0.0     # заказы/мес (год / 12)
    orders_30d: float = 0.0             # последние 30 дней ряда
    redeemed_year_est: float = 0.0      # выкупы ≈ заказы × выкуп%
    redeemed_monthly_avg: float = 0.0   # ВЫКУПЫ/мес — главная метрика спроса (реальные продажи)
    monthly_orders: list[float] = field(default_factory=list)  # помесячно (сезонность)
    season_index: float | None = None   # последний месяц / среднемесячное

    ok: bool = False                    # удалось ли собрать данные


def _active_months(age_months: float | None) -> float:
    """Сколько месяцев карточка реально живёт — делитель для среднемесячного. Молодую
    карточку НЕЛЬЗЯ делить на 12: виральный новичок (Labubu) иначе выглядит в разы
    слабее реального. Возраст неизвестен → 12 (консервативно)."""
    if not age_months or age_months <= 0:
        return 12.0
    return min(12.0, max(1.0, round(age_months)))


def _age_months(first_date: str | None) -> float | None:
    if not first_date:
        return None
    try:
        d0 = datetime.strptime(first_date, "%Y-%m-%d").date()
    except ValueError:
        return None
    return round(max(0.0, (date.today() - d0).days) / 30.4, 1)


def calendar_monthly(rows: list[dict]) -> list[float]:
    """Заказы по КАЛЕНДАРНЫМ месяцам (индекс 0=январь … 11=декабрь) — для сезонности."""
    buckets = [0.0] * 12
    for r in rows or []:
        d = r.get("data") or r.get("date")
        if not d or len(d) < 7:
            continue
        try:
            mon = int(d[5:7])
        except ValueError:
            continue
        if 1 <= mon <= 12:
            buckets[mon - 1] += float(r.get("sales") or 0)
    return buckets


def series_trend(rows: list[dict], window: int = 90) -> float | None:
    """Тренд год-к-году (сезон-нейтральный): сумма заказов за последние `window` дней
    ÷ за те же `window` дней РОВНО год назад. Нужен ряд ~455 дней (365 + window).
    >1 растёт, <1 падает. None — если данных за прошлый год нет."""
    from datetime import date as _date, timedelta as _td
    dated: list[tuple[_date, float]] = []
    for r in rows or []:
        d = r.get("data") or r.get("date")
        if d:
            try:
                dated.append((_date.fromisoformat(d[:10]), float(r.get("sales") or 0)))
            except ValueError:
                continue
    if len(dated) < window + 30:
        return None
    dated.sort()
    last = dated[-1][0]
    rec_start = last - _td(days=window)
    anchor = last - _td(days=365)
    past_start = anchor - _td(days=window)
    recent = sum(s for d, s in dated if rec_start < d <= last)
    past = sum(s for d, s in dated if past_start < d <= anchor)
    if past < 5:                       # крошечная база год назад → тренд это шум (×100 артефакты)
        return None
    return round(min(5.0, max(0.1, recent / past)), 3)   # clamp: даже виралка не «+10500%»


def _monthly_from_by_period(rows: list[dict]) -> tuple[list[float], float]:
    """Помесячные суммы заказов + заказы за последние 30 дней (поле API `sales`)."""
    by_month: dict[str, float] = defaultdict(float)
    dated: list[tuple[str, float]] = []
    for r in rows or []:
        d = r.get("data") or r.get("date")
        s = float(r.get("sales") or 0)
        if not d:
            continue
        by_month[d[:7]] += s
        dated.append((d, s))
    months = [by_month[k] for k in sorted(by_month)]
    dated.sort()
    last_30 = sum(s for _, s in dated[-30:])
    return months, last_30


def build_item_metrics(full: dict | None, by_period: list | None, nm: int) -> ItemMetrics:
    m = ItemMetrics(nm=nm)
    if not full:
        return m
    m.ok = True
    m.name = full.get("name") or ""
    m.brand = full.get("brand") or ""
    m.link = full.get("link") or ""

    subj = full.get("subject") or {}
    m.subject_id = subj.get("id")
    m.subject_name = subj.get("name")
    comm = subj.get("commission") or {}
    m.commission_fbo = comm.get("fbo")
    pur = subj.get("purchase") or {}
    m.buyout_pct = pur.get("purchase")

    price = full.get("price") or {}
    m.price = float(price.get("final_price") or 0)
    m.base_price = float(price.get("price") or 0)

    m.balance = int(full.get("balance") or 0)
    stock = full.get("stock") or {}
    m.in_stock = bool((stock.get("fbo") or 0) + (stock.get("fbs") or 0) > 0) or m.balance > 0

    m.first_date = full.get("first_date")
    m.age_months = _age_months(m.first_date)

    photos = (full.get("photo") or {}).get("list") or []
    if photos:
        m.image_thumb = photos[0].get("t") or photos[0].get("f")

    ps = full.get("period_stats") or {}
    m.orders_year = float(ps.get("sales") or 0)   # API `sales` = заказы (шт)
    m.revenue_year = float(ps.get("revenue") or 0)
    am = _active_months(m.age_months)             # молодую карточку делим на её возраст, не на 12
    m.orders_monthly_avg = round(m.orders_year / am, 1)

    if m.buyout_pct and m.buyout_pct > 0:
        m.redeemed_year_est = round(m.orders_year * (m.buyout_pct / 100.0), 1)
    else:
        m.redeemed_year_est = m.orders_year     # выкуп% неизвестен → не штрафуем
    m.redeemed_monthly_avg = round(m.redeemed_year_est / am, 1)

    m.monthly_orders, m.orders_30d = _monthly_from_by_period(by_period or [])
    if m.monthly_orders:
        avg = sum(m.monthly_orders) / len(m.monthly_orders)
        if avg > 0:
            m.season_index = round(m.monthly_orders[-1] / avg, 2)
    return m


def fetch_item_metrics(client, nm: int, d1: str | None = None, d2: str | None = None,
                       with_graph: bool = True) -> ItemMetrics:
    """Один товар: full (+by_period для сезонности). with_graph=False — без дневного
    графика (для аналогов, где сезонность не нужна): экономит по вызову на товар."""
    full = client.item_full(nm, d1, d2)
    by_period = client.item_by_period(nm, d1, d2) if with_graph else None
    return build_item_metrics(full, by_period, nm)


def _monthly_from_graph(graph: list) -> tuple[list[float], float]:
    """Дневной массив (без дат) → 12 помесячных корзин + сумма последних 30 дней."""
    g = [float(x or 0) for x in (graph or [])]
    if not g:
        return [], 0.0
    n = len(g)
    months = [sum(g[i * n // 12:(i + 1) * n // 12]) for i in range(12)]
    return months, sum(g[-30:])


def build_item_metrics_from_row(row: dict) -> ItemMetrics:
    """Строка similar/category уже содержит всё — собираем ItemMetrics без доп. вызовов.

    Поле `sales` = заказы (шт) за период; `sales_graph` — дневной ряд заказов;
    `purchase` = % выкупа; `final_price`/`balance`/`subject`/`thumb` — как в карточке.
    """
    nm = int(row.get("id") or row.get("nmId") or 0)
    m = ItemMetrics(nm=nm, ok=nm > 0)
    m.name = row.get("name") or ""
    m.brand = row.get("brand") or ""
    m.link = row.get("url") or ""
    m.image_thumb = row.get("thumb") or row.get("thumb_middle")
    m.subject_id = row.get("subject_id")
    m.subject_name = row.get("subject")
    m.price = float(row.get("final_price") or 0)
    m.base_price = float(row.get("start_price") or row.get("basic_price") or 0)
    m.balance = int(row.get("balance") or 0)
    m.in_stock = m.balance > 0
    m.buyout_pct = row.get("purchase")
    m.commission_fbo = row.get("commission_fbo")
    m.first_date = row.get("sku_first_date")
    m.age_months = _age_months(m.first_date)

    m.orders_year = float(row.get("sales") or 0)
    m.revenue_year = float(row.get("revenue") or 0)
    am = _active_months(m.age_months)
    m.orders_monthly_avg = round(m.orders_year / am, 1)
    if m.buyout_pct and m.buyout_pct > 0:
        m.redeemed_year_est = round(m.orders_year * (m.buyout_pct / 100.0), 1)
    else:
        m.redeemed_year_est = m.orders_year
    m.redeemed_monthly_avg = round(m.redeemed_year_est / am, 1)

    m.monthly_orders, m.orders_30d = _monthly_from_graph(row.get("sales_graph") or [])
    if m.monthly_orders:
        avg = sum(m.monthly_orders) / len(m.monthly_orders)
        if avg > 0:
            m.season_index = round(m.monthly_orders[-1] / avg, 2)
    return m
