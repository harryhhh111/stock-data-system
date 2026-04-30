"""Sync API endpoints."""
from fastapi import APIRouter, Query

from web import ok, err
from web.services import sync_service

router = APIRouter()


@router.get("/sync/status")
async def sync_status(market: str | None = Query(None)):
    """同步进度摘要（市场级）。"""
    try:
        return ok(sync_service.get_status(market))
    except Exception as e:
        return err("sync_status_error", str(e))


@router.get("/sync/progress")
async def sync_progress(
    market: str | None = Query(None),
    limit: int = Query(100),
    offset: int = Query(0),
):
    """个股同步进度。"""
    try:
        return ok(sync_service.get_progress(market, limit, offset))
    except Exception as e:
        return err("sync_progress_error", str(e))


@router.get("/sync/log")
async def sync_log(
    market: str | None = Query(None),
    limit: int = Query(50),
    offset: int = Query(0),
):
    """同步日志历史。"""
    try:
        return ok(sync_service.get_log(market, limit, offset))
    except Exception as e:
        return err("sync_log_error", str(e))
