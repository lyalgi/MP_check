"""Клиент MPStats API (Wildberries).

Документация эндпоинтов — навык mpstats-io. Здесь только то, что нужно пайплайну v2:

    items/{nm}/full       — детали карточки + статистика за период (цена, продажи,
                            категория, % выкупа, дата создания, фото, комиссия)
    items/{nm}/by_period  — дневной ряд за период (для сезонности/тренда)
    similar/items         — похожие товары по nm (каталожный метод WB)
    category/items        — товары категории (для перцентиля спроса)
    category/list         — дерево категорий (path для category/items)

Аутентификация: заголовок `X-Mpstats-TOKEN`. Токен НЕ хранится в коде — берётся из
переменной окружения `MPSTATS_TOKEN`, иначе из файла `API.md` в корне проекта.

Подводные камни, найденные при разведке API (2026-06):
  • `d2` должна быть строго РАНЬШЕ сегодняшней даты, иначе 400.
  • API отдаёт ПРОДАЖИ (выкупы) в `sales`; отдельного поля «заказы» в товарных
    эндпоинтах нет. % выкупа лежит в `subject.purchase.purchase` (можно оценить
    заказы ≈ продажи / выкуп).
  • `similar`/`category` для мёртвых (без остатка) карточек и для будущих дат
    возвращают пусто; `category/list` иногда отдаёт временную серверную ошибку.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import quote

import requests

logger = logging.getLogger(__name__)

BASE = "https://mpstats.io/api/analytics/v1/wb"
LIMITS_URL = "https://mpstats.io/api/user/report_api_limit"
CATEGORIES_URL = "https://mpstats.io/api/wb/get/categories"  # дерево категорий (GET, работает)
TIMEOUT = 30.0
_CACHE_DIR = Path(os.environ.get("SAOL2_CACHE", Path(__file__).resolve().parent / ".cache"))
_CACHE_TTL = float(os.environ.get("SAOL2_CACHE_TTL", 24 * 3600))  # сутки


# ─────────────────────────── токен ──────────────────────────────────────────
def load_token() -> str:
    """Токен из env MPSTATS_TOKEN или из API.md в корне проекта. В код не зашит."""
    t = (os.environ.get("MPSTATS_TOKEN") or "").strip()
    if t:
        return t
    for p in (Path(__file__).resolve().parents[2] / "API.md", Path("API.md")):
        if p.exists():
            val = p.read_text(encoding="utf-8").strip()
            if val:
                return val
    raise RuntimeError("Не найден токен: задайте MPSTATS_TOKEN или положите API.md в корень проекта")


# ─────────────────────────── даты ───────────────────────────────────────────
def year_window(end: date | None = None) -> tuple[str, str]:
    """Годовое окно [d1, d2]. d2 = вчера (API требует дату строго до сегодня)."""
    d2 = (end or date.today()) - timedelta(days=1)
    d1 = d2 - timedelta(days=365)
    return d1.isoformat(), d2.isoformat()


def trend_window(end: date | None = None, extra: int = 90) -> tuple[str, str]:
    """Окно для YoY-тренда: 365 + `extra` дней (нужно сравнить последние `extra` дней
    с теми же днями год назад). d2 = вчера."""
    d2 = (end or date.today()) - timedelta(days=1)
    d1 = d2 - timedelta(days=365 + extra)
    return d1.isoformat(), d2.isoformat()


# ─────────────────────────── кэш ────────────────────────────────────────────
def _cache_path(key: str) -> Path:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return _CACHE_DIR / (hashlib.sha1(key.encode("utf-8")).hexdigest() + ".json")


def _cache_get(key: str):
    p = _cache_path(key)
    if p.exists() and (time.time() - p.stat().st_mtime) < _CACHE_TTL:
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            return None
    return None


def _cache_put(key: str, value) -> None:
    try:
        _cache_path(key).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


# ─────────────────────────── HTTP ───────────────────────────────────────────
class MPStats:
    def __init__(self, token: str | None = None, use_cache: bool = True):
        self.token = token or load_token()
        self.use_cache = use_cache
        self._session = requests.Session()
        self._session.headers.update({
            "X-Mpstats-TOKEN": self.token,
            "Content-Type": "application/json",
        })

    # низкоуровневый запрос с кэшем и единым разбором ошибок MPStats
    def _request(self, method: str, url: str, *, body: dict | None = None):
        cache_key = f"{method} {url} {json.dumps(body, sort_keys=True) if body else ''}"
        if self.use_cache:
            hit = _cache_get(cache_key)
            if hit is not None:
                return hit
        try:
            if method == "GET":
                r = self._session.get(url, timeout=TIMEOUT)
            else:
                r = self._session.post(url, data=json.dumps(body or _DEFAULT_BODY), timeout=TIMEOUT)
        except requests.RequestException as e:
            logger.warning("MPStats транспорт упал: %s", e)
            return None
        try:
            data = r.json()
        except ValueError:
            logger.warning("MPStats вернул не JSON (%s): %s", r.status_code, r.text[:200])
            return None
        # MPStats кладёт ошибку в {"message": ..., "errors": ...}
        if isinstance(data, dict) and "message" in data and "data" not in data:
            logger.warning("MPStats ошибка: %s", data.get("message"))
            return None
        if self.use_cache:
            _cache_put(cache_key, data)
        return data

    # ── товар ────────────────────────────────────────────────────────────────
    def item_full(self, nm: int, d1: str | None = None, d2: str | None = None) -> dict | None:
        """Детали + статистика за период: цена, продажи, категория, % выкупа, фото, дата."""
        if d1 is None or d2 is None:
            d1, d2 = year_window()
        return self._request("GET", f"{BASE}/items/{nm}/full?d1={d1}&d2={d2}")

    def item_by_period(self, nm: int, d1: str | None = None, d2: str | None = None) -> list | None:
        """Дневной ряд за период (sales, final_price, revenue по дням)."""
        if d1 is None or d2 is None:
            d1, d2 = year_window()
        out = self._request("GET", f"{BASE}/items/{nm}/by_period?d1={d1}&d2={d2}")
        return out if isinstance(out, list) else None

    # ── похожие ────────────────────────────────────────────────────────────────
    def similar(self, nm: int, d1: str | None = None, d2: str | None = None,
                limit: int = 30, kind: str = "similar") -> list[dict]:
        """Похожие товары по nm. kind: similar (каталог WB) | identical (AI) | identical_wb (AI WB)."""
        if d1 is None or d2 is None:
            d1, d2 = year_window()
        seg = {"similar": "similar", "identical": "identical", "identical_wb": "identical_wb"}.get(kind, "similar")
        url = f"{BASE}/{seg}/items?d1={d1}&d2={d2}&path={quote(str(nm))}&fbs=0"
        data = self._request("POST", url, body=_body(limit))
        return (data or {}).get("data") or []

    # ── категория ──────────────────────────────────────────────────────────────
    def category_items(self, path: str, d1: str | None = None, d2: str | None = None,
                       limit: int = 100) -> list[dict]:
        """Товары категории (для распределения/перцентиля)."""
        if d1 is None or d2 is None:
            d1, d2 = year_window()
        url = f"{BASE}/category/items?d1={d1}&d2={d2}&path={quote(path)}"
        data = self._request("POST", url, body=_body(limit))
        return (data or {}).get("data") or []

    def subject_items(self, subject_id: int, d1: str | None = None, d2: str | None = None,
                      limit: int = 200) -> list[dict]:
        """Товары категории по subject_id (надёжнее category/items — без угадывания пути)."""
        if d1 is None or d2 is None:
            d1, d2 = year_window()
        url = f"{BASE}/subject/items?d1={d1}&d2={d2}&path={int(subject_id)}&fbs=0"
        data = self._request("POST", url, body=_body(limit))
        return (data or {}).get("data") or []

    def category_list(self) -> list[dict]:
        """Дерево категорий WB: список {url, name, path}. Эндпоинт вне /analytics
        (тот, что в /analytics — `category/list` — требует POST и отдаёт 405 на GET)."""
        data = self._request("GET", CATEGORIES_URL)
        if isinstance(data, list):
            return data
        return (data or {}).get("data") or []

    # ── служебное ──────────────────────────────────────────────────────────────
    def limits(self) -> dict | None:
        return self._request("GET", LIMITS_URL)


_DEFAULT_BODY = {"startRow": 0, "endRow": 100, "filterModel": {}, "sortModel": [{"colId": "sales", "sort": "desc"}]}


def _body(limit: int) -> dict:
    return {"startRow": 0, "endRow": int(limit), "filterModel": {},
            "sortModel": [{"colId": "sales", "sort": "desc"}]}
