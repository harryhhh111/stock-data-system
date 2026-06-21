"""Quality API endpoints."""
from fastapi import APIRouter, Query
from pydantic import BaseModel

from web import ok, err
from web.services import quality_service

router = APIRouter()


class AcknowledgeRequest(BaseModel):
    reason: str | None = None


@router.on_event("startup")
def _migrate():
    """应用启动时确保确认表存在。"""
    quality_service.ensure_acknowledgments_table()


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
    include_acknowledged: bool = Query(False),
    limit: int = Query(50),
    offset: int = Query(0),
):
    """问题列表。默认排除已确认问题。"""
    try:
        return ok(quality_service.get_issues(severity, market, check, limit, offset, include_acknowledged))
    except Exception as e:
        return err("quality_issues_error", str(e))


@router.post("/quality/issues/{issue_id}/acknowledge")
async def quality_acknowledge(issue_id: int, body: AcknowledgeRequest):
    """确认某条校验记录无问题。"""
    try:
        success = quality_service.acknowledge_issue(issue_id, None, body.reason)
        if not success:
            return err("not_found", "Issue not found")
        return ok({"acknowledged": True})
    except Exception as e:
        return err("acknowledge_error", str(e))


@router.post("/quality/issues/{issue_id}/unacknowledge")
async def quality_unacknowledge(issue_id: int):
    """取消确认。"""
    try:
        quality_service.unacknowledge_issue(issue_id)
        return ok({"acknowledged": False})
    except Exception as e:
        return err("unacknowledge_error", str(e))
