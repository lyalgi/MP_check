from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas import TaxonomyGroupOut, TaxonomySubgroupOut, TaxonomyVidOut
from app.services import taxonomy

router = APIRouter(prefix="/api/v1/taxonomy", tags=["Таксономия"])


@router.get("/groups", response_model=list[TaxonomyGroupOut])
def get_groups(db: Session = Depends(get_db)):
    return taxonomy.list_groups(db)


@router.get("/subgroups", response_model=list[TaxonomySubgroupOut])
def get_subgroups(group: str = Query(...), db: Session = Depends(get_db)):
    return taxonomy.list_subgroups(db, group)


@router.get("/vids", response_model=list[TaxonomyVidOut])
def get_vids(
    group: str = Query(...),
    subgroup: str = Query(...),
    db: Session = Depends(get_db),
):
    return taxonomy.list_vids(db, group, subgroup)


@router.get("/search")
def search(q: str = Query(..., min_length=2), limit: int = 20, db: Session = Depends(get_db)):
    items = taxonomy.search_vids(db, q, limit=limit)
    return [
        {
            "group": i.group,
            "subgroup": i.subgroup,
            "vid": i.vid,
            "tovaroved": i.tovaroved,
            "wb_paths": i.wb_paths or [],
        }
        for i in items
    ]
