"""PaperPreloader — 轻量级 PIT 数据适配器。

委托 SQL-driven 的 universe / roe history 查询函数，
避免 PITPreloader 的全量内存加载。实现与 PITPreloader
相同的 get_universe / get_roe_history 接口，使 composite
引擎函数无需修改即可复用。
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from quant.backtest.universe import get_point_in_time_universe, get_roe_history_as_of


class PaperPreloader:
    def __init__(self, market: str) -> None:
        self.market = market

    def get_universe(self, as_of_date: date) -> pd.DataFrame:
        df = get_point_in_time_universe(as_of_date, market=self.market)
        # PITPreloader 返回 float64，但 SQL 路径可能返回 Decimal (object)。
        # 统一转换为 float 来兼容 build_universe 的算术操作。
        numeric_cols = [
            "net_profit_ttm", "cfo_ttm", "capex_ttm",
            "gross_margin", "net_margin", "roe",
            "revenue_yoy", "net_profit_yoy", "debt_ratio",
            "current_ratio", "quick_ratio",
            "eps_basic", "eps_diluted",
        ]
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce").astype("float64")
        # build_universe 的 fillna 需要这些列。必须用 np.nan（float）而非 None（object），
        # 否则 float * None 会产生 object Series，污染后续除法结果。
        for col in (
            "total_shares", "parent_equity", "total_equity",
            "fcf", "report_date",
        ):
            if col not in df.columns:
                df[col] = np.nan
        return df

    def get_roe_history(self, as_of_date: date, years: int) -> pd.DataFrame:
        return get_roe_history_as_of(as_of_date, self.market, years)
