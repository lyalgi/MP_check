from app.models import WbOzonMapping
from app.services.mapping import find_mapping
from app.services.providers.wb_public import WBPublicProvider


def _seed(db):
    db.add_all([
        WbOzonMapping(wb_path="Детям/Для мальчиков/Белье/Трусы",
                      ozon_path="Одежда/Белье/Трусы"),
        WbOzonMapping(wb_path="Дом/Кухня/Чайники",
                      ozon_path="Дом и сад/Кухня/Чайники"),
    ])
    db.commit()


def test_exact_match(db):
    _seed(db)
    r = find_mapping(db, "Детям/Для мальчиков/Белье/Трусы")
    assert r.match_kind == "exact"
    assert r.ozon_path == "Одежда/Белье/Трусы"


def test_by_subject(db):
    _seed(db)
    r = find_mapping(db, wb_path=None, subject_name="Трусы")
    assert r.match_kind == "by_subject"
    assert r.ozon_path == "Одежда/Белье/Трусы"


def test_by_subject_stem_plural(db):
    db.add_all([
        WbOzonMapping(wb_path="Детям/Подарки детям/Книги и канцтовары/Ручка",
                      ozon_path="Детям"),
        WbOzonMapping(wb_path="Детям/Подарки детям/Книги и канцтовары/Ручка",
                      ozon_path="Канцтовары/Письменные принадлежности/Ручка"),
    ])
    db.commit()
    r = find_mapping(db, wb_path="Канцелярские товары/Ручки", subject_name="Ручки")
    assert r.match_kind == "by_subject_stem"
    assert r.ozon_path == "Канцтовары/Письменные принадлежности/Ручка"


def test_prefix_fallback(db):
    _seed(db)
    r = find_mapping(db, "Детям/Для мальчиков/Белье/НесуществующийВид")
    assert r.match_kind == "prefix"
    assert r.ozon_path == "Одежда/Белье/Трусы"


def test_no_match(db):
    _seed(db)
    r = find_mapping(db, "Что-то совсем другое")
    assert r.match_kind == "none"
    assert r.ozon_path is None


def test_wb_menu_name_match_does_not_shorten_multiword_subject():
    assert WBPublicProvider._menu_name_matches("Ручки", "Ручка")
    assert WBPublicProvider._menu_name_matches("Игрушки антистресс", "Игрушка антистресс")
    assert not WBPublicProvider._menu_name_matches("Коляски", "Коляски для кукол")
