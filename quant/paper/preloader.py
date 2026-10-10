"""PaperPreloader — 模拟盘的轻量级 PIT 数据适配器。

CN 市场继续委托既有 SQL 查询。US 旧 SQL 依赖的宽表/MV 已在 Phase E-1
退役，因此 US 必须委托流式 ``PITPreloader``，与正式回测共用版本事实
as-of 选择、事实排除与 TTM 构建语义，而不把整套历史事实加载到内存。
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from quant.backtest.universe import get_point_in_time_universe, get_roe_history_as_of


_UNIVERSE_NUMERIC_COLUMNS = (
    "total_shares", "parent_equity", "total_equity", "total_assets", "total_liab",
    "fcf", "annual_fcf", "revenue_ttm", "net_profit_ttm", "cfo_ttm", "capex_ttm",
    "gross_margin", "operating_margin", "net_margin", "roe", "debt_ratio",
    "current_ratio", "quick_ratio", "revenue_yoy", "net_profit_yoy",
    "eps_basic", "eps_diluted",
)

_REQUIRED_UNIVERSE_COLUMNS = (
    "total_shares", "parent_equity", "total_equity", "total_assets", "total_liab",
    "fcf", "annual_fcf", "revenue_ttm", "net_profit_ttm", "cfo_ttm", "capex_ttm",
    "report_date",
)


class PaperPreloader:
    def __init__(self, market: str) -> None:
        self.market = market
        self._us_preloader = None

    def _get_us_preloader(self):
        """惰性构造一份流式 US PIT loader，供 universe / ROE 历史复用。"""
        from quant.backtest import us_pit_source

        if not us_pit_source.us_backtest_pit_enabled():
            raise RuntimeError(
                "US 模拟盘 PIT 已迁移至版本事实层；请启用 "
                "US_BACKTEST_PIT_VERSION=1，不能回退到已退役的旧财务表。"
            )
        if self._us_preloader is None:
            from quant.backtest.preloader import PITPreloader

            self._us_preloader = PITPreloader("US", pit_streaming=True)
            self._us_preloader.load()
        return self._us_preloader

    @staticmethod
    def _normalise_universe(df: pd.DataFrame) -> pd.DataFrame:
        """保持模拟盘消费的数值/缺列契约，绝不把财务缺失填成零。"""
        result = df.copy()
        for col in _UNIVERSE_NUMERIC_COLUMNS:
            if col in result.columns:
                result[col] = pd.to_numeric(result[col], errors="coerce").astype("float64")
        for col in _REQUIRED_UNIVERSE_COLUMNS:
            if col not in result.columns:
                result[col] = np.nan
        return result

    def get_universe(self, as_of_date: date) -> pd.DataFrame:
        if self.market == "US":
            df = self._get_us_preloader().get_universe(as_of_date)
        else:
            df = get_point_in_time_universe(as_of_date, market=self.market)
        return self._normalise_universe(df)

    def get_roe_history(self, as_of_date: date, years: int) -> pd.DataFrame:
        if self.market == "US":
            result = self._get_us_preloader().get_roe_history(as_of_date, years)
        else:
            result = get_roe_history_as_of(as_of_date, self.market, years)
        if "roe" in result.columns:
            result = result.copy()
            result["roe"] = pd.to_numeric(result["roe"], errors="coerce").astype("float64")
        return result
