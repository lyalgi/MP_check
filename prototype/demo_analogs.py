#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Демо: как из ФОТО получаются аналоги и их продажи (на реальных данных WB).
Запуск: python demo_analogs.py <путь_к_фото>
"""
import statistics
import sys

import saol_core as s


def main(path: str) -> int:
    img = open(path, "rb").read()
    print(f"\nФото: {path} ({len(img)} байт)\n")

    # ── Шаг 1. Визуальный поиск WB по фото → артикулы похожих карточек ──
    nm_ids = s.visual_search(img)
    print(f"[1] visual_search вернул {len(nm_ids)} артикулов (порядок = похожесть по версии WB):")
    print("   ", nm_ids[:30], "..." if len(nm_ids) > 30 else "")
    if not nm_ids:
        print("    WB не вернул результатов (возможно, бан серверного IP).")
        return 1

    # ── Шаг 2. Детали карточек (реальные feedbacks/цена/остаток/subjectId) ──
    raw = s.fetch_cards(nm_ids[:30])
    analogs_all = [s.card_to_analog(p) for p in raw]
    print(f"\n[2] Детали получены по {len(analogs_all)} карточкам.\n")

    # ── Шаг 3. Фильтр шума: feedbacks >= 10 ──
    analogs = [a for a in analogs_all if a["feedbacks"] >= s.MIN_FEEDBACKS]
    print(f"[3] Фильтр feedbacks >= {s.MIN_FEEDBACKS}: осталось {len(analogs)} из {len(analogs_all)}.\n")

    # ── Таблица: на каких данных всё держится ──
    hdr = f"{'nm_id':>10} | {'feedbacks':>9} | {'возр.мес':>8} | {'продажи/мес':>11} | {'цена':>7} | {'subj':>6} | name"
    print(hdr)
    print("-" * len(hdr))
    for a in sorted(analogs, key=lambda x: x["sales_30d"], reverse=True):
        # повторяем расчёт возраста ровно как в estimate_monthly_sales
        from datetime import datetime, timezone
        age = "?"
        try:
            # грубо: продажи/мес обратно к (fb/возраст*20) → возраст = fb/(продажи/20)
            if a["sales_30d"] > 0:
                age = round(a["feedbacks"] / (a["sales_30d"] / s.FEEDBACK_TO_SALES), 1)
        except Exception:
            pass
        print(f"{a['nm_id']:>10} | {a['feedbacks']:>9} | {str(age):>8} | "
              f"{a['sales_30d']:>11.1f} | {a['price']:>7.0f} | {str(a['subject_id']):>6} | {a['name'][:40]}")

    # ── Шаг 4. Голосование по категории ──
    sid, share = s.vote_category(analogs)
    print(f"\n[4] Голосование по subjectId (вес = feedbacks): лидер={sid}, доля={share:.0%} "
          f"({'принято' if sid else 'СМЕШАННО → UNKNOWN'})")

    # ── Итоговые числа продаж, которые видит пользователь ──
    sales = [a["sales_30d"] for a in analogs]
    if sales:
        print(f"\n[ИТОГ] median(продажи аналогов) = {statistics.median(sales):.1f}/мес "
              f"(это и есть «WB-спрос»), Σ = {sum(sales):.0f}/мес, "
              f"min={min(sales):.0f}, max={max(sales):.0f}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "photo.jpg"))
