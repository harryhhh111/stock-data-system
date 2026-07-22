"""多因子打分方向性测试。"""

import pandas as pd
import pytest

from quant.screener.scorer import rank_factors


class TestRankFactors:
    """rank_factors 百分位排名方向测试。"""

    @pytest.fixture
    def single_factor_df(self):
        return pd.DataFrame({
            "stock_code": ["A", "B", "C"],
            "industry": ["X", "X", "X"],
            "fcf_yield": [0.10, 0.20, 0.30],
            "pb": [3.0, 2.0, 1.0],
            "cfo_ttm": [100.0, 100.0, 100.0],
            "net_profit_ttm": [100.0, 100.0, 100.0],
        })

    def test_higher_better_gets_higher_score(self, single_factor_df):
        """ascending=False 时，因子值越大，得分应越高。"""
        weights = {"fcf_yield": {"weight": 1.0, "ascending": False}}
        scored = rank_factors(single_factor_df, weights, by_industry=False)

        # 期望 C(0.30) > B(0.20) > A(0.10)
        scores = scored.set_index("stock_code")["score"]
        assert scores["C"] > scores["B"] > scores["A"]
        assert scores["C"] == pytest.approx(100.0)
        assert scores["A"] == pytest.approx(100.0 / 3, abs=1e-6)

    def test_lower_better_gets_higher_score(self, single_factor_df):
        """ascending=True 时，因子值越小，得分应越高。"""
        weights = {"pb": {"weight": 1.0, "ascending": True}}
        scored = rank_factors(single_factor_df, weights, by_industry=False)

        # 期望 C(PB=1.0) > B(PB=2.0) > A(PB=3.0)
        scores = scored.set_index("stock_code")["score"]
        assert scores["C"] > scores["B"] > scores["A"]
        assert scores["C"] == pytest.approx(100.0)
        assert scores["A"] == pytest.approx(100.0 / 3, abs=1e-6)

    def test_score_rank_orders_by_score_desc(self, single_factor_df):
        """score_rank 应按综合得分降序排列，rank=1 为最高分。"""
        weights = {
            "fcf_yield": {"weight": 0.5, "ascending": False},
            "pb": {"weight": 0.5, "ascending": True},
        }
        scored = rank_factors(single_factor_df, weights, by_industry=False)

        # C 在 fcf_yield 和 pb 上都是最好，应排第一
        ranks = scored.set_index("stock_code")["score_rank"]
        assert ranks["C"] == 1
        assert ranks["A"] == 3
