"""FCF+ROE 深度价值策略 API。"""
from fastapi import APIRouter
from pydantic import BaseModel, Field

from web import ok, err
from web.wrappers import strategy_wrapper

router = APIRouter()

VALID_MARKETS = {"US", "CN_A", "CN_HK"}


class FcfRoeStrategyParams(BaseModel):
    market: str = Field(default="US", description="市场：US | CN_A | CN_HK")
    market_cap_min: int | None = Field(default=None, description="最低市值（覆盖预设默认值）")
    fcf_yield_min: float | None = Field(default=None, ge=0, le=1, description="最低 FCF Yield（0–1）")
    roe_min: float | None = Field(default=None, ge=0, le=1, description="最低 ROE（0–1）")
    top_n: int | None = Field(default=None, ge=1, le=100, description="返回数量（1–100）")


@router.post("/strategies/fcf-roe/run")
async def fcf_roe_strategy_run(params: FcfRoeStrategyParams):
    """运行 FCF+ROE 深度价值策略。

    固定规则（客户端不可覆盖）：
    - 金融行业排除
    - ST 排除
    - 最近连续 3 年 ROE ≥ 下限
    - 数据缺失即淘汰

    固定权重：FCF Yield 30% + CFO 质量 25% + PB 20% + 营收同比 15% + 毛利率 10%
    """
    try:
        result = strategy_wrapper.run_fcf_roe_strategy(
            market=params.market,
            market_cap_min=params.market_cap_min,
            fcf_yield_min=params.fcf_yield_min,
            roe_min=params.roe_min,
            top_n=params.top_n,
        )
        return ok(result)
    except ValueError as e:
        return err("invalid_request", str(e))
    except Exception as e:
        return err("strategy_error", str(e))
