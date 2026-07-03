"""Residential-proxy ротация для запросов к WB.

Шаблон URL: `http://user:pass@host:{port}` — {port} подставляется случайно
из диапазона. На 429 порт «горит» 90 сек; на успехе порт «греется» и
переиспользуется до 90 сек. Кэш — module-level (per-process).

Если PROXY_URL не задан — функция возвращает None, провайдер работает
напрямую (как сейчас).
"""
from __future__ import annotations

import logging
import os
import random
import time
from threading import Lock

log = logging.getLogger(__name__)

DEFAULT_PORTS = list(range(10000, 10100))  # совпадает с pool wb-system
_good_ports: list[tuple[int, float]] = []   # (port, last_success_ts)
_burned_ports: dict[int, float] = {}        # port -> burn_ts
_GOOD_TTL = 90.0
_BURN_COOLDOWN = 90.0
_lock = Lock()


def _now() -> float:
    return time.monotonic()


def _ports() -> list[int]:
    raw = os.environ.get("PROXY_PORTS", "").strip()
    if not raw:
        return DEFAULT_PORTS
    out: list[int] = []
    for tok in raw.split(","):
        tok = tok.strip()
        if "-" in tok:
            a, b = tok.split("-", 1)
            try:
                out.extend(range(int(a), int(b) + 1))
            except ValueError:
                continue
        elif tok.isdigit():
            out.append(int(tok))
    return out or DEFAULT_PORTS


def _evict_expired():
    n = _now()
    # снять «горящие» порты после cooldown
    expired = [p for p, ts in _burned_ports.items() if n - ts > _BURN_COOLDOWN]
    for p in expired:
        _burned_ports.pop(p, None)
    # снять старые good
    while _good_ports and _now() - _good_ports[0][1] > _GOOD_TTL:
        _good_ports.pop(0)


def pick_port() -> int | None:
    """Выбрать порт для запроса. None — если прокси не настроен."""
    if not os.environ.get("PROXY_URL"):
        return None
    with _lock:
        _evict_expired()
        # сначала «горячие» — быстрее проходят
        if _good_ports:
            return _good_ports[-1][0]
        # из тех, что не «горят»
        pool = [p for p in _ports() if p not in _burned_ports]
        if not pool:
            pool = _ports()  # все «горят» — лучше попробовать чем стоять
        return random.choice(pool)


def mark_good(port: int) -> None:
    with _lock:
        _good_ports.append((port, _now()))
        # дедупликация и ограничение размера
        seen = set()
        deduped = []
        for p, ts in reversed(_good_ports):
            if p in seen:
                continue
            seen.add(p)
            deduped.append((p, ts))
        deduped.reverse()
        _good_ports[:] = deduped[-10:]
        _burned_ports.pop(port, None)


def mark_burned(port: int) -> None:
    with _lock:
        _burned_ports[port] = _now()
        _good_ports[:] = [(p, ts) for p, ts in _good_ports if p != port]


def proxy_url_for(port: int) -> str | None:
    tpl = os.environ.get("PROXY_URL", "").strip()
    if not tpl:
        return None
    return tpl.replace("{port}", str(port))
