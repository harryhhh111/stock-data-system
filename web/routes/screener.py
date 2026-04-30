"""Screener API endpoints."""
from fastapi import APIRouter
from pydantic import BaseModel

from web import ok, err
from web.wrappers import screener_wrapper

router = APIRouter()


class ScreenerParams(BaseModel):
    market: str = "all"
    preset: str | None = None
    top_n: int = 30


@router.get("/screener/presets")
async def screener_presets():
    """预设策略列表。"""
    try:
        return ok(screener_wrapper.get_presets())
    except Exception as e:
        return err("screener_presets_error", str(e))


@router.post("/screener/run")
async def screener_run(params: ScreenerParams):
    """运行筛选。"""
    try:
        return ok(screener_wrapper.run_screener(params.market, params.preset, params.top_n))
    except ValueError as e:
        return err("invalid_request", str(e))
    except Exception as e:
        return err("screener_error", str(e))
