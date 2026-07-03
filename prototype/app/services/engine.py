"""Оркестратор пайплайна оценки ликвидности (ТЗ САОЛ, шаги 1-6).

1. Визуальный поиск и фильтр  ── визуальный поиск WB → массив nm_id;
                                   убираем карточки с feedbacks < N.
2. Агрегация аналогов          ── Σ feedbacks/sales — совокупный спрос.
3. Извлечение категории        ── взвешенное голосование по subjectId
                                   среди ВСЕХ отфильтрованных аналогов
                                   (не один max-SKU — это устойчиво
                                   к шуму визуального поиска). Если ни одна
                                   subject не доминирует ≥60% — HETEROGENEOUS.
4. Кросс-маппинг WB↔OZON       ── статическая таблица.
5. Эталон рынка                ── top-30 в найденной категории WB → среднее.
6. Рейтинг и вердикт           ── два балла: спрос SKU (median/median)
                                   и объём ниши (Σ/Σ). Главный rating
                                   = гармоническое среднее.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, replace

from sqlalchemy.orm import Session

from app.models import LookupHistory
from app.schemas import AnalogSku, LookupResponse, PipelineStep
from app.services import category_extractor as ce
from app.services import mapping as wb_ozon_mapping
from app.services.analytics import get_analytics_provider
from app.services.benchmark_cache import get_cached_benchmark
from app.services.forecast import lookup_coefficient
from app.services.liquidity import build_advice, compute_score
from app.services.providers.wb_public import _product_to_analog_safe
from app.services.reason_codes import ReasonCode
from app.services.retail_history import lookup_retail_profile, retail_profile_reason_codes
from app.services.snapshots import build_snapshot_metrics, record_market_snapshots
from app.services.wb_visual_search import (
    VisualSearchProvider,
    get_visual_search_provider,
    parse_wb_nm_from_url,
)
from app.settings import settings

logger = logging.getLogger(__name__)


@dataclass
class LookupInput:
    image_bytes: bytes | None
    purchase_price: float
    seed_url: str | None = None
    query: str | None = None          # уточнение: вес/объём/шт/цвет
    seed_nm_ids: list[int] | None = None  # nm_id, найденные на телефоне закупщика


_QUERY_STOP = {"для", "или", "под", "без", "при", "над", "шт", "штук", "штуки"}


def _query_tokens(q: str | None) -> list[str]:
    """Значимые токены уточнения: числа + слова ≥2 букв, без служебных."""
    import re
    toks = re.findall(r"[0-9a-zа-яё]+", (q or "").lower())
    return [t for t in toks if (t.isdigit() or len(t) >= 2) and t not in _QUERY_STOP]


def _card_matches_query(card: dict, tokens: list[str]) -> bool:
    if not tokens:
        return False
    name = (card.get("name") or "").lower()
    return all(t in name for t in tokens)


class LiquidityEngine:
    async def evaluate(self, db: Session, inp: LookupInput) -> LookupResponse:
        started = time.perf_counter()
        steps: list[PipelineStep] = []
        verdict_reasons: list[str] = []

        # ── Шаг 1: визуальный поиск ────────────────────────────────────
        vs: VisualSearchProvider = get_visual_search_provider()
        nm_ids: list[int] = []
        seed_nm = parse_wb_nm_from_url(inp.seed_url) if inp.seed_url else None
        if inp.seed_nm_ids:
            # nm_id уже найдены на ТЕЛЕФОНЕ закупщика (его мобильный IP). Серверный
            # Визуальный поиск пропускаем — снимаем самый банимый запрос с серверного IP.
            nm_ids = [int(n) for n in inp.seed_nm_ids if int(n) > 0]
            steps.append(PipelineStep(
                name="visual_search_device",
                detail="поиск по фото выполнен на устройстве закупщика",
                count=len(nm_ids),
            ))
        if not nm_ids and inp.image_bytes:
            nm_ids = await vs.search_by_image(inp.image_bytes)
            steps.append(PipelineStep(
                name="visual_search",
                detail=f"визуальный поиск WB ({settings.visual_search_provider})",
                count=len(nm_ids),
            ))
        if not nm_ids and seed_nm is not None:
            nm_ids = await vs.search_by_seed_nm(seed_nm)
            steps.append(PipelineStep(
                name="visual_search_seed",
                detail=f"резервный поиск: похожие на nm_id={seed_nm}",
                count=len(nm_ids),
            ))

        if not nm_ids:
            verdict_reasons.append(ReasonCode.NO_VISUAL.value)
            return self._empty_response(steps, started, verdict_reasons, advice=(
                "Визуальный поиск не вернул аналогов. "
                "Подключите SAOL_VISUAL_SEARCH_URL или вставьте ссылку с WB."
            ))

        # ── Подтянем детали карточек ───────────────────────────────────
        # Тянем карточки только по ближайшим N результатов визуального поиска
        # (они самые релевантные): WB отдаёт до ~256 nm, но скачивать все —
        # лишний прокси-трафик. Остальные обычно мусор (новые карточки без отзывов).
        nm_ids_to_fetch = nm_ids[: settings.visual_max_cards]
        raw_cards = await ce.fetch_cards_raw(nm_ids_to_fetch)
        if not raw_cards:
            verdict_reasons.append(ReasonCode.WB_DETAIL_UNAVAILABLE.value)
            return self._empty_response(steps, started, verdict_reasons, advice=(
                "WB не отдаёт детали карточек. Проверьте PROXY_URL и сеть."
            ))

        # ── Шаг 1a: уточнение по тексту (вес/объём/шт/цвет) ────────────
        # WB photo-search (uploadsearch) текст ИГНОРИРУЕТ (проверено), поэтому
        # сужаем визуальную выдачу у себя: оставляем карточки, чьё имя содержит
        # все значимые токены уточнения. Если точных совпадений мало (<3 с
        # отзывами) — текст не применяем, чтобы не сузиться до мусора.
        if inp.query:
            tokens = _query_tokens(inp.query)
            matched = [p for p in raw_cards if _card_matches_query(p, tokens)]
            matched_fb = sum(1 for p in matched if int(p.get("feedbacks") or 0) >= settings.min_feedbacks)
            if matched_fb >= 3:
                raw_cards = matched
                detail = f"«{inp.query}» → {len(raw_cards)} подходящих карточек"
            else:
                detail = f"«{inp.query}»: мало точных совпадений — учитываю всю выдачу"
            steps.append(PipelineStep(name="refine_query", detail=detail, count=len(raw_cards)))

        analogs_all = [a for a in (_product_to_analog_safe(p) for p in raw_cards) if a]

        # ── Шаг 1b: фильтр feedbacks >= N ──────────────────────────────
        analogs = [a for a in analogs_all if a.feedbacks >= settings.min_feedbacks]
        steps.append(PipelineStep(
            name="filter_feedbacks",
            detail=f"≥ {settings.min_feedbacks} отзывов (отсеяно {len(analogs_all) - len(analogs)})",
            count=len(analogs),
        ))

        if not analogs:
            verdict_reasons.append(ReasonCode.LOW_FEEDBACKS.value)

        # ── Шаг 2: совокупный спрос ────────────────────────────────────
        total_feedbacks = sum(a.feedbacks for a in analogs)
        total_sales_30d = sum(a.sales_30d_est for a in analogs)
        steps.append(PipelineStep(
            name="aggregate_demand",
            detail=f"Σ отзывов = {total_feedbacks}; Σ продаж/мес ≈ {total_sales_30d:.0f}",
            count=len(analogs),
        ))

        # ── Шаг 3: категория через weighted vote ───────────────────────
        category = None
        vote_share = 0.0
        top_seed = None
        if analogs:
            dominant_subj, vote_share = ce.vote_subject_id(raw_cards, analogs, min_share=0.6)
            if dominant_subj is None:
                verdict_reasons.append(ReasonCode.HETEROGENEOUS_SUBJECTS.value)
                # Закрываемся в безопасную сторону: если выдача разнородная, не
                # назначаем категорию по одному лидеру. Такой эталон опаснее, чем
                # честный UNKNOWN.
                top_seed = ce.pick_top_by_feedbacks(analogs)
            else:
                # доминирующая категория — берём её «представителя» (макс. отзывов в этой категории)
                repr_card = ce.pick_raw_card_for_subject(raw_cards, dominant_subj)
                if repr_card is not None:
                    category = ce.extract_category([repr_card], db=db)
                    top_seed = next(
                        (a for a in analogs if a.nm_id == int(repr_card.get("id") or 0)),
                        None,
                    )
        steps.append(PipelineStep(
            name="category_extract",
            detail=(
                f"доля голосов={vote_share:.0%}, категория={category.subject_name if category else '—'} "
                f"(id={category.subject_id if category else '—'})"
            ) if category else f"доля голосов={vote_share:.0%} — категория не определена",
        ))
        if analogs and category is None and ReasonCode.CATEGORY_UNRESOLVED.value not in verdict_reasons:
            verdict_reasons.append(ReasonCode.CATEGORY_UNRESOLVED.value)

        # ── Шаг 4: WB → OZON ───────────────────────────────────────────
        wb_path = None
        if category and category.parent_name and category.subject_name:
            wb_path = f"{category.parent_name}/{category.subject_name}"
        mapping = wb_ozon_mapping.find_mapping(
            db,
            wb_path=wb_path,
            subject_name=category.subject_name if category else None,
        )
        steps.append(PipelineStep(
            name="map_wb_to_ozon",
            detail=(
                f"{wb_path or (category.subject_name if category else '—')} → "
                f"{mapping.ozon_path or '—'} ({mapping.match_kind})"
            ),
        ))
        if mapping.ozon_path is None:
            verdict_reasons.append(ReasonCode.WB_ONLY.value)

        # ── Шаг 5: топ-30 как эталон ниши ──────────────────────────────
        # Источник:
        #   1) wb_catalog — попытка дёрнуть catalog.wb.ru по WB-пути из xlsx
        #      или по shard'у из menu-tree. Это РЕАЛЬНЫЙ top-30 ниши.
        #   2) visual_subset_in_subject — резервная эвристика: берём из самой
        #      визуальной выдачи карточки в доминирующей категории. Это не «топ ниши»,
        #      а «срез визуальной выдачи в её доминирующей категории».
        #      Явно помечаем top_is_heuristic=True, чтобы решение пользователя
        #      опиралось не вслепую.
        top: list[AnalogSku] = []
        top_source: str | None = None
        top_is_heuristic = False

        provider = get_analytics_provider()

        # Кэш с чтением сквозь промах: если фоновый индексатор уже собрал топ ниши и он свеж —
        # берём его (быстро + выборка полная и одинаковая для всех товаров, не
        # зависит от того, сколько успели за бюджет). Промах → обычный живой скрапинг.
        if category and category.subject_id:
            cached_top = get_cached_benchmark(
                db, category.subject_id, settings.benchmark_cache_max_age_hours
            )
            if cached_top:
                top = cached_top
                top_source = "cache"

        if not top and category and category.subject_id and self._prefer_subject_catalog(category.parent_name):
            try:
                top = await provider.top_n_by_subject(
                    category.subject_id,
                    limit=100,
                    parent_name=category.parent_name,
                )
                top = [t for t in top if t.feedbacks > 0]
                if top:
                    top_source = "wb_subject_catalog"
            except Exception as e:
                logger.warning("предварительный top_n_by_subject не сработал для %s: %s", category.subject_id, e)

        if category and category.parent_name and category.subject_name:
            wb_path = f"{category.parent_name}/{category.subject_name}"
            try:
                # Топ-100 — оптимум: страница 1 catalog WB отсортирована по популярности,
                # дальше идёт шум (новинки без отзывов). Top-300 уже искажал
                # медиану вниз. После забора фильтруем активные (отзывы > 0):
                # WB подмешивает в топ-100 свежие карточки с fb=0 — они шумят.
                if not top:
                    top = await provider.top_n_in_category(
                        wb_path, limit=100, subject_id=category.subject_id
                    )
                    top = [t for t in top if t.feedbacks > 0]
                if top and not top_source:
                    top_source = "wb_catalog"
            except Exception as e:
                logger.warning("top_n_in_category не сработал для %s: %s", wb_path, e)

        if not top and category and category.subject_id:
            try:
                top = await provider.top_n_by_subject(
                    category.subject_id,
                    limit=100,
                    parent_name=category.parent_name,
                )
                top = [t for t in top if t.feedbacks > 0]
                if top:
                    top_source = "wb_subject_catalog"
            except Exception as e:
                logger.warning("top_n_by_subject не сработал для %s: %s", category.subject_id, e)

        if not top and category and category.subject_id:
            # Резервный путь: подмножество визуального поиска в той же категории.
            # Это эвристика — НЕ реальный топ ниши, но даёт хоть какой-то
            # эталон из визуально похожих карточек.
            # ВАЖНО: фильтруем карточки с отзывами=0 — иначе median(top) занулится
            # (визуальный поиск возвращает много пустых новых карточек), и sku_demand
            # станет 0 при положительном спросе аналогов.
            nm_to_subject = {int(p.get("id") or 0): int(p.get("subjectId") or 0) for p in raw_cards}
            same_subject_active = [
                a for a in analogs_all
                if nm_to_subject.get(a.nm_id) == category.subject_id and a.feedbacks > 0
            ]
            top = sorted(
                same_subject_active,
                key=lambda a: (a.feedbacks, a.rating or 0.0, a.nm_id),
                reverse=True,
            )[:30]
            if top:
                top_source = "visual_subset_in_subject"
                top_is_heuristic = True
                if ReasonCode.HEURISTIC_BENCHMARK.value not in verdict_reasons:
                    verdict_reasons.append(ReasonCode.HEURISTIC_BENCHMARK.value)

        top_avg = sum(t.sales_30d_est for t in top) / max(1, len(top)) if top else 0.0
        steps.append(PipelineStep(
            name="benchmark_top30",
            detail=(
                f"источник={top_source or '—'}{' (эвристика)' if top_is_heuristic else ''} "
                f"в категории «{category.subject_name if category else '—'}»: "
                f"среднее {top_avg:.0f}/мес"
            ),
            count=len(top),
        ))

        # ── Шаг 6: снимки маркетплейса + рейтинг ──────────────────────
        analog_snapshot_metrics = build_snapshot_metrics(db, analogs)
        top_snapshot_metrics = build_snapshot_metrics(db, top)
        score = compute_score(
            analogs,
            top,
            purchase_price=inp.purchase_price,
            analog_snapshot_metrics=analog_snapshot_metrics,
            top_snapshot_metrics=top_snapshot_metrics,
        )
        inserted_snapshots = 0
        inserted_snapshots += record_market_snapshots(
            db,
            analogs_all,
            "visual",
            subject_id=category.subject_id if category else None,
            subject_name=category.subject_name if category else None,
            parent_name=category.parent_name if category else None,
        )
        inserted_snapshots += record_market_snapshots(
            db,
            top,
            "benchmark",
            subject_id=category.subject_id if category else None,
            subject_name=category.subject_name if category else None,
            parent_name=category.parent_name if category else None,
        )
        steps.append(PipelineStep(
            name="market_snapshots",
            detail=(
                f"совпало={score.snapshot_match_count}, окно={score.snapshot_observation_days:.1f}д, "
                f"новых={inserted_snapshots}"
            ),
            count=inserted_snapshots,
        ))
        if int((time.perf_counter() - started) * 1000) > settings.slow_lookup_ms:
            if ReasonCode.SLOW_LOOKUP.value not in verdict_reasons:
                verdict_reasons.append(ReasonCode.SLOW_LOOKUP.value)
        # сливаем причины из liquidity в общий список (dedup)
        for rc in score.reasons:
            if rc not in verdict_reasons:
                verdict_reasons.append(rc)

        # ── Шаг 6b: внутренняя история Разноторга ─────────────────────
        retail_profile = lookup_retail_profile(
            db,
            wb_path=wb_path,
            wb_subject_name=category.subject_name if category else None,
        )
        for rc in retail_profile_reason_codes(retail_profile):
            if rc not in verdict_reasons:
                verdict_reasons.append(rc)
        if retail_profile:
            steps.append(PipelineStep(
                name="retail_history",
                detail=(
                    f"{retail_profile.item_count} строк, {retail_profile.matched_vid_count} видов, "
                    f"оборачиваемость={retail_profile.sell_through or 0:.0%}, "
                    f"год-к-году={retail_profile.yoy_ratio or 0:.2f}, "
                    f"рентаб={retail_profile.median_profitability if retail_profile.median_profitability is not None else '—'}"
                ),
                count=retail_profile.item_count,
            ))
        else:
            steps.append(PipelineStep(
                name="retail_history",
                detail="нет внутренних товарных строк Разноторга для этой WB-категории",
                count=0,
            ))

        examples = (top or analogs)[:5]

        # Защита от смещения по размеру выборки: тонкая/усечённая выдача (мало
        # аналогов или короткий эталон) не должна давать уверенный GREEN —
        # иначе балл зависит от того, сколько успели собрать за бюджет времени,
        # а не от реальной ликвидности.
        if score.analog_count < 3 or score.top_count < 10:
            if ReasonCode.LOW_SAMPLE.value not in verdict_reasons:
                verdict_reasons.append(ReasonCode.LOW_SAMPLE.value)

        final_score = self._apply_decision_policy(score, verdict_reasons)
        for rc in final_score.reasons:
            if rc not in verdict_reasons:
                verdict_reasons.append(rc)
        confidence = self._decision_confidence(final_score.verdict, verdict_reasons)
        wb_demand_verdict = self._wb_demand_verdict(score)

        # ── Шаг 7: сколько взять = ТЕСТОВАЯ ПАРТИЯ (поиск НОВЫХ товаров) ──
        # Задача — новинки из Китая, которых у Разноторга ещё НЕТ, поэтому закуп НЕ
        # привязываем к истории Разноторга (её по новинке и нет). Берём базовую
        # тест-партию по цене входа × множитель уверенности (вердикт). Данные
        # Разноторга (наценка/остатки) — только справка, в число НЕ входят.
        recommended_units = self._test_quantity(inp.purchase_price, final_score.verdict)
        forecast_source = "test_quantity" if recommended_units is not None else None
        forecast_units = float(recommended_units) if recommended_units is not None else None
        wb_forecast_units = None
        wb_strength = None
        baseline_units = None
        if recommended_units is not None and ReasonCode.FORECAST_OK.value not in verdict_reasons:
            verdict_reasons.append(ReasonCode.FORECAST_OK.value)

        # справка по Разноторгу (категория/доля рынка) — НЕ влияет на закуп
        forecast = None
        if category and category.parent_name and category.subject_name:
            forecast = lookup_coefficient(db, f"{category.parent_name}/{category.subject_name}")
        if forecast is None and category and category.subject_name:
            forecast = lookup_coefficient(db, category.subject_name)

        tier_label = {
            "STRONG": "расширенный тест", "GREEN": "тест", "YELLOW": "малый тест",
            "RED": "не брать", "UNKNOWN": "нет данных",
        }.get(final_score.verdict, "")
        detail = (
            f"тест-партия по цене входа {inp.purchase_price:.0f}₽ + вердикт "
            f"{final_score.verdict} ({tier_label}) → "
            f"{recommended_units if recommended_units is not None else '—'} шт"
        )
        steps.append(PipelineStep(name="forecast_test_qty", detail=detail, count=recommended_units))

        advice, reasons = build_advice(final_score, inp.purchase_price)
        self._append_operational_reasons(reasons, verdict_reasons)
        steps.append(PipelineStep(
            name="rating",
            detail=(
                f"балл {final_score.liquidity_score:.0f}/100 (sku={final_score.sku_demand_score:.2f}, "
                f"ниша={final_score.niche_volume_score:.2f}, доверие={confidence}) → {final_score.verdict}"
            ),
        ))

        duration_ms = int((time.perf_counter() - started) * 1000)
        response = LookupResponse(
            verdict=final_score.verdict,
            liquidity_score=final_score.liquidity_score,
            wb_popularity_score=final_score.wb_popularity_score,
            wb_demand_verdict=wb_demand_verdict,
            wb_demand_units_month=final_score.analog_median_sales_30d,
            rating=final_score.rating,
            demand_score=final_score.demand_score,
            sell_through_score=final_score.sell_through_score,
            margin_score=final_score.margin_score,
            competition_score=final_score.competition_score,
            trend_score=final_score.trend_score,
            data_quality_score=final_score.data_quality_score,
            sku_demand_score=final_score.sku_demand_score,
            niche_volume_score=final_score.niche_volume_score,
            decision_confidence=confidence,
            rating_green_threshold=settings.rating_green,
            rating_yellow_threshold=settings.rating_yellow,
            description=top_seed.name if top_seed else None,
            pipeline=steps,
            visual_search_count=len(nm_ids),
            filtered_analog_count=len(analogs),
            analog_total_feedbacks=total_feedbacks,
            analog_total_sales_30d=round(total_sales_30d, 1),
            analog_median_sales_30d=final_score.analog_median_sales_30d,
            top_seed_nm_id=top_seed.nm_id if top_seed else None,
            wb_subject_id=category.subject_id if category else None,
            wb_subject_name=category.subject_name if category else None,
            wb_parent_name=category.parent_name if category else None,
            subject_vote_share=round(vote_share, 3),
            ozon_category=mapping.ozon_path,
            mapping_kind=mapping.match_kind,
            top_avg_sales_30d=round(top_avg, 1),
            top_total_sales_30d=final_score.top_total_sales_30d,
            top_median_sales_30d=final_score.top_median_sales_30d,
            top_count=len(top),
            top_source=top_source,
            top_is_heuristic=top_is_heuristic,
            market_share_coefficient=forecast.coefficient if forecast else None,
            coefficient_match_kind=forecast.match_kind if forecast else None,
            raznotorg_revenue_year=forecast.raznotorg_revenue_year if forecast else None,
            marketplace_revenue_year=forecast.marketplace_revenue_year if forecast else None,
            raznotorg_units_year=forecast.raznotorg_units_year if forecast else None,
            raznotorg_positions=forecast.raznotorg_positions if forecast else None,
            baseline_units_per_position=round(baseline_units, 1) if baseline_units is not None else None,
            wb_strength=round(wb_strength, 3) if wb_strength is not None else None,
            forecast_units_year=round(forecast_units, 2) if forecast_units is not None else None,
            wb_forecast_units_year=round(wb_forecast_units, 2) if wb_forecast_units is not None else None,
            forecast_source=forecast_source,
            recommended_units_year=recommended_units,
            snapshot_match_count=final_score.snapshot_match_count,
            snapshot_observation_days=final_score.snapshot_observation_days,
            stock_pressure_months=final_score.stock_pressure_months,
            market_price_median=final_score.market_price_median,
            retail_history_match_kind=retail_profile.match_kind if retail_profile else None,
            retail_history_item_count=retail_profile.item_count if retail_profile else 0,
            retail_history_sales_year=retail_profile.sales_year if retail_profile else None,
            retail_history_stock=retail_profile.stock if retail_profile else None,
            retail_history_sell_through=retail_profile.sell_through if retail_profile else None,
            retail_history_yoy_ratio=retail_profile.yoy_ratio if retail_profile else None,
            retail_history_median_price=retail_profile.median_price if retail_profile else None,
            retail_history_markup=retail_profile.median_markup if retail_profile else None,
            retail_history_profitability=retail_profile.median_profitability if retail_profile else None,
            advice=advice,
            reasons=reasons,
            verdict_reasons=verdict_reasons,
            examples=examples,
            duration_ms=duration_ms,
        )

        db.add(LookupHistory(
            purchase_price=inp.purchase_price,
            seed_nm_id=seed_nm,
            visual_search_count=len(nm_ids),
            filtered_analog_count=len(analogs),
            analog_total_feedbacks=total_feedbacks,
            analog_total_sales_30d=round(total_sales_30d, 1),
            top_seed_nm_id=top_seed.nm_id if top_seed else None,
            wb_subject_id=category.subject_id if category else None,
            wb_subject_name=category.subject_name if category else None,
            wb_parent_name=category.parent_name if category else None,
            ozon_category=mapping.ozon_path,
            top_avg_sales_30d=round(top_avg, 1),
            rating=final_score.rating,
            liquidity_score=final_score.liquidity_score,
            wb_popularity_score=final_score.wb_popularity_score,
            wb_demand_verdict=wb_demand_verdict,
            wb_demand_units_month=final_score.analog_median_sales_30d,
            demand_score=final_score.demand_score,
            sell_through_score=final_score.sell_through_score,
            margin_score=final_score.margin_score,
            competition_score=final_score.competition_score,
            trend_score=final_score.trend_score,
            data_quality_score=final_score.data_quality_score,
            sku_demand_score=final_score.sku_demand_score,
            niche_volume_score=final_score.niche_volume_score,
            verdict=final_score.verdict,
            decision_confidence=confidence,
            verdict_reasons=verdict_reasons,
            subject_vote_share=round(vote_share, 3),
            market_share_coefficient=forecast.coefficient if forecast else None,
            raznotorg_revenue_year=forecast.raznotorg_revenue_year if forecast else None,
            marketplace_revenue_year=forecast.marketplace_revenue_year if forecast else None,
            forecast_units_year=round(forecast_units, 2) if forecast_units is not None else None,
            wb_forecast_units_year=round(wb_forecast_units, 2) if wb_forecast_units is not None else None,
            forecast_source=forecast_source,
            recommended_units_year=recommended_units,
            snapshot_match_count=final_score.snapshot_match_count,
            snapshot_observation_days=final_score.snapshot_observation_days,
            stock_pressure_months=final_score.stock_pressure_months,
            market_price_median=final_score.market_price_median,
            retail_history_match_kind=retail_profile.match_kind if retail_profile else None,
            retail_history_item_count=retail_profile.item_count if retail_profile else 0,
            retail_history_sales_year=retail_profile.sales_year if retail_profile else None,
            retail_history_stock=retail_profile.stock if retail_profile else None,
            retail_history_sell_through=retail_profile.sell_through if retail_profile else None,
            retail_history_yoy_ratio=retail_profile.yoy_ratio if retail_profile else None,
            retail_history_median_price=retail_profile.median_price if retail_profile else None,
            retail_history_markup=retail_profile.median_markup if retail_profile else None,
            retail_history_profitability=retail_profile.median_profitability if retail_profile else None,
            advice=advice,
            payload={
                "steps": [s.model_dump() for s in steps],
                "examples": [e.model_dump() for e in examples],
                "reasons": reasons,
                "verdict_reasons": verdict_reasons,
                "decision_confidence": confidence,
                "wb_demand_verdict": wb_demand_verdict,
                "wb_forecast_units_year": wb_forecast_units,
                "forecast_source": forecast_source,
                "baseline_units_per_position": baseline_units,
                "wb_strength": wb_strength,
                "raznotorg_units_year": forecast.raznotorg_units_year if forecast else None,
                "raznotorg_positions": forecast.raznotorg_positions if forecast else None,
                "snapshot_metrics": {
                    "analog": analog_snapshot_metrics.__dict__,
                    "top": top_snapshot_metrics.__dict__,
                },
                "retail_history": retail_profile.__dict__ if retail_profile else None,
                "duration_ms": duration_ms,
            },
        ))
        db.commit()
        return response

    def _empty_response(self, steps, started, verdict_reasons, advice: str) -> LookupResponse:
        duration_ms = int((time.perf_counter() - started) * 1000)
        return LookupResponse(
            verdict="UNKNOWN",
            rating=0.0,
            sku_demand_score=0.0,
            niche_volume_score=0.0,
            decision_confidence="LOW",
            rating_green_threshold=settings.rating_green,
            rating_yellow_threshold=settings.rating_yellow,
            pipeline=steps,
            advice=advice,
            reasons=["пайплайн остановлен на шаге сбора аналогов"],
            verdict_reasons=verdict_reasons,
            duration_ms=duration_ms,
        )

    def _apply_decision_policy(self, score, verdict_reasons: list[str]):
        """Понижать вердикт при операционной неопределённости.

        Математика ликвидности может сказать GREEN, но полевое решение не должно
        звучать как «брать уверенно», если категория, эталон или покрытие рынка
        ненадёжны.
        """
        reasons = list(score.reasons)

        hard_blocks = {
            ReasonCode.HETEROGENEOUS_SUBJECTS.value,
            ReasonCode.CATEGORY_UNRESOLVED.value,
        }
        if score.verdict != "UNKNOWN" and any(r in verdict_reasons for r in hard_blocks):
            if ReasonCode.VERDICT_CAPPED.value not in verdict_reasons:
                verdict_reasons.append(ReasonCode.VERDICT_CAPPED.value)
            if ReasonCode.VERDICT_CAPPED.value not in reasons:
                reasons.append(ReasonCode.VERDICT_CAPPED.value)
            return replace(score, verdict="UNKNOWN", rating=0.0, reasons=reasons)

        # WB_ONLY НЕ капает GREEN: проект WB-first (90% оценки — спрос на WB),
        # отсутствие Ozon-маппинга не должно понижать хорошую WB-закупку. WB_ONLY
        # остаётся пометкой + снижает доверие. OFFLINE_OVERSTOCK тоже не включаем
        # (остаток Разноторга игнорируется). LOW_SAMPLE капает: на тонкой/усечённой
        # выборке GREEN ненадёжен.
        caution_reasons = {
            ReasonCode.HEURISTIC_BENCHMARK.value,
            ReasonCode.LOW_SAMPLE.value,
            ReasonCode.OFFLINE_DECLINING.value,
            ReasonCode.LOW_OFFLINE_PROFITABILITY.value,
        }
        if score.verdict in ("GREEN", "STRONG") and any(r in verdict_reasons for r in caution_reasons):
            if ReasonCode.VERDICT_CAPPED.value not in verdict_reasons:
                verdict_reasons.append(ReasonCode.VERDICT_CAPPED.value)
            if ReasonCode.VERDICT_CAPPED.value not in reasons:
                reasons.append(ReasonCode.VERDICT_CAPPED.value)
            return replace(
                score,
                verdict="YELLOW",
                rating=min(score.rating, settings.rating_green),
                reasons=reasons,
            )
        return score

    @staticmethod
    def _test_quantity(purchase_price: float, verdict: str) -> int | None:
        """Тестовая партия для НОВОГО товара: база по цене входа × уверенность.
        История Разноторга не нужна (новинки у него ещё нет). Тиры и множители —
        бизнес-настройка, легко поменять."""
        p = purchase_price or 0.0
        if p <= 100:
            base = 50
        elif p <= 300:
            base = 20
        elif p <= 1000:
            base = 7
        else:
            base = 2
        mult = {"STRONG": 1.5, "GREEN": 1.0, "YELLOW": 0.5, "RED": 0.0}.get(verdict)
        if mult is None:        # UNKNOWN — число не даём
            return None
        if mult <= 0:           # RED — не брать
            return 0
        return max(1, round(base * mult))

    @staticmethod
    def _wb_demand_verdict(score) -> str:
        """Чистый WB-ответ: востребован ли сфотографированный товар/его аналоги на WB?

        Намеренно игнорируем Ozon-сопоставление, остатки Разноторга и внутреннюю
        рентабельность. Эти сигналы могут ограничить итоговый закупочный вердикт,
        но не должны скрывать главный маркетплейс-сигнал от закупщика.
        """
        if score.analog_count <= 0 or score.top_count <= 0:
            return "UNKNOWN"
        abs_month = score.analog_median_sales_30d or 0.0
        target = settings.absolute_demand_target_month
        # GREEN — только хит (уровень лидеров + верх перцентиля). Абсолютный спрос
        # сам по себе НЕ даёт GREEN, лишь поднимает до YELLOW (≥30% target).
        if score.sku_demand_score >= settings.rating_green and score.wb_popularity_score >= 45:
            return "GREEN"
        if score.sku_demand_score >= settings.rating_yellow or score.wb_popularity_score >= 30 \
                or (target > 0 and abs_month >= target * 0.3):
            return "YELLOW"
        return "RED"

    @staticmethod
    def _prefer_subject_catalog(parent_name: str | None) -> bool:
        # В subjects.json часть крупных веток названа иначе, чем в WB menu-tree:
        # «Спортивный товар» vs «Спорт». Для них subjectId быстрее и точнее,
        # чем дорогая попытка пройти menu path, который всё равно не совпадёт.
        return (parent_name or "").strip().casefold() in {
            "спортивный товар",
        }

    @staticmethod
    def _action_units_for_verdict(verdict: str, raw_units: int) -> int | None:
        if raw_units <= 0:
            return 0
        if verdict == "GREEN":
            return raw_units
        if verdict == "YELLOW":
            return min(raw_units, max(1, min(50, int(round(raw_units * 0.1)))))
        if verdict == "RED":
            return 0
        return None

    @staticmethod
    def _decision_confidence(verdict: str, verdict_reasons: list[str]) -> str:
        if verdict == "UNKNOWN":
            return "LOW"
        if any(r in verdict_reasons for r in (
            ReasonCode.VERDICT_CAPPED.value,
            ReasonCode.WB_ONLY.value,
            ReasonCode.HEURISTIC_BENCHMARK.value,
            ReasonCode.SLOW_LOOKUP.value,
            ReasonCode.SNAPSHOT_COLD_START.value,
            ReasonCode.LOW_DATA_QUALITY.value,
            ReasonCode.NO_RETAIL_HISTORY.value,
            ReasonCode.OFFLINE_DECLINING.value,
            ReasonCode.LOW_OFFLINE_PROFITABILITY.value,
        )):
            return "MEDIUM"
        return "HIGH"

    @staticmethod
    def _append_operational_reasons(reasons: list[str], verdict_reasons: list[str]) -> None:
        if ReasonCode.VERDICT_CAPPED.value in verdict_reasons:
            reasons.append("вердикт понижен из-за неполных данных пайплайна")
        if ReasonCode.WB_ONLY.value in verdict_reasons:
            reasons.append("OZON не учтён: решение построено только на данных WB")
        if ReasonCode.HEURISTIC_BENCHMARK.value in verdict_reasons:
            reasons.append("топ-30 WB не получен: эталон заменён срезом визуального поиска")
        if ReasonCode.HETEROGENEOUS_SUBJECTS.value in verdict_reasons:
            reasons.append("визуальный поиск вернул разнородные категории — нужна повторная фотография или ручная ссылка")
        if ReasonCode.SLOW_LOOKUP.value in verdict_reasons:
            reasons.append("ответ вышел за полевой SLA — возможны проблемы сети, WB или прокси")
        if ReasonCode.SNAPSHOT_COLD_START.value in verdict_reasons:
            reasons.append("история снимков ещё копится — скорость продаж пока оценочная")
        if ReasonCode.SNAPSHOT_VELOCITY.value in verdict_reasons:
            reasons.append("скорость продаж уточнена по дельтам снимков")
        if ReasonCode.LOW_MARGIN.value in verdict_reasons:
            reasons.append("закупочная цена слишком близка к медианной цене рынка")
        if ReasonCode.HIGH_STOCK_PRESSURE.value in verdict_reasons:
            reasons.append("у аналогов много остатков относительно месячного спроса")
        if ReasonCode.TOP_HEAVY_CATEGORY.value in verdict_reasons:
            reasons.append("спрос в категории сконцентрирован у нескольких лидеров")
        if ReasonCode.NO_RETAIL_HISTORY.value in verdict_reasons:
            reasons.append("внутренняя история Разноторга по этому виду не найдена")
        if ReasonCode.RETAIL_HISTORY_OK.value in verdict_reasons:
            reasons.append("внутренняя история Разноторга не конфликтует с WB-сигналом")
        if ReasonCode.OFFLINE_OVERSTOCK.value in verdict_reasons:
            reasons.append("в Разноторге похожий вид продаётся медленнее текущих остатков")
        if ReasonCode.OFFLINE_DECLINING.value in verdict_reasons:
            reasons.append("продажи похожего вида в Разноторге заметно ниже прошлого года")
        if ReasonCode.LOW_OFFLINE_PROFITABILITY.value in verdict_reasons:
            reasons.append("по внутренней истории у похожего вида слабая рентабельность")


engine = LiquidityEngine()
