"""Полевая регрессия: прогнать папку фото через API и собрать CSV.

Зачем: перед выпуском нужна пачка из 100-200 реальных фото, прогнанных через
систему, чтобы вручную разметить аномалии (неверная категория, ложный
GREEN/RED, неадекватный закуп) и понять, где модель врёт. Без этого выпуск
вслепую.

Использование:
    python scripts/field_regression.py /путь/к/папке_с_фото
    python scripts/field_regression.py ./photos --price 1500 --workers 3 \\
        --url http://127.0.0.1:8088 --out ~/Downloads/saol_regress.csv

В CSV две пустые колонки — `expected_verdict` и `note` — для ручной разметки:
проставь ожидаемый вердикт и комментарий там, где система ошиблась. Потом
по этому файлу считаем точность и чиним конкретные категории.
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import requests

_IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".bmp"}
_COLUMNS = [
    "group", "file", "verdict", "wb_demand_verdict", "subject", "parent",
    "found_seed_nm", "nm_found",
    "recommended_units", "baseline_per_position", "wb_strength",
    "liquidity_score", "wb_popularity", "analog_count", "top_count",
    "forecast_source", "key_reasons", "duration_ms", "error",
    "expected_verdict", "note",
]
# коды, которые важно видеть в разметке (деградации/капы)
_FLAG_REASONS = {
    "LOW_SAMPLE", "WB_ONLY", "HEURISTIC_BENCHMARK", "VERDICT_CAPPED",
    "CATEGORY_UNRESOLVED", "HETEROGENEOUS_SUBJECTS", "NO_BENCHMARK",
    "DEAD_NICHE", "NO_RAZNOTORG_HISTORY", "SLOW_LOOKUP", "LOW_MARGIN",
}


def _is_real_photo(p: Path) -> bool:
    """Реальное фото товара, а не нано-банана генерация (slot_/qa_/server_/*_raw)."""
    if p.suffix.lower() not in _IMG_EXT:
        return False
    n = p.name.lower()
    return not n.startswith(("slot_", "qa_", "server_")) and "_raw" not in n


def _select_last_per_folder(root: Path, n: int, limit_folders: int = 0) -> list[Path]:
    """Для каждой подпапки — последние n РЕАЛЬНЫХ фото (по имени),
    дедуп .jpg/.webp по основе (предпочтение .jpg). Папки `_*` пропускаем."""
    out: list[Path] = []
    dirs = sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith("_"))
    if limit_folders:
        dirs = dirs[:limit_folders]
    for d in dirs:
        by_stem: dict[str, Path] = {}
        for p in sorted(d.iterdir()):
            if not _is_real_photo(p):
                continue
            cur = by_stem.get(p.stem)
            if cur is None or (p.suffix.lower() in (".jpg", ".jpeg") and cur.suffix.lower() not in (".jpg", ".jpeg")):
                by_stem[p.stem] = p
        out.extend(sorted(by_stem.values(), key=lambda x: x.name)[-n:])
    return out


def _run_one(url: str, price: float, path: Path, timeout: float) -> dict:
    row = {c: "" for c in _COLUMNS}
    row["file"] = path.name
    row["group"] = path.parent.name      # имя папки = nm_id товара (ground truth)
    t0 = time.perf_counter()
    try:
        with path.open("rb") as fh:
            r = requests.post(
                f"{url.rstrip('/')}/api/v1/lookup",
                data={"purchase_price": str(price)},
                files={"image": (path.name, fh, "application/octet-stream")},
                timeout=timeout,
            )
        if r.status_code != 200:
            row["error"] = f"HTTP {r.status_code}: {r.text[:120]}"
            return row
        d = r.json()
        row.update({
            "verdict": d.get("verdict"),
            "wb_demand_verdict": d.get("wb_demand_verdict"),
            "subject": d.get("wb_subject_name"),
            "parent": d.get("wb_parent_name"),
            "recommended_units": d.get("recommended_units_year"),
            "baseline_per_position": d.get("baseline_units_per_position"),
            "wb_strength": d.get("wb_strength"),
            "liquidity_score": d.get("liquidity_score"),
            "wb_popularity": d.get("wb_popularity_score"),
            "analog_count": d.get("filtered_analog_count"),
            "top_count": d.get("top_count"),
            "forecast_source": d.get("forecast_source"),
            "key_reasons": ",".join(c for c in (d.get("verdict_reasons") or []) if c in _FLAG_REASONS),
            "duration_ms": d.get("duration_ms"),
        })
        found = d.get("top_seed_nm_id")
        row["found_seed_nm"] = found if found is not None else ""
        # папка названа nm_id товара → проверяем, нашёл ли visual search ИМЕННО его
        if found is not None and row["group"].isdigit():
            row["nm_found"] = 1 if str(found) == row["group"] else 0
    except Exception as e:  # noqa: BLE001
        row["error"] = f"{type(e).__name__}: {e}"
    finally:
        row["duration_ms"] = row["duration_ms"] or int((time.perf_counter() - t0) * 1000)
    return row


def main() -> int:
    ap = argparse.ArgumentParser(description="Полевая регрессия SAOL по папке фото")
    ap.add_argument("photos_dir", help="папка с фото товаров")
    ap.add_argument("--url", default="http://127.0.0.1:8088", help="базовый URL сервиса")
    ap.add_argument("--price", type=float, default=1000.0, help="закупочная цена (для margin)")
    ap.add_argument("--workers", type=int, default=3, help="параллельных запросов (WB не любит много)")
    ap.add_argument("--timeout", type=float, default=95.0)
    ap.add_argument("--out", default=None, help="путь к CSV (по умолч. ~/Downloads/saol_regression_<ts>.csv)")
    ap.add_argument("--last-per-folder", type=int, default=0,
                    help="режим подпапок: брать N последних РЕАЛЬНЫХ фото из каждой папки (имя папки=nm_id)")
    ap.add_argument("--limit-folders", type=int, default=0, help="ограничить число папок (для пробной партии)")
    args = ap.parse_args()

    root = Path(args.photos_dir).expanduser()
    if not root.is_dir():
        print(f"Нет папки: {root}", file=sys.stderr)
        return 2
    if args.last_per_folder:
        images = _select_last_per_folder(root, args.last_per_folder, args.limit_folders)
    else:
        images = sorted(p for p in root.rglob("*") if p.suffix.lower() in _IMG_EXT)
    if not images:
        print(f"В {root} нет изображений ({', '.join(sorted(_IMG_EXT))})", file=sys.stderr)
        return 2

    out = Path(args.out).expanduser() if args.out else (
        Path.home() / "Downloads" / f"saol_regression_{datetime.now():%Y%m%d_%H%M}.csv"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    print(f"Фото: {len(images)} · воркеров: {args.workers} · цена: {args.price:.0f}₽")
    print(f"CSV: {out}")

    done = 0
    verdicts: dict[str, int] = {}
    errors = 0
    flagged = 0
    nm_checked = 0
    nm_hits = 0
    with out.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=_COLUMNS)
        w.writeheader()
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(_run_one, args.url, args.price, p, args.timeout): p for p in images}
            for fut in as_completed(futs):
                row = fut.result()
                w.writerow(row)
                fh.flush()  # инкрементально — частичный прогон переживёт падение
                done += 1
                if row["error"]:
                    errors += 1
                else:
                    verdicts[row["verdict"] or "?"] = verdicts.get(row["verdict"] or "?", 0) + 1
                    if row["key_reasons"]:
                        flagged += 1
                if row["nm_found"] != "":
                    nm_checked += 1
                    nm_hits += int(row["nm_found"])
                mark = "ERR" if row["error"] else (row["verdict"] or "?")
                nm = "✓nm" if row["nm_found"] == 1 else ("✗nm" if row["nm_found"] == 0 else "")
                print(f"  [{done}/{len(images)}] {row['group']:>10}/{row['file']:<18} {mark:<8} "
                      f"{nm:<4} {row['subject'] or ''} закуп={row['recommended_units']}")

    print("\n=== сводка ===")
    for v, n in sorted(verdicts.items(), key=lambda x: -x[1]):
        print(f"  {v:<8} {n}")
    print(f"  ошибок: {errors} · с флагами-деградациями: {flagged}/{done}")
    if nm_checked:
        print(f"  visual search нашёл сам товар (nm из папки): {nm_hits}/{nm_checked}")
    print(f"\nОткрой CSV, проставь expected_verdict/note там, где система ошиблась: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
