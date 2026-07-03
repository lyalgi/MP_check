"""Тесты имя-ориентированного матчера WB-лист ↔ виды Разноторга."""
from __future__ import annotations

from app.services.name_match import (
    build_index,
    leaf_of,
    normalize,
    revenue_for_leaf,
    stem_word,
    stems,
)


def test_normalize_lowercases_and_strips_punct_and_yo():
    assert normalize("Светильники НАСТЕННО-потолочные") == "светильники настенно потолочные"
    assert normalize("Ёлка/Игрушки") == "елка игрушки"
    assert normalize("  ") == ""


def test_stem_word_singular_plural_collapse():
    # ед/мн должны схлопываться
    assert stem_word("лампы") == stem_word("лампа") == "ламп"
    assert stem_word("люстры") == stem_word("люстра") == "люстр"


def test_stem_word_keeps_distinct_roots():
    # светильник и светодиодная — РАЗНЫЕ корни (иначе лампы утекут в светильники)
    assert stem_word("светильники") != stem_word("светодиодная")


def test_stems_drops_short_and_stop_words():
    s = stems("Лампа для сушки GU 5")
    assert "ламп" in s
    assert "для" not in s          # стоп-слово
    assert "gu" not in s and "5" not in s   # короткие


def test_leaf_of():
    assert leaf_of("Дом/Освещение/Светильники") == "Светильники"
    assert leaf_of("Светильники") == "Светильники"


def _lighting_index():
    items = [
        (stems("Светильники настольные") | stems("Настольный светильник"), 100.0),
        (stems("Лампы") | stems("Светодиодная лампа Е27 A"), 50.0),
        (stems("Люстры") | stems("Люстра 3 рожка"), 30.0),
        (stems("Уличное освещение") | stems("Светильник подвесной"), 7.0),
    ]
    return build_index(items)


def test_leaf_matches_only_its_own_root():
    posting, revenue = _lighting_index()
    # «Светильники» НЕ должны вобрать лампу (50) и люстру (30)
    assert revenue_for_leaf(stems("Светильники"), posting, revenue) == 107.0  # настольный + уличный
    assert revenue_for_leaf(stems("Лампы"), posting, revenue) == 50.0
    assert revenue_for_leaf(stems("Люстры"), posting, revenue) == 30.0


def test_two_word_leaf_requires_both_roots():
    posting, revenue = _lighting_index()
    # «Светильники уличные» = пересечение корней {светильник, уличн} → только уличный item
    assert revenue_for_leaf(stems("Светильники уличные"), posting, revenue) == 7.0


def test_unmatched_leaf_returns_zero():
    posting, revenue = _lighting_index()
    assert revenue_for_leaf(stems("Ножницы для рукоделия"), posting, revenue) == 0.0
    assert revenue_for_leaf(frozenset(), posting, revenue) == 0.0
