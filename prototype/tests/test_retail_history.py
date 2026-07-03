import openpyxl

from app.models import RetailHistoryItem, TaxonomyItem
from app.services.reason_codes import ReasonCode
from app.services.retail_history import lookup_retail_profile, retail_profile_reason_codes
from scripts.import_retail_history import import_sources


def test_import_retail_history_fills_context_and_skips_totals(db, tmp_path):
    path = tmp_path / "Реестр Игрушки для ИИ.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append([
        "Группа", "Подгруппа", "Вид", "Ценовая", "Товар", "Цена", "% наценки",
        "Рентабельность за период: 01.01.2025 - 31.12.2025",
        "Рентабельность за период: 01.01.2024 - 31.12.2024",
        "Продажи за период: 01.01.2025 - 31.12.2025",
        "Продажи за период: 01.01.2024 - 31.12.2024",
        "Остаток 31.12.2025",
        "Количество моделей 31.12.2025",
        "Количество моделей 31.12.2024",
    ])
    ws.append(["Игрушки", "Мячи", "Мяч пляжный", "0 - 999", "МЯЧ 20 см", 199, 2.1, 24, 20, 50, 40, 10, 3, 2])
    ws.append([None, None, None, None, "МЯЧ 30 см", 299, 2.3, 28, 22, 60, 50, 20, 4, 3])
    ws.append([None, None, None, "0 - 999 Итог", None, None, None, None, None, 110, 90, 30, 7, 5])
    wb.save(path)

    stats = import_sources([path], all_xlsx=True)
    rows = db.query(RetailHistoryItem).order_by(RetailHistoryItem.product_name).all()

    assert stats["inserted"] == 2
    assert [r.product_name for r in rows] == ["МЯЧ 20 см", "МЯЧ 30 см"]
    assert rows[1].group == "Игрушки"
    assert rows[1].subgroup == "Мячи"
    assert rows[1].vid == "Мяч пляжный"
    assert rows[1].period_current == "01.01.2025 - 31.12.2025"


def test_lookup_retail_profile_flags_offline_risks(db):
    db.add(TaxonomyItem(
        group="Игрушки",
        subgroup="Мячи",
        vid="Мяч пляжный",
        sold_qty=0,
        stock_qty=0,
        sold_rub=0,
        stock_rub=0,
        cost_sold=0,
        cost_stock=0,
        wb_paths=["Спорт/Мячи пляжные"],
        ozon_paths=[],
        source_file="taxonomy.xlsx",
    ))
    db.add(RetailHistoryItem(
        group="Игрушки",
        subgroup="Мячи",
        vid="Мяч пляжный",
        price_band="0 - 999",
        product_name="МЯЧ",
        price=250,
        markup=1.2,
        profitability_current=-3,
        profitability_prev=20,
        sales_current=10,
        sales_prev=100,
        stock_current=120,
        source_file="registry.xlsx",
        source_sheet="Лист1",
    ))
    db.commit()

    profile = lookup_retail_profile(db, wb_path="Спорт/Мячи пляжные")
    codes = retail_profile_reason_codes(profile)

    assert profile is not None
    assert profile.match_kind == "exact"
    assert profile.item_count == 1
    assert ReasonCode.OFFLINE_OVERSTOCK.value in codes
    assert ReasonCode.OFFLINE_DECLINING.value in codes
    assert ReasonCode.LOW_OFFLINE_PROFITABILITY.value in codes


def test_lookup_retail_profile_falls_back_to_internal_vid_stem(db):
    db.add(RetailHistoryItem(
        group="Спорт",
        subgroup="Бассейны",
        vid="Бассейн надувной детский",
        price_band="0 - 9999",
        product_name="БАССЕЙН",
        price=1199,
        markup=130,
        profitability_current=-24,
        profitability_prev=-10,
        sales_current=20,
        sales_prev=40,
        stock_current=0,
        source_file="bestway.xlsx",
        source_sheet="Реестр",
    ))
    db.commit()

    profile = lookup_retail_profile(db, wb_path="Спорт/Бассейны надувные")
    codes = retail_profile_reason_codes(profile)

    assert profile is not None
    assert profile.match_kind == "retail_stem"
    assert profile.item_count == 1
    assert ReasonCode.LOW_OFFLINE_PROFITABILITY.value in codes
