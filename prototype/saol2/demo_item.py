#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Демо: показатели одного товара из MPStats.
Запуск:  python -m saol2.demo_item 152490541   (из папки prototype)
"""
import sys

from saol2.metrics import fetch_item_metrics
from saol2.mpstats import MPStats


def main(nm: int) -> int:
    client = MPStats()
    lim = client.limits() or {}
    print(f"Лимит API: использовано {lim.get('use')} из {lim.get('available')}\n")

    m = fetch_item_metrics(client, nm)
    if not m.ok:
        print(f"nm {nm}: данные не получены (см. лог).")
        return 1

    print(f"nm {m.nm}: {m.name}  [{m.brand}]")
    print(f"  Категория: {m.subject_name} (id={m.subject_id})")
    print(f"  Создан: {m.first_date}  (возраст ~{m.age_months} мес)")
    print(f"  Цена: {m.price:.0f}₽ (без скидки {m.base_price:.0f}₽)   остаток: {m.balance}  в наличии: {m.in_stock}")
    print(f"  Выкуп: {m.buyout_pct}%   комиссия FBO: {m.commission_fbo}%")
    print(f"  ЗАКАЗЫ: {m.orders_year:.0f}/год → {m.orders_monthly_avg:.0f}/мес;  за 30д: {m.orders_30d:.0f}")
    print(f"  Выкупы (≈ заказы×выкуп): ~{m.redeemed_year_est:.0f}/год")
    print(f"  Выручка: {m.revenue_year:.0f}₽/год")
    if m.monthly_orders:
        spark = _spark(m.monthly_orders)
        print(f"  Сезонность (помесячно): {spark}  индекс текущего месяца: {m.season_index}")
    return 0


def _spark(vals: list[float]) -> str:
    blocks = "▁▂▃▄▅▆▇█"
    hi = max(vals) or 1
    return "".join(blocks[min(7, int(v / hi * 7))] for v in vals)


if __name__ == "__main__":
    sys.exit(main(int(sys.argv[1]) if len(sys.argv) > 1 else 152490541))
