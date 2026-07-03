from app.models import TaxonomyItem
from app.services import taxonomy


def _seed(db):
    db.add_all([
        TaxonomyItem(group="A", subgroup="A1", vid="A1a", wb_paths=["WB/A1/a"], ozon_paths=[], source_file="t"),
        TaxonomyItem(group="A", subgroup="A1", vid="A1b", wb_paths=[], ozon_paths=["OZ/x"], source_file="t"),
        TaxonomyItem(group="A", subgroup="A2", vid="A2a", wb_paths=[], ozon_paths=[], source_file="t"),
        TaxonomyItem(group="B", subgroup="B1", vid="B1a", wb_paths=["WB/B1/a"], ozon_paths=[], source_file="t"),
    ])
    db.commit()


def test_list_groups_counts(db):
    _seed(db)
    g = taxonomy.list_groups(db)
    assert {x["group"] for x in g} == {"A", "B"}
    a = next(x for x in g if x["group"] == "A")
    assert a["subgroup_count"] == 2
    assert a["vid_count"] == 3


def test_subgroups_and_vids(db):
    _seed(db)
    subs = taxonomy.list_subgroups(db, "A")
    assert {s["subgroup"] for s in subs} == {"A1", "A2"}
    vids = taxonomy.list_vids(db, "A", "A1")
    assert [v["vid"] for v in vids] == ["A1a", "A1b"]


def test_search_substring(db):
    _seed(db)
    res = taxonomy.search_vids(db, "a1a")
    assert any(r.vid == "A1a" for r in res)
