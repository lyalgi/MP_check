"""HTTP-клиент к WB через requests.

Используем requests (а не httpx), потому что у WB на httpx-fingerprint
прилетает 403/429 быстрее.

Поддерживаем PROXY_URL (формат `http://user:pass@host:{port}`); порт
ротируем между запросами и помечаем burnt/good через wb_proxy.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
from typing import Any

import requests

from app.services.providers import wb_proxy

logger = logging.getLogger(__name__)

USER_AGENT = "WBClient/9.1.4 (com.wildberries.ru;build:202; iOS 17.0.0) Alamofire/5.6.1"
CURL_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
)
DEFAULT_HEADERS = {"User-Agent": USER_AGENT, "Accept": "*/*"}
DEFAULT_TIMEOUT = 10.0


def _get_proxy_dict() -> tuple[dict | None, int | None]:
    if not os.environ.get("PROXY_URL"):
        return None, None
    port = wb_proxy.pick_port()
    if port is None:
        return None, None
    url = wb_proxy.proxy_url_for(port)
    if not url:
        return None, port
    return {"http": url, "https": url}, port


def _sync_get(url: str, params: dict, timeout: float) -> dict | None:
    proxies, port = _get_proxy_dict()
    should_try_curl = False
    for attempt in range(3):
        try:
            r = requests.get(
                url,
                params=params,
                headers=DEFAULT_HEADERS,
                proxies=proxies,
                timeout=timeout,
            )
        except requests.RequestException as e:
            logger.warning("requests не смог выполнить запрос (%s), попытка=%d: %s", url, attempt, e)
            should_try_curl = True
            if port is not None:
                wb_proxy.mark_burned(port)
            proxies, port = _get_proxy_dict()
            continue
        if r.status_code in (429, 502, 503, 504, 403):
            should_try_curl = True
            if port is not None:
                wb_proxy.mark_burned(port)
            proxies, port = _get_proxy_dict()
            continue
        if not r.ok:
            logger.warning("WB %s вернул неуспешный статус: %d", url, r.status_code)
            return None
        if port is not None:
            wb_proxy.mark_good(port)
        try:
            return r.json()
        except ValueError:
            should_try_curl = True
            return None
    if should_try_curl:
        return _curl_get_json(url, params, timeout)
    return None


def _curl_get_json(url: str, params: dict, timeout: float) -> dict | None:
    """Резервный путь против антибота WB.

    WB иногда отклоняет TLS-отпечатки Python requests/httpx на card/catalog
    endpoint-ах с части IP, а системный curl получает JSON с той же ссылки.
    Этот обход намеренно живёт в одном низкоуровневом клиенте: бизнес-логика
    остаётся явной и тестируемой.
    """
    curl = shutil.which("curl")
    if not curl:
        return None
    req = requests.Request("GET", url, params=params).prepare()
    cmd = [
        curl,
        "-sS",
        "--compressed",
        "--max-time",
        str(max(1, int(timeout))),
        "-A",
        CURL_USER_AGENT,
        req.url,
    ]
    try:
        res = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=timeout + 2)
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("резервный curl-запрос не выполнился (%s): %s", url, e)
        return None
    if res.returncode != 0:
        logger.warning("резервный curl-запрос завершился с ошибкой (%s): %s", url, res.stderr[:200])
        return None
    try:
        data: Any = json.loads(res.stdout)
    except ValueError:
        logger.warning("резервный curl-запрос вернул не JSON (%s): %s", url, res.stdout[:120])
        return None
    return data if isinstance(data, dict) else None


async def get_json(url: str, params: dict, timeout: float = DEFAULT_TIMEOUT) -> dict | None:
    return await asyncio.to_thread(_sync_get, url, params, timeout)
