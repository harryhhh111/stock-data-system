"""Quality API endpoints."""
from fastapi import APIRouter, Query

from web import ok, err
from web.services import quality_service

router = APIRouter()


@router.get("/quality/summary")
async def quality_summary():
    """质量问题汇总。"""
    try:
        return ok(quality_service.get_summary())
    except Exception as e:
        return err("quality_summary_error", str(e))


@router.get("/quality/issues")
async def quality_issues(
    severity: str | None = Query(None),
    market: str | None = Query(None),
    check: str | None = Query(None),
    limit: int = Query(50),
    offset: int = Query(0),
):
    """问题列表。"""
    try:
        return ok(quality_service.get_issues(severity, market, check, limit, offset))
    except Exception as e:
        return err("quality_issues_error", str(e))
