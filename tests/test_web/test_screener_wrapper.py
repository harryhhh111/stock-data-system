"""选股筛选器 web wrapper 测试。"""

from datetime import date
from unittest.mock import patch, MagicMock

import pandas as pd
import pytest

from web.wrappers import screener_wrapper


class TestRunScreener:
    """run_screener 结果结构测试。"""

    @pytest.fixture
    def mock_universe(self):
        """构造一个能通过 fcf_roe_value / gold_value 硬过滤的选股池。"""
        return pd.DataFrame({
            "stock_code": ["000001", "000002", "000975"],
            "stock_name": ["平安银行", "万科A", "山金国际"],
            "market": ["CN_A", "CN_A", "CN_A"],
            "industry": ["医药生物", "食品饮料", "有色金属"],
            "market_cap": [1e10, 2e10, 5e10],
            "pe_ttm": [6.0, 8.0, 10.0],
            "pb": [0.8, 1.0, 1.5],
            "dividend_yield": [0.03, 0.02, 0.01],
            "fcf_yield": [0.15, 0.13, 0.16],
            "roe": [0.13, 0.14, 0.1992],
            "gross_margin": [0.30, 0.25, 0.35],
            "net_margin": [0.10, 0.08, 0.15],
            "debt_ratio": [0.50, 0.40, 0.30],
            # 打分所需派生因子
            "cfo_ttm": [1.2e9, 1.5e9, 2.0e9],
            "net_profit_ttm": [1.0e9, 1.0e9, 1.5e9],
            "revenue_yoy": [0.10, 0.15, 0.20],
        })

    @pytest.fixture
    def mock_roe_history(self):
        """构造 3 年 ROE 历史，全部 >= 12%。"""
        return pd.DataFrame({
            "stock_code": (
                ["000001"] * 3 +
                ["000002"] * 3 +
                ["000975"] * 3
            ),
            "report_date": (
                [date(2025, 12, 31), date(2024, 12, 31), date(2023, 12, 31)] * 3
            ),
            "roe": [
                0.130, 0.125, 0.121,   # 000001
                0.140, 0.135, 0.130,   # 000002
                0.1992, 0.1674, 0.1232,  # 000975
            ],
        })

    def test_results_include_roe_history_columns(self, mock_universe, mock_roe_history):
        """run_screener 应返回 roe_1y_ago / roe_2y_ago，且值正确。"""
        with patch("web.wrappers.screener_wrapper.get_universe", return_value=mock_universe), \
             patch("web.wrappers.screener_wrapper.compute_dividend_yield", side_effect=lambda x: x), \
             patch("web.wrappers.screener_wrapper.get_roe_history", return_value=mock_roe_history):

            result = screener_wrapper.run_screener("CN_A", "fcf_roe_value", 10)

        assert result["total"] >= 1
        first = result["results"][0]
        assert "roe" in first
        assert "roe_1y_ago" in first
        assert "roe_2y_ago" in first
        # 000975 在 mock 中 ROE 最高，应排在第一位
        assert first["stock_code"] == "000975"
        assert round(first["roe"], 4) == 0.1992
        assert round(first["roe_1y_ago"], 4) == 0.1674
        assert round(first["roe_2y_ago"], 4) == 0.1232

    def test_results_exclude_roe_3y_ago(self, mock_universe, mock_roe_history):
        """不应返回多余的 roe_3y_ago 列。"""
        with patch("web.wrappers.screener_wrapper.get_universe", return_value=mock_universe), \
             patch("web.wrappers.screener_wrapper.compute_dividend_yield", side_effect=lambda x: x), \
             patch("web.wrappers.screener_wrapper.get_roe_history", return_value=mock_roe_history):

            result = screener_wrapper.run_screener("CN_A", "fcf_roe_value", 10)

        for r in result["results"]:
            assert "roe_3y_ago" not in r

    def test_commodity_preset_restricts_universe(self, mock_universe, mock_roe_history):
        """gold_value 预设应先限制到黄金映射股票，再过滤。"""
        with patch("web.wrappers.screener_wrapper.get_universe", return_value=mock_universe), \
             patch("web.wrappers.screener_wrapper.compute_dividend_yield", side_effect=lambda x: x), \
             patch("web.wrappers.screener_wrapper.get_roe_history", return_value=mock_roe_history), \
             patch("quant.screener.filters.get_mapped_stocks", return_value=["000975"]):

            result = screener_wrapper.run_screener("CN_A", "gold_value", 10)

        codes = {r["stock_code"] for r in result["results"]}
        assert codes == {"000975"}
