"""Dashboard API endpoints."""
from fastapi import APIRouter

from web import ok, err
from web.services.dashboard_service import get_stats

router = APIRouter()


@router.get("/dashboard/stats")
async def dashboard_stats():
    """仪表板聚合数据。"""
    try:
        return ok(get_stats())
    except Exception as e:
        return err("dashboard_error", str(e))
