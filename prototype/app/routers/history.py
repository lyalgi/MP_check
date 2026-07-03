from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import LookupHistory
from app.schemas import HistoryItemOut

router = APIRouter(prefix="/api/v1", tags=["История"])


@router.get("/history", response_model=list[HistoryItemOut])
def get_history(limit: int = Query(50, le=200), db: Session = Depends(get_db)):
    rows = (
        db.query(LookupHistory)
        .order_by(LookupHistory.created_at.desc())
        .limit(limit)
        .all()
    )
    return [
        HistoryItemOut(
            id=r.id,
            created_at=r.created_at,
            purchase_price=r.purchase_price,
            rating=r.rating,
            verdict=r.verdict,
            decision_confidence=r.decision_confidence,
            verdict_reasons=r.verdict_reasons or [],
            wb_subject_name=r.wb_subject_name,
            advice=r.advice,
        )
        for r in rows
    ]
