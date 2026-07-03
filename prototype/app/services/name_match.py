"""Имя-ориентированный матч WB-листа ↔ виды Разноторга.

Нужен, когда у Разноторга нет привязки к WB-пути (`taxonomy_items.wb_paths`
пуст) — например, всё освещение. Тогда долю рынка K считаем, сопоставляя по
ИМЕНИ листа категории, а не по полному пути.

Лёгкий стеммер режет частые русские окончания (ед/мн число, падежи), чтобы
«лампа» ↔ «лампы» и «светильники» ↔ «светильник» сходились, но «светильник»
и «светодиодная» оставались РАЗНЫМИ корнями (иначе лампы утекут в светильники).

Матч строго направленный: вид Разноторга принадлежит WB-листу L, если все
значимые корни имени L содержатся в корнях имени вида (`leaf_stems <= item_stems`).
Разноторговские имена обычно длиннее («Светильники настольные» ⊇ «Светильники»),
поэтому subset в эту сторону — это «вид является уточнением листа». Без усреднения.
"""
from __future__ import annotations

import re

_MIN_STEM = 4
_STOP = {"для", "под", "без", "или", "при", "над", "что", "это", "как", "так", "его", "обл"}
# длинные окончания первыми, чтобы резать жадно
_SUFFIXES = (
    "ого", "ому", "ыми", "ими", "его", "ему", "ями", "ами",
    "ое", "ые", "ый", "ий", "ая", "яя", "ою", "ею", "ов", "ев",
    "ей", "ах", "ях", "ам", "ям", "ом", "ем", "их", "ых",
    "ы", "и", "а", "я", "у", "ю", "е", "о", "й", "ь",
)


def normalize(s: str) -> str:
    """casefold + ё→е + всё кроме букв/цифр → пробел, схлопнуть пробелы."""
    low = (s or "").casefold().replace("ё", "е")
    return " ".join(re.sub(r"[^0-9a-zа-я]+", " ", low).split())


def stem_word(w: str) -> str:
    """Срезать одно частое окончание, если остаётся ≥ _MIN_STEM букв."""
    for suf in _SUFFIXES:
        if w.endswith(suf) and len(w) - len(suf) >= _MIN_STEM:
            return w[: -len(suf)]
    return w


def stems(s: str) -> frozenset[str]:
    """Множество корней значимых слов имени (короткие/служебные отброшены)."""
    out: set[str] = set()
    for w in normalize(s).split():
        if len(w) >= _MIN_STEM and w not in _STOP:
            out.add(stem_word(w))
    return frozenset(out)


def leaf_of(path: str) -> str:
    return (path or "").rstrip("/").split("/")[-1].strip()


def build_index(items: list[tuple[frozenset[str], float]]) -> tuple[dict[str, set[int]], list[float]]:
    """items: [(stems, revenue)]. → (posting: корень→{idx}, revenue[idx])."""
    posting: dict[str, set[int]] = {}
    revenue: list[float] = []
    for st, rub in items:
        idx = len(revenue)
        revenue.append(rub)
        for s in st:
            posting.setdefault(s, set()).add(idx)
    return posting, revenue


def matched_indices(
    leaf_stems: frozenset[str],
    posting: dict[str, set[int]],
) -> set[int]:
    """Индексы видов, чьи корни — надмножество корней листа (leaf ⊆ item)."""
    if not leaf_stems:
        return set()
    acc: set[int] | None = None
    for s in leaf_stems:
        ids = posting.get(s)
        if not ids:
            return set()
        acc = set(ids) if acc is None else (acc & ids)
        if not acc:
            return set()
    return acc or set()


def revenue_for_leaf(
    leaf_stems: frozenset[str],
    posting: dict[str, set[int]],
    revenue: list[float],
) -> float:
    """Σ выручки видов, чьи корни — надмножество корней листа (leaf ⊆ item)."""
    return float(sum(revenue[i] for i in matched_indices(leaf_stems, posting)))
