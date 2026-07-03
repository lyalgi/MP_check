"""Провайдер публичных WB-эндпойнтов.

Источники (бесплатные, без авторизации):
  - search.wb.ru/exactmatch — поиск по запросу
  - catalog.wb.ru          — каталог категории
  - card.wb.ru             — детали карточек
  - static-basket-XX       — JSON-дерево категорий

Оценка скорости продаж приближённая: используются отзывы как прокси
суммарных продаж (стандартная база у Анабар/wbstat: выкуп ≈ 1/20).
Для рейтинга важно отношение «аналог / топ», поэтому одинаковая база допустима.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.schemas import AnalogSku
from app.services.providers import wb_proxy
from app.settings import settings

logger = logging.getLogger(__name__)

DEST = "-1257786"  # дефолтный регион (Москва)
# UA мобильного приложения WB — единственный, который НЕ режется на 429.
# Браузерные UA с серверных IP блокируются.
USER_AGENT = "WBClient/9.1.4 (com.wildberries.ru;build:202; iOS 17.0.0) Alamofire/5.6.1"
BROWSER_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "*/*",
    # ничего больше: WB-антибот помечает HTTP-отпечаток (Accept-Language/Encoding/Connection)
}
MENU_URL = "https://static-basket-01.wbbasket.ru/vol0/data/main-menu-ru-ru-v3.json"
SEARCH_URL = "https://search.wb.ru/exactmatch/ru/common/v9/search"
# v4 — текущая стабильная (v9 регулярно возвращает 429 на серверных IP).
CATALOG_URL_FMT = "https://catalog.wb.ru/catalog/{shard}/v4/catalog"
# Резервные шарды, если shard категории не известен.
DEFAULT_SHARDS = ("catalog", "presets", "default")
SUBJECT_SHARD_HINTS = {
    # subjects.json: «Спортивный товар/Ракетки»; main-menu: «Спорт/...».
    # Без этой подсказки subjectId=421 не находит эталон и падает в
    # visual_subset_in_subject, хотя catalog/sport31 отдаёт реальные карточки.
    "спортивный товар": ("sport31", "sport27", "sport26", "sport30"),
    "канцтовары": ("stationery4", "stationery15", "stationery14"),
    "книги и канцтовары": ("stationery4", "stationery15", "books_fiction"),
    "здоровье": ("shealth8", "shealth2", "shealth6"),
    "красота": ("beauty49", "beauty50", "beauty26", "beauty1"),
    "игрушки": ("toys10", "toys1", "toys2"),
    "детям": ("children_things2", "toys10", "sport27"),
    "хозяйственные товары": ("bathroom6", "bathroom5", "bathroom2"),
    "товары для животных": ("zoo2", "zoo5", "zoo9"),
}
SUBJECT_PARENT_ROOT_ALIASES = {
    "спортивный товар": ("Спорт",),
    "книги и канцтовары": ("Книги", "Канцтовары"),
    "хозяйственные товары": ("Дом",),
    "товары для животных": ("Зоотовары",),
}

# Множитель feedbacks → продажи (по умолчанию ~5% покупателей оставляют отзыв).
# Применяется к отзывам за месяц, а не ко всем отзывам за жизнь карточки.
FEEDBACK_TO_SALES = 20.0

# Калибровка возраста карточки по nm_id (последовательный counter WB).
# Точки эмпирические — корректируется по мере дрифта WB.
from datetime import datetime, timezone  # noqa: E402

_NM_AGE_CALIBRATION: list[tuple[int, datetime]] = [
    (1, datetime(2017, 1, 1, tzinfo=timezone.utc)),
    (50_000_000, datetime(2019, 1, 1, tzinfo=timezone.utc)),
    (100_000_000, datetime(2020, 1, 1, tzinfo=timezone.utc)),
    (150_000_000, datetime(2021, 6, 1, tzinfo=timezone.utc)),
    (200_000_000, datetime(2023, 1, 1, tzinfo=timezone.utc)),
    (250_000_000, datetime(2024, 6, 1, tzinfo=timezone.utc)),
    (300_000_000, datetime(2025, 6, 1, tzinfo=timezone.utc)),
    (350_000_000, datetime(2026, 4, 1, tzinfo=timezone.utc)),
]
# Кэп месячных продаж: даже у топ-карточек редко больше этого.
_MAX_REASONABLE_SALES_PER_MONTH = 5000.0


def estimate_card_age_months(nm_id: int, now: datetime | None = None) -> float:
    """Грубая линейная интерполяция возраста карточки по её nm_id.
    Возвращает количество месяцев. Минимум 1 — чтобы избежать деления на 0."""
    if nm_id <= 0:
        return 1.0
    now = now or datetime.now(timezone.utc)
    calib = _NM_AGE_CALIBRATION
    # за пределами — экстраполяция по последнему сегменту
    if nm_id <= calib[0][0]:
        created = calib[0][1]
    elif nm_id >= calib[-1][0]:
        # ~10M новых SKU в месяц на 2026
        last_nm, last_date = calib[-1]
        delta_nm = nm_id - last_nm
        # rate per month: последний сегмент
        prev_nm, prev_date = calib[-2]
        months_in_seg = (last_date - prev_date).days / 30.4
        rate = (last_nm - prev_nm) / max(1.0, months_in_seg)
        extra_months = delta_nm / max(1.0, rate)
        from datetime import timedelta
        created = last_date + timedelta(days=int(extra_months * 30.4))
    else:
        for (nm_a, date_a), (nm_b, date_b) in zip(calib, calib[1:]):
            if nm_a <= nm_id < nm_b:
                frac = (nm_id - nm_a) / max(1, nm_b - nm_a)
                delta_days = (date_b - date_a).days * frac
                from datetime import timedelta
                created = date_a + timedelta(days=int(delta_days))
                break
        else:
            created = calib[-1][1]
    age_days = (now - created).days
    return max(1.0, age_days / 30.4)


def estimate_monthly_sales(nm_id: int, feedbacks: int) -> float:
    """Отзывы за всё время → оценка продаж в месяц с поправкой на возраст карточки.
    Это компромисс между «делением на возраст» (старая карточка с накопленными
    отзывами не показывает ложный спрос) и доступностью данных без платных API."""
    if feedbacks <= 0:
        return 0.0
    age_m = estimate_card_age_months(nm_id)
    feedbacks_per_month = feedbacks / age_m
    sales_per_month = feedbacks_per_month * FEEDBACK_TO_SALES
    return round(min(sales_per_month, _MAX_REASONABLE_SALES_PER_MONTH), 1)


def _kop_to_rub(v: Any) -> float:
    try:
        return round(float(v) / 100.0, 2)
    except (ValueError, TypeError):
        return 0.0


def _extract_price(product: dict) -> tuple[float, float | None]:
    """Возвращает (full_price, sale_price) в рублях. WB вернул как priceU/salePriceU (копейки),
    либо в новых ответах — `sizes[0].price.basic|product|total`. Поддерживаем оба варианта."""
    sale = product.get("salePriceU")
    full = product.get("priceU")
    if sale is not None or full is not None:
        return _kop_to_rub(full or sale or 0), _kop_to_rub(sale) if sale else None
    sizes = product.get("sizes") or []
    if sizes:
        price = sizes[0].get("price") or {}
        basic = price.get("basic")
        product_p = price.get("product")
        if basic or product_p:
            return _kop_to_rub(basic or product_p), _kop_to_rub(product_p) if product_p else None
    return 0.0, None


def _wb_image_url(nm_id: int, size: str = "c516x688") -> str | None:
    """Build a public WB CDN image URL from nm_id.

    WB catalog responses usually include only nm_id and image count, while the
    image CDN path is deterministic: basket/vol/part/nm/images/<size>/1.webp.
    """
    if nm_id <= 0:
        return None
    vol = nm_id // 100000
    part = nm_id // 1000
    basket_limits = [
        (143, 1), (287, 2), (431, 3), (719, 4), (1007, 5),
        (1061, 6), (1115, 7), (1169, 8), (1313, 9), (1601, 10),
        (1655, 11), (1919, 12), (2045, 13), (2189, 14), (2405, 15),
        (2623, 16), (2837, 17), (3051, 18), (3265, 19), (3489, 20),
        (3703, 21), (3917, 22), (4131, 23), (4345, 24), (4559, 25),
    ]
    basket = 1
    for limit, number in basket_limits:
        if vol <= limit:
            basket = number
            break
    else:
        # New WB baskets keep growing in roughly 214-volume buckets.
        basket = 26 + max(0, (vol - 4560) // 214)
    return f"https://basket-{basket:02d}.wbbasket.ru/vol{vol}/part{part}/{nm_id}/images/{size}/1.webp"


def _product_to_analog(product: dict) -> AnalogSku:
    nm = product.get("id") or product.get("nmId") or 0
    nm_int = int(nm)
    full_p, sale_p = _extract_price(product)
    feedbacks = int(product.get("feedbacks") or 0)
    stocks = product.get("totalQuantity")
    if stocks is None:
        stocks = sum(
            (q.get("qty") or 0)
            for size in (product.get("sizes") or [])
            for q in (size.get("stocks") or [])
        )
    return AnalogSku(
        nm_id=nm_int,
        name=product.get("name") or "",
        brand=product.get("brand"),
        image=_wb_image_url(nm_int),
        price=full_p,
        sale_price=sale_p,
        feedbacks=feedbacks,
        rating=float(product["reviewRating"]) if product.get("reviewRating") is not None else None,
        stocks=int(stocks) if stocks is not None else None,
        sales_30d_est=estimate_monthly_sales(nm_int, feedbacks),
        url=f"https://www.wildberries.ru/catalog/{nm}/detail.aspx",
    )


def _product_to_analog_safe(product: dict) -> AnalogSku | None:
    try:
        a = _product_to_analog(product)
        return a if a.nm_id else None
    except Exception as e:  # noqa: BLE001
        logger.warning("не удалось разобрать карточку товара: %s", e)
        return None


def _search_params(query: str, page: int = 1) -> dict:
    return {
        "ab_testing": "false",
        "appType": "1",
        "curr": "rub",
        "dest": DEST,
        "page": str(page),
        "query": query,
        "resultset": "catalog",
        "sort": "popular",
        "spp": "30",
        "suppressSpellcheck": "false",
    }


def _catalog_params(category_query: str, page: int = 1) -> dict:
    """Параметры для catalog.wb.ru. category_query — это «query»-поле узла меню."""
    base = {
        "ab_testing": "false",
        "appType": "1",
        "curr": "rub",
        "dest": DEST,
        "page": str(page),
        "sort": "popular",
        "spp": "30",
    }
    for kv in category_query.split("&"):
        if "=" in kv:
            k, v = kv.split("=", 1)
            base[k] = v
    return base


class WBPublicProvider:
    def __init__(self, timeout: float | None = None):
        self.timeout = timeout or settings.request_timeout_seconds
        self._menu: list[dict] | None = None
        self._menu_lock = asyncio.Lock()

    async def _client(self, proxy_url: str | None = None) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout),
            headers=BROWSER_HEADERS,
            follow_redirects=True,
            proxy=proxy_url,
        )

    async def _get_json(self, client: httpx.AsyncClient, url: str, params: dict) -> dict | None:
        """GET с ретраями на 429/5xx. Если запрос идёт через прокси-порт,
        мы передаём его как атрибут client._port — чтобы fail/success помечались.
        Если без прокси (port=None) — просто ретрай."""
        port = getattr(client, "_proxy_port", None)
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=0.6, min=0.5, max=3),
            retry=retry_if_exception_type((httpx.HTTPError, httpx.TransportError)),
            reraise=False,
        ):
            with attempt:
                r = await client.get(url, params=params)
                if r.status_code in (429, 502, 503, 504):
                    if port is not None:
                        wb_proxy.mark_burned(port)
                    raise httpx.HTTPError(f"retryable status {r.status_code}")
                r.raise_for_status()
                if port is not None:
                    wb_proxy.mark_good(port)
                try:
                    return r.json()
                except ValueError:
                    return None
        return None

    async def _make_client(self) -> httpx.AsyncClient:
        """Создать клиент с (опционально) подбором прокси-порта.
        Порт прикрепляется к клиенту как _proxy_port для отслеживания."""
        port = wb_proxy.pick_port()
        proxy_url = wb_proxy.proxy_url_for(port) if port is not None else None
        client = await self._client(proxy_url=proxy_url)
        client._proxy_port = port  # type: ignore[attr-defined]
        return client

    async def _load_menu(self) -> list[dict]:
        if self._menu is not None:
            return self._menu
        async with self._menu_lock:
            if self._menu is not None:
                return self._menu
            async with await self._make_client() as c:
                r = await c.get(MENU_URL)
                r.raise_for_status()
                self._menu = r.json()
                return self._menu

    @staticmethod
    def _walk_menu(nodes: list[dict], names: list[str]) -> dict | None:
        if not names:
            return None
        first = names[0].strip().lower()
        for n in nodes:
            if (n.get("name") or "").strip().lower() == first:
                if len(names) == 1:
                    return n
                return WBPublicProvider._walk_menu(n.get("childs") or [], names[1:])
        return None

    @staticmethod
    def _node_has_valid_shard(node: dict) -> bool:
        shard = (node.get("shard") or "").strip()
        return bool(shard) and shard != "blackhole" and bool(node.get("query"))

    @classmethod
    def _descend_to_shard(cls, node: dict) -> dict | None:
        """Если узел сам имеет валидный shard — возвращаем его.
        Иначе рекурсивно спускаемся в первый child с валидным shard."""
        if cls._node_has_valid_shard(node):
            return node
        for child in (node.get("childs") or []):
            descended = cls._descend_to_shard(child)
            if descended is not None:
                return descended
        return None

    @classmethod
    def _find_first_with_shard(cls, nodes: list[dict], target: str) -> dict | None:
        """DFS по меню по имени узла; возвращает первый узел с валидным shard.
        Если совпавший узел сам без shard ('Кружки' может быть навигационным
        с shard='blackhole'), спускаемся в его childs."""
        for n in nodes:
            if cls._menu_name_matches(n.get("name") or "", target):
                descended = cls._descend_to_shard(n)
                if descended is not None:
                    return descended
            kid = cls._find_first_with_shard(n.get("childs") or [], target)
            if kid is not None:
                return kid
        return None

    @staticmethod
    def _menu_name_matches(menu_name: str, target: str) -> bool:
        menu_l = " ".join((menu_name or "").strip().lower().split())
        target_l = " ".join((target or "").strip().lower().split())
        if not menu_l or not target_l:
            return False
        if menu_l == target_l:
            return True
        menu_tokens = menu_l.split()
        target_tokens = target_l.split()
        # subjects.json часто даёт единственное число («Ручка»), а меню WB —
        # множественное («Ручки»). Но нельзя матчить многословный target только
        # по первому слову: «Коляски для кукол» иначе превращались во взрослые
        # «Коляски» и эталон уходил в другую нишу.
        if len(target_tokens) != len(menu_tokens):
            if len(target_tokens) == 1 and len(menu_tokens) == 1:
                pass
            else:
                return False
        pairs = zip(menu_tokens, target_tokens)
        return all(WBPublicProvider._word_stem_matches(menu_word, target_word) for menu_word, target_word in pairs)

    @staticmethod
    def _word_stem_matches(menu_word: str, target_word: str) -> bool:
        stem_len = min(6, max(4, len(target_word) - 1))
        stem = target_word[:stem_len]
        return len(stem) >= 4 and menu_word.startswith(stem)

    @staticmethod
    def _norm_menu_name(value: str | None) -> str:
        return " ".join((value or "").strip().lower().split())

    @classmethod
    def _collect_shards(cls, nodes: list[dict]) -> list[str]:
        shards: list[str] = []

        def walk(items: list[dict]) -> None:
            for item in items:
                shard = (item.get("shard") or "").strip()
                if shard and shard != "blackhole" and shard not in shards:
                    shards.append(shard)
                walk(item.get("childs") or [])

        walk(nodes)
        return shards

    async def _subject_candidate_shards(self, parent_name: str | None = None) -> list[str]:
        parent_norm = self._norm_menu_name(parent_name)
        candidates: list[str] = list(SUBJECT_SHARD_HINTS.get(parent_norm, ()))
        candidates.extend(DEFAULT_SHARDS)

        menu = await self._load_menu()
        root_names = SUBJECT_PARENT_ROOT_ALIASES.get(parent_norm, (parent_name,) if parent_name else ())
        roots = {self._norm_menu_name(n.get("name")): n for n in menu}
        for root_name in root_names:
            root = roots.get(self._norm_menu_name(root_name))
            if root is None:
                continue
            candidates.extend(self._collect_shards([root]))

        deduped: list[str] = []
        for shard in candidates:
            if shard and shard not in deduped:
                deduped.append(shard)
        return deduped

    async def lookup_category(self, wb_path: str) -> dict | None:
        """Найти узел WB-меню для категории.

        Стратегии в порядке убывания точности:
          1) пройти по полному пути 'A/B/C' дословно (точное совпадение всей иерархии);
          2) DFS по последнему сегменту ('C') — ищем ЛЮБОЙ узел с этим именем,
             у которого shard валидный (не 'blackhole').
          3) укоротить хвост 'A/B' → 'A' (последний резервный путь).

        Шаг 2 нужен, потому что subjects.json и main-menu имеют РАЗНЫЕ иерархии:
        subject 'Рюкзаки' имеет parent 'Аксессуары' (из subjects), но в меню он лежит
        в 'Аксессуары > Сумки и аксессуары > Сумки и рюкзаки > Рюкзаки'.
        """
        if not wb_path:
            return None
        menu = await self._load_menu()
        parts = [p.strip() for p in wb_path.split("/") if p.strip()]
        # 1: только полный путь. Нельзя сначала соглашаться на родителя:
        # «Детям/.../Игрушки/Конструктор» иначе превращается в широкие
        # «Игрушки» и даёт неверный/пустой эталон.
        node = self._walk_menu(menu, parts)
        if node and (node.get("shard") or "").strip() not in ("", "blackhole"):
            return node

        # 2: DFS по последнему сегменту
        if parts:
            node = self._find_first_with_shard(menu, parts[-1])
            if node is not None:
                return node

        # 3: последний резервный путь — родительские префиксы.
        path = list(parts[:-1])
        while path:
            node = self._walk_menu(menu, path)
            if node and (node.get("shard") or "").strip() not in ("", "blackhole"):
                return node
            path = path[:-1]
        return None

    async def search_analogs(
        self,
        query: str,
        wb_category_path: str | None = None,
        price_min: float | None = None,
        price_max: float | None = None,
        limit: int = 30,
    ) -> list[AnalogSku]:
        params = _search_params(query)
        if price_min is not None or price_max is not None:
            lo = int((price_min or 0) * 100)
            hi = int((price_max or 0) * 100) if price_max else 999_999_999_00
            params["priceU"] = f"{lo};{hi}"
        async with await self._make_client() as c:
            data = None
            try:
                data = await self._get_json(c, SEARCH_URL, params)
            except Exception as e:
                logger.warning("поиск WB не сработал: %s", e)
            products = ((data or {}).get("data") or {}).get("products") or []
            if products:
                return [_product_to_analog(p) for p in products[:limit]]
            # Search вернул только metadata? Достанем catalog_value и дёрнем catalog.
            meta = (data or {}).get("metadata") or {}
            cv = meta.get("catalog_value")
            if cv:
                products = await self._catalog_by_value(c, cv, limit=limit, price_min=price_min, price_max=price_max)
                if products:
                    return products
            # Совсем ничего — резервный путь на категорию.
            if wb_category_path:
                return await self._top_n_inner(c, wb_category_path, limit)
            return []

    async def _catalog_by_value(
        self,
        client: httpx.AsyncClient,
        catalog_value: str,
        limit: int = 30,
        price_min: float | None = None,
        price_max: float | None = None,
    ) -> list[AnalogSku]:
        """Достать карточки по catalog_value (preset=..., subject=..., cat=...).
        Пробуем несколько шардов, потому что shard в metadata не приходит."""
        params = {
            "ab_testing": "false",
            "appType": "1",
            "curr": "rub",
            "dest": DEST,
            "page": "1",
            "sort": "popular",
            "spp": "30",
        }
        for kv in catalog_value.split("&"):
            if "=" in kv:
                k, v = kv.split("=", 1)
                params[k] = v
        if price_min is not None or price_max is not None:
            lo = int((price_min or 0) * 100)
            hi = int((price_max or 0) * 100) if price_max else 999_999_999_00
            params["priceU"] = f"{lo};{hi}"
        for shard in DEFAULT_SHARDS:
            try:
                data = await self._get_json(client, CATALOG_URL_FMT.format(shard=shard), params)
            except Exception:
                continue
            products = ((data or {}).get("data") or {}).get("products") or []
            if products:
                return [_product_to_analog(p) for p in products[:limit]]
        return []

    async def _top_n_inner(self, client: httpx.AsyncClient, wb_category_path: str, limit: int) -> list[AnalogSku]:
        node = await self.lookup_category(wb_category_path)
        if not node:
            logger.warning("категория WB не найдена: %s", wb_category_path)
            return []
        shard = node.get("shard")
        query_str = node.get("query")
        if not shard or not query_str:
            children = node.get("childs") or []
            if children:
                node = children[0]
                shard = node.get("shard")
                query_str = node.get("query")
        if not shard or not query_str:
            return []
        url = CATALOG_URL_FMT.format(shard=shard)
        params = _catalog_params(query_str)
        try:
            data = await self._get_json(client, url, params)
        except Exception as e:
            logger.warning("каталог WB не сработал для %s (shard=%s): %s", wb_category_path, shard, e)
            data = None
        products = ((data or {}).get("data") or {}).get("products") or []
        if not products:
            # Резервный путь: shard ещё неактуален → пробуем дефолтные шарды.
            products_models = await self._catalog_by_value(client, query_str, limit=limit)
            if products_models:
                return products_models
        return [_product_to_analog(p) for p in products[:limit]]

    async def top_n_in_category(self, wb_category_path: str, limit: int = 100,
                                *, subject_id: int | None = None) -> list[AnalogSku]:
        """Топ-N WB-категории через shard дерева меню. По умолчанию запрашиваем
        несколько страниц (до 3), чтобы получить распределение «середины топа»,
        а не только лидеров (медиана top-30 — это верхушка, сравнение с ней
        даёт ложный RED почти всегда)."""
        from app.services import wb_http

        node = await self.lookup_category(wb_category_path)
        if not node:
            logger.warning("категория не найдена в меню WB: %s", wb_category_path)
            return []
        shard = node.get("shard")
        query_str = node.get("query")
        if not shard or not query_str or shard == "blackhole":
            return []

        base_params = {
            "ab_testing": "false",
            "appType": "1",
            "curr": "rub",
            "dest": DEST,
            "sort": "popular",
            "spp": "30",
        }
        for kv in query_str.split("&"):
            if "=" in kv:
                k, v = kv.split("=", 1)
                base_params[k] = v

        url = CATALOG_URL_FMT.format(shard=shard)
        collected: list[dict] = []
        pages_to_fetch = max(1, (limit + 99) // 100)  # 100 SKU на страницу WB
        for page in range(1, pages_to_fetch + 1):
            params = dict(base_params, page=str(page))
            data = await wb_http.get_json(url, params, timeout=settings.request_timeout_seconds)
            products = (data or {}).get("products") or ((data or {}).get("data") or {}).get("products") or []
            if not products:
                break
            collected.extend(products)
            if len(collected) >= limit:
                break
        if not collected:
            logger.warning("каталог WB не вернул товары для %s (shard=%s, %s)",
                           wb_category_path, shard, query_str)
            return []
        if subject_id:
            # menu-tree shard иногда указывает на ЧУЖУЮ категорию (напр. канц-путь →
            # одежда). Если почти ничего не совпало с нужным subjectId — это не наш
            # эталон: отдаём пусто, движок уйдёт в резервный путь по subjectId.
            same = [p for p in collected if int(p.get("subjectId") or 0) == int(subject_id)]
            if len(same) < max(1, int(0.2 * len(collected))):
                logger.warning("shard каталога для %s отдал чужие категории (%d/%d совпали с %s) — отбрасываем",
                               wb_category_path, len(same), len(collected), subject_id)
                return []
            collected = same
        return [_product_to_analog(p) for p in collected[:limit]]

    async def top_n_by_subject(
        self,
        subject_id: int,
        limit: int = 30,
        *,
        parent_name: str | None = None,
    ) -> list[AnalogSku]:
        """Топ-N карточек в WB-категории по subjectId.
        Идём через requests (wb_http) — httpx тут регулярно ловит 403."""
        from app.services import wb_http

        params = {
            "ab_testing": "false",
            "appType": "1",
            "curr": "rub",
            "dest": DEST,
            "page": "1",
            "sort": "popular",
            "spp": "30",
            "subject": str(subject_id),
        }
        for shard in await self._subject_candidate_shards(parent_name):
            data = await wb_http.get_json(
                CATALOG_URL_FMT.format(shard=shard),
                params,
                timeout=settings.request_timeout_seconds,
            )
            # v4: products на верхнем уровне; v9: data.products
            products = (data or {}).get("products") or ((data or {}).get("data") or {}).get("products") or []
            if products:
                # некоторые шарды игнорят subject= и отдают свою категорию —
                # берём только реально совпавшие по subjectId
                same = [p for p in products if int(p.get("subjectId") or 0) == int(subject_id)]
                if same:
                    return [_product_to_analog(p) for p in same[:limit]]
        return []
