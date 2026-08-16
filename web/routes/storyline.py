"""Storyline API endpoints — 故事线（财报时间线 + 公司大事件）."""
from fastapi import APIRouter, Query

from web import ok, err
from web.services import storyline_service

router = APIRouter()


@router.get("/storyline/timeline")
async def storyline_timeline(
    stock_code: str = Query(...),
):
    """个股故事线：统一返回 A股/港股/美股 的财报时间线与大事件。"""
    try:
        return ok(storyline_service.get_timeline(stock_code))
    except ValueError as e:
        return err("invalid_request", str(e))
    except Exception as e:
        return err("storyline_error", str(e))


@router.get("/storyline/kline")
async def storyline_kline(
    stock_code: str = Query(...),
    years: int = Query(10, ge=0, le=50),
    adjust: str = Query("qfq", pattern="^(qfq|none)$"),
):
    """日 K 线 OHLCV。years=0 返回全部历史；adjust=qfq 前复权（默认），none 不复权。"""
    try:
        return ok(storyline_service.get_kline(stock_code, years, adjust))
    except Exception as e:
        return err("storyline_error", str(e))
