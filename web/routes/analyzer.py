"""Analyzer API endpoints."""
from fastapi import APIRouter, Query

from web import ok, err
from web.wrappers import analyzer_wrapper

router = APIRouter()


@router.get("/analyzer/search")
async def analyzer_search(
    q: str = Query(""),
    market: str | None = Query(None),
):
    """股票搜索。需要 market 指定查哪个市场，market=all 返回 400。"""
    try:
        return ok(analyzer_wrapper.search_stocks(q, market))
    except ValueError as e:
        return err("invalid_request", str(e))
    except Exception as e:
        return err("analyzer_search_error", str(e))


@router.get("/analyzer/analyze")
async def analyzer_analyze(
    stock_code: str = Query(...),
    market: str | None = Query(None),
):
    """个股分析报告。"""
    try:
        return ok(analyzer_wrapper.get_report(stock_code, market))
    except ValueError as e:
        return err("invalid_request", str(e))
    except Exception as e:
        return err("analyzer_error", str(e))
