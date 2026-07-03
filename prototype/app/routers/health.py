from fastapi import APIRouter

from app.settings import settings

router = APIRouter()


@router.get("/healthz")
def healthz():
    return {
        "ok": True,
        "analytics_provider": settings.analytics_provider,
        "visual_search_provider": settings.visual_search_provider,
        "min_feedbacks": settings.min_feedbacks,
    }
