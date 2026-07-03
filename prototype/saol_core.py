#!/usr/bin/env python3
"""
SAOL — автономная оценка ликвидности товара на Wildberries + прогноз закупа.

Один файл. Без БД и веб-сервера. Для аналитиков и разработчиков: запустил —
получил вердикт по фото товара. Та же логика, что в боевом сервисе (app/),
собранная компактно в один модуль.

УСТАНОВКА
    pip install requests cryptography

ЗАПУСК
    python saol_core.py photo.jpg --price 350
    python saol_core.py --nm 143489486 --price 350        # по артикулу WB
    python saol_core.py photo.jpg --price 350 --json        # машинный вывод

ДАННЫЕ
    Рядом ожидается coefficient.json (доля Разноторга по категориям, K).
    Если файла нет — прогноз закупа не считается, остальное работает.

АЛГОРИТМ (главный вопрос: «востребован ли ЭТОТ товар на WB и сколько брать»)
    1. Visual search WB по фото → массив артикулов-аналогов.
    2. Детали карточек (card.wb.ru) → цена, отзывы, остаток.
    3. Фильтр: отзывов >= MIN_FEEDBACKS (отсечь шум).
    4. Категория: взвешенное голосование по subjectId (вес = отзывы).
    5. Benchmark: топ-100 этой категории на WB.
    6. Балл ликвидности 0..100 = перцентиль товара по продажам в нише
       + факторы (спрос/маржа/наличие конкуренции).
    7. Закуп = WB-спрос аналога (шт/год) × доля Разноторга K.

ОГОВОРКИ
    • Продажи WB не публикуются — оцениваются по отзывам с поправкой на возраст
      карточки (по nm_id). Это baseline, не точные продажи.
    • Visual search — закрытый эндпоинт мобильного приложения WB (реверс из
      официального расширения). Может меняться.
"""
from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import os
import re
import statistics
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# ─────────────────────────── Константы ──────────────────────────────────────
DEST = "-1257786"  # регион (Москва)
MIN_FEEDBACKS = 10
WB_UA = "WBClient/9.1.4 (com.wildberries.ru;build:202; iOS 17.0.0) Alamofire/5.6.1"
CURL_UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148"
HEADERS = {"User-Agent": WB_UA, "Accept": "*/*"}
TIMEOUT = 30.0

UPLOAD_URL = "https://search-by-photo.wb.ru/uploadsearch"
CARD_URL = "https://card.wb.ru/cards/v4/detail"
CATALOG_FMT = "https://catalog.wb.ru/catalog/{shard}/v4/catalog"
MENU_URL = "https://static-basket-01.wbbasket.ru/vol0/data/main-menu-ru-ru-v3.json"
SUBJECTS_URL = "https://static-basket-01.wbbasket.ru/vol0/data/subjects.json"

# Подпись для visual search (реверс из chrome-extension WB Image Search)
_SALT = b"b723375b3aac60afa239c149"
_ENCODED_KEY = bytes([
    84, 7, 81, 11, 3, 86, 84, 91, 82, 0, 85, 86, 83, 3, 83, 94, 4, 10, 2, 15, 6, 3, 81, 90,
    7, 5, 7, 4, 1, 82, 5, 87, 4, 85, 89, 80, 82, 0, 89, 7, 85, 87, 5, 12, 87, 6, 82, 9,
    90, 2, 84, 85, 2, 86, 84, 1, 1, 84, 83, 83, 84, 7, 82, 94,
])

FEEDBACK_TO_SALES = 20.0          # 1 отзыв ≈ 20 продаж (baseline)
MAX_SALES_PER_MONTH = 5000.0
_NM_AGE = [
    (1, datetime(2017, 1, 1, tzinfo=timezone.utc)),
    (50_000_000, datetime(2019, 1, 1, tzinfo=timezone.utc)),
    (100_000_000, datetime(2020, 1, 1, tzinfo=timezone.utc)),
    (150_000_000, datetime(2021, 6, 1, tzinfo=timezone.utc)),
    (200_000_000, datetime(2023, 1, 1, tzinfo=timezone.utc)),
    (250_000_000, datetime(2024, 6, 1, tzinfo=timezone.utc)),
    (300_000_000, datetime(2025, 6, 1, tzinfo=timezone.utc)),
    (350_000_000, datetime(2026, 4, 1, tzinfo=timezone.utc)),
]
_CACHE = Path(os.environ.get("SAOL_CACHE", "/tmp/saol_cache"))
_CACHE.mkdir(parents=True, exist_ok=True)


# ─────────────────────────── HTTP-помощники ─────────────────────────────────
def _proxies() -> dict | None:
    tpl = os.environ.get("PROXY_URL", "").strip()
    if not tpl:
        return None
    import random
    lo, hi = 10000, 10099
    rng = os.environ.get("PROXY_PORTS")
    if rng and "-" in rng:
        a, b = rng.split("-", 1)
        lo, hi = int(a), int(b)
    url = tpl.replace("{port}", str(random.randint(lo, hi)))
    return {"http": url, "https": url}


def _get_json(url: str, params: dict) -> dict | None:
    """GET JSON через requests; при анти-боте 403/429 — fallback на системный curl."""
    proxies = _proxies()
    try:
        r = requests.get(url, params=params, headers=HEADERS, proxies=proxies, timeout=TIMEOUT)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    # curl-fallback (другой TLS-fingerprint)
    import shutil
    import subprocess
    curl = shutil.which("curl")
    if not curl:
        return None
    prepared = requests.Request("GET", url, params=params).prepare()
    try:
        res = subprocess.run(
            [curl, "-sS", "--compressed", "--max-time", str(int(TIMEOUT)), "-A", CURL_UA, prepared.url],
            capture_output=True, text=True, timeout=TIMEOUT + 2,
        )
        if res.returncode == 0:
            return json.loads(res.stdout)
    except Exception:
        return None
    return None


# ─────────────────────────── Visual search ──────────────────────────────────
def _signature(message: str) -> str:
    key = bytes(_ENCODED_KEY[i] ^ _SALT[i % len(_SALT)] for i in range(len(_ENCODED_KEY)))
    aes_key = hashlib.sha256(key).digest()
    payload = message.encode("utf-8")
    out = ""
    for _ in range(3):
        iv = os.urandom(16)
        enc = Cipher(algorithms.AES(aes_key), modes.CTR(iv)).encryptor()
        ct = enc.update(payload) + enc.finalize()
        out = base64.b64encode(iv + ct).decode("ascii")
        payload = out.encode("utf-8")
    return out


def visual_search(image_bytes: bytes) -> list[int]:
    """Фото → артикулы визуально похожих карточек WB."""
    for attempt in range(2):
        ruid = str(uuid.uuid4())
        headers = {
            "Signature": _signature(f"RequestUUID:{ruid}"),
            "RequestUUID": ruid,
            "test-properties": "ab_testing=false",
            "userid": "0",
        }
        try:
            r = requests.post(UPLOAD_URL, headers=headers,
                              files={"image": ("photo.jpg", image_bytes, "image/jpeg")},
                              timeout=TIMEOUT)
            if r.status_code == 200:
                data = r.json()
                if data.get("status") == "OK":
                    return [int(i["im_name"]) for i in (data.get("result") or []) if "im_name" in i]
        except Exception:
            pass
    return []


# ─────────────────────────── WB-данные ──────────────────────────────────────
def fetch_cards(nm_ids: list[int]) -> list[dict]:
    out: list[dict] = []
    for i in range(0, len(nm_ids), 50):
        chunk = nm_ids[i:i + 50]
        data = _get_json(CARD_URL, {
            "appType": "1", "curr": "rub", "dest": DEST, "spp": "30",
            "nm": ";".join(str(n) for n in chunk),
        })
        if data:
            out.extend(data.get("products") or (data.get("data") or {}).get("products") or [])
    return out


def estimate_monthly_sales(nm_id: int, feedbacks: int) -> float:
    """Оценка продаж/мес: отзывы / возраст карточки × коэффициент."""
    if feedbacks <= 0:
        return 0.0
    now = datetime.now(timezone.utc)
    if nm_id <= _NM_AGE[0][0]:
        created = _NM_AGE[0][1]
    elif nm_id >= _NM_AGE[-1][0]:
        (pn, pd), (ln, ld) = _NM_AGE[-2], _NM_AGE[-1]
        rate = (ln - pn) / max(1.0, (ld - pd).days / 30.4)
        created = ld + timedelta(days=int((nm_id - ln) / max(1.0, rate) * 30.4))
    else:
        created = _NM_AGE[-1][1]
        for (na, da), (nb, db_) in zip(_NM_AGE, _NM_AGE[1:]):
            if na <= nm_id < nb:
                frac = (nm_id - na) / max(1, nb - na)
                created = da + timedelta(days=int((db_ - da).days * frac))
                break
    age_m = max(1.0, (now - created).days / 30.4)
    return round(min(feedbacks / age_m * FEEDBACK_TO_SALES, MAX_SALES_PER_MONTH), 1)


def _price(p: dict) -> float:
    for k in ("salePriceU", "priceU"):
        if p.get(k):
            try:
                return round(float(p[k]) / 100.0, 2)
            except (TypeError, ValueError):
                pass
    sizes = p.get("sizes") or []
    if sizes:
        pr = sizes[0].get("price") or {}
        v = pr.get("product") or pr.get("basic")
        if v:
            return round(float(v) / 100.0, 2)
    return 0.0


def card_to_analog(p: dict) -> dict:
    nm = int(p.get("id") or p.get("nmId") or 0)
    fb = int(p.get("feedbacks") or 0)
    stocks = p.get("totalQuantity")
    if stocks is None:
        stocks = sum((q.get("qty") or 0) for s in (p.get("sizes") or []) for q in (s.get("stocks") or []))
    return {
        "nm_id": nm, "name": p.get("name") or "", "price": _price(p),
        "feedbacks": fb, "subject_id": p.get("subjectId"),
        "stocks": int(stocks or 0), "sales_30d": estimate_monthly_sales(nm, fb),
    }


# ─────────────────────────── Справочники ────────────────────────────────────
def load_subjects() -> dict[int, dict]:
    cache = _CACHE / "subjects.json"
    if cache.exists():
        raw = cache.read_bytes()
    else:
        r = requests.get(SUBJECTS_URL, timeout=20)
        raw = r.content
        cache.write_bytes(raw)
    try:
        items = json.loads(raw)
    except ValueError:
        items = json.loads(gzip.decompress(raw))
    return {int(it["id"]): {"name": it.get("name"), "parent_id": it.get("parentId")}
            for it in items if it.get("id")}


def subject_path(subjects: dict[int, dict], sid: int | None) -> tuple[str | None, str | None]:
    if not sid or sid not in subjects:
        return None, None
    s = subjects[sid]
    parent = subjects.get(s.get("parent_id") or -1, {}).get("name")
    return s.get("name"), parent


def _menu_node(nodes: list, target: str) -> dict | None:
    """DFS меню: первый узел name==target с рабочим shard (не blackhole)."""
    t = target.strip().lower()
    for n in nodes:
        if (n.get("name") or "").strip().lower() == t:
            shard, query = (n.get("shard") or "").strip(), n.get("query")
            if shard and shard != "blackhole" and query:
                return n
            for ch in (n.get("childs") or []):
                d = _menu_node([ch], target)
                if d:
                    return d
        d = _menu_node(n.get("childs") or [], target)
        if d:
            return d
    return None


def top_category(subject_name: str | None, limit: int = 100) -> list[dict]:
    if not subject_name:
        return []
    cache = _CACHE / "menu.json"
    if cache.exists():
        menu = json.loads(cache.read_text())
    else:
        r = requests.get(MENU_URL, headers=HEADERS, timeout=20)
        menu = r.json()
        cache.write_text(json.dumps(menu))
    node = _menu_node(menu, subject_name)
    if not node:
        return []
    params = {"appType": "1", "curr": "rub", "dest": DEST, "sort": "popular", "spp": "30"}
    for kv in (node["query"] or "").split("&"):
        if "=" in kv:
            k, v = kv.split("=", 1)
            params[k] = v
    collected: list[dict] = []
    for page in range(1, (limit + 99) // 100 + 1):
        data = _get_json(CATALOG_FMT.format(shard=node["shard"]), dict(params, page=str(page)))
        prods = (data or {}).get("products") or ((data or {}).get("data") or {}).get("products") or []
        if not prods:
            break
        collected.extend(card_to_analog(p) for p in prods)
        if len(collected) >= limit:
            break
    return [c for c in collected[:limit] if c["feedbacks"] > 0]


# ─────────────────────────── Категория (голосование) ────────────────────────
def vote_category(analogs: list[dict], min_share: float = 0.6) -> tuple[int | None, float]:
    weights: dict[int, float] = {}
    for a in analogs:
        sid = a.get("subject_id")
        if sid and a["feedbacks"] > 0:
            weights[int(sid)] = weights.get(int(sid), 0) + a["feedbacks"]
    if not weights:
        return None, 0.0
    total = sum(weights.values())
    sid, w = max(weights.items(), key=lambda kv: kv[1])
    share = w / total if total else 0.0
    return (sid, share) if share >= min_share else (None, share)


# ─────────────────────────── Скоринг ────────────────────────────────────────
def percentile_rank(value: float, pop: list[float]) -> float:
    if not pop or value <= 0:
        return 0.0
    below = sum(1 for x in pop if x < value)
    equal = sum(1 for x in pop if x == value)
    return round(100.0 * (below + 0.5 * equal) / len(pop), 1)


def compute_scores(analogs: list[dict], top: list[dict], purchase_price: float) -> dict:
    a_sales = [a["sales_30d"] for a in analogs]
    t_sales = [t["sales_30d"] for t in top] or a_sales
    a_med = statistics.median(a_sales) if a_sales else 0.0
    t_med = statistics.median(t_sales) if t_sales else 0.0

    pop = {t["nm_id"]: t["sales_30d"] for t in top}
    for a in analogs:
        pop.setdefault(a["nm_id"], a["sales_30d"])
    liquidity = percentile_rank(a_med, list(pop.values()))

    sku_demand = a_med / t_med if t_med > 0 else 0.0
    demand = min(1.0, sku_demand / 0.6)

    prices = [t["price"] for t in (top or analogs) if t["price"] > 0]
    market_median = statistics.median(prices) if prices else 0.0
    markup = market_median / purchase_price if purchase_price > 0 and market_median else 0.0
    margin = (0.1 if markup < 1.3 else 0.35 if markup < 1.5 else 0.65 if markup < 2 else 0.85 if markup < 3 else 1.0) if markup else 0.5

    if a_med <= 0 or t_med <= 0:
        verdict = "UNKNOWN"
    elif demand >= 1.0 and margin >= 0.55:
        verdict = "GREEN"
    elif margin <= 0.25:
        verdict = "RED"
    elif demand >= 0.5:
        verdict = "YELLOW"
    else:
        verdict = "RED"

    return {
        "liquidity_score": liquidity, "verdict": verdict,
        "demand_score": round(demand, 2), "margin_score": round(margin, 2),
        "analog_median_sales_30d": round(a_med, 1), "top_median_sales_30d": round(t_med, 1),
        "market_price_median": round(market_median, 0) if market_median else None,
    }


# ─────────────────────────── Прогноз закупа ─────────────────────────────────
def load_coefficients(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def _stem(s: str) -> str:
    return (s or "").strip().lower()[:6]


def lookup_k(coefs: list[dict], subject_name: str | None, parent_name: str | None) -> dict | None:
    if not subject_name:
        return None
    full = f"{parent_name}/{subject_name}" if parent_name else subject_name
    for c in coefs:                                  # exact
        if c["wb_path"].lower() == full.lower():
            return c
    last = subject_name.strip().lower()
    for c in coefs:                                  # suffix
        if c["wb_path"].split("/")[-1].strip().lower() == last:
            return c
    stem = _stem(subject_name)                       # stem
    cands = [c for c in coefs if c["wb_path"].split("/")[-1].strip().lower().startswith(stem)]
    if cands:
        return max(cands, key=lambda c: c["razno_year"])
    return None


# ─────────────────────────── Главный пайплайн ───────────────────────────────
def analyze(image_bytes: bytes | None, purchase_price: float, *,
            seed_nm: int | None = None, coef_path: Path | None = None) -> dict:
    coefs = load_coefficients(coef_path or Path(__file__).parent / "data" / "coefficient.json")

    # 1. visual search
    nm_ids = visual_search(image_bytes) if image_bytes else []
    if not nm_ids and seed_nm:
        nm_ids = [seed_nm]
    if not nm_ids:
        return {"verdict": "UNKNOWN", "reason": "visual search не дал результатов"}

    # 2-3. карточки + фильтр
    raw = fetch_cards(nm_ids)
    if not raw:
        return {"verdict": "UNKNOWN", "reason": "WB не отдал детали карточек"}
    analogs_all = [card_to_analog(p) for p in raw]
    analogs = [a for a in analogs_all if a["feedbacks"] >= MIN_FEEDBACKS]
    if not analogs:
        return {"verdict": "UNKNOWN", "reason": f"нет аналогов с отзывами >= {MIN_FEEDBACKS}",
                "visual_search_count": len(nm_ids)}

    # 4. категория
    subjects = load_subjects()
    sid, vote = vote_category(analogs)
    subject_name, parent_name = subject_path(subjects, sid)
    if sid is None:
        return {"verdict": "UNKNOWN", "reason": f"visual search дал смешанные категории (vote {vote:.0%})",
                "visual_search_count": len(nm_ids), "filtered_analogs": len(analogs)}

    # 5. benchmark
    same = [a for a in analogs_all if a.get("subject_id") == sid]
    top = top_category(subject_name, limit=100) or sorted(
        [a for a in same if a["feedbacks"] > 0], key=lambda a: a["feedbacks"], reverse=True)[:30]

    # 6. скоринг
    scores = compute_scores(analogs, top, purchase_price)

    # 7. закуп = WB-спрос × K
    wb_units_year = scores["analog_median_sales_30d"] * 12.0
    k = lookup_k(coefs, subject_name, parent_name)
    recommended = None
    if k and wb_units_year > 0:
        recommended = max(0, round(wb_units_year * k["k"]))

    return {
        "verdict": scores["verdict"],
        "liquidity_score": scores["liquidity_score"],
        "wb_subject": subject_name, "wb_parent": parent_name, "subject_vote_share": round(vote, 3),
        "demand_score": scores["demand_score"], "margin_score": scores["margin_score"],
        "analog_median_sales_30d": scores["analog_median_sales_30d"],
        "top_median_sales_30d": scores["top_median_sales_30d"],
        "market_price_median": scores["market_price_median"],
        "visual_search_count": len(nm_ids), "filtered_analogs": len(analogs), "top_count": len(top),
        "wb_demand_units_year": round(wb_units_year),
        "market_share_k": k["k"] if k else None,
        "recommended_units_year": recommended,
        "examples": [{"nm_id": a["nm_id"], "name": a["name"][:50], "price": a["price"],
                      "feedbacks": a["feedbacks"]} for a in sorted(top or analogs,
                      key=lambda x: x["feedbacks"], reverse=True)[:5]],
    }


# ─────────────────────────── CLI ────────────────────────────────────────────
_VERDICT_RU = {"GREEN": "🟢 ЛИКВИДНЫЙ", "YELLOW": "🟡 ТЕСТ", "RED": "🔴 НЕЛИКВИД", "UNKNOWN": "⚪ НЕТ ДАННЫХ"}


def _print_human(r: dict) -> None:
    print(f"\n  {_VERDICT_RU.get(r['verdict'], r['verdict'])}")
    if "liquidity_score" in r:
        print(f"  Балл ликвидности: {r['liquidity_score']}/100")
    if r.get("wb_subject"):
        print(f"  WB-категория: {r.get('wb_parent') or '—'} / {r['wb_subject']}  (vote {r.get('subject_vote_share', 0):.0%})")
    if "demand_score" in r:
        print(f"  Спрос на WB: {r['demand_score']}   Маржа: {r['margin_score']}")
    if r.get("recommended_units_year") is not None:
        print(f"  >>> ЗАКУП НА ГОД: {r['recommended_units_year']} шт "
              f"(WB-спрос {r.get('wb_demand_units_year')}/год × K={r.get('market_share_k', 0) * 100:.3f}%)")
    elif r.get("wb_subject"):
        print("  Закуп: нет данных Разноторга по категории (K не найден)")
    if r.get("reason"):
        print(f"  Причина: {r['reason']}")
    for e in r.get("examples", [])[:5]:
        print(f"    · {e['nm_id']}  {e['feedbacks']} отз.  {e['price']:.0f}₽  {e['name']}")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(description="SAOL — оценка ликвидности WB по фото + прогноз закупа")
    ap.add_argument("image", nargs="?", help="путь к фото товара (jpg/png)")
    ap.add_argument("--nm", type=int, help="артикул WB (вместо фото)")
    ap.add_argument("--price", type=float, required=True, help="закупочная цена, ₽")
    ap.add_argument("--coef", type=Path, help="путь к coefficient.json")
    ap.add_argument("--json", action="store_true", help="вывод в JSON")
    args = ap.parse_args()

    image_bytes = None
    if args.image:
        image_bytes = Path(args.image).read_bytes()
    elif not args.nm:
        ap.error("нужно либо фото, либо --nm <артикул>")

    result = analyze(image_bytes, args.price, seed_nm=args.nm, coef_path=args.coef)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        _print_human(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
