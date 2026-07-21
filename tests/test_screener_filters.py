"""选股过滤器单元测试。"""

import pandas as pd
from unittest.mock import patch

from quant.screener.filters import apply_commodity_filter


class TestApplyCommodityFilter:
    """商品映射过滤测试。"""

    def test_restricts_to_mapped_stocks(self):
        """应仅保留商品映射的股票。"""
        df = pd.DataFrame({
            "stock_code": ["000001", "000975", "600547", "000651"],
            "stock_name": ["平安银行", "山金国际", "山东黄金", "格力电器"],
            "market": ["CN_A", "CN_A", "CN_A", "CN_A"],
        })

        with patch(
            "quant.screener.filters.get_mapped_stocks",
            return_value=["000975", "600547"],
        ):
            result, n_before, n_after = apply_commodity_filter(
                df, "CN_A", ["XAU"]
            )

        assert n_before == 4
        assert n_after == 2
        assert sorted(result["stock_code"].tolist()) == ["000975", "600547"]

    def test_all_market_filters_cn_a_and_cn_hk(self):
        """market=all 时应对 CN_A 和 CN_HK 分别过滤。"""
        df = pd.DataFrame({
            "stock_code": ["000001", "000975", "600547", "02899"],
            "market": ["CN_A", "CN_A", "CN_A", "CN_HK"],
        })

        def mock_get_mapped(market, commodity):
            if market == "CN_A" and commodity == "XAU":
                return ["000975", "600547"]
            if market == "CN_HK" and commodity == "XAU":
                return ["02899"]
            return []

        with patch(
            "quant.screener.filters.get_mapped_stocks",
            side_effect=mock_get_mapped,
        ):
            result, n_before, n_after = apply_commodity_filter(
                df, "all", ["XAU"]
            )

        assert n_before == 4
        assert n_after == 3
        assert sorted(result["stock_code"].tolist()) == ["000975", "02899", "600547"]

    def test_empty_commodities_returns_unchanged(self):
        """无商品列表时保持原样。"""
        df = pd.DataFrame({
            "stock_code": ["000001", "000975"],
            "market": ["CN_A", "CN_A"],
        })
        result, n_before, n_after = apply_commodity_filter(df, "CN_A", [])
        assert n_after == n_before
        assert result["stock_code"].tolist() == ["000001", "000975"]

    def test_no_mapping_returns_unchanged(self):
        """商品无映射时保持原样。"""
        df = pd.DataFrame({
            "stock_code": ["000001", "000975"],
            "market": ["CN_A", "CN_A"],
        })

        with patch(
            "quant.screener.filters.get_mapped_stocks",
            side_effect=ValueError("no mapping"),
        ):
            result, n_before, n_after = apply_commodity_filter(
                df, "CN_A", ["UNKNOWN"]
            )

        assert n_after == n_before
        assert result["stock_code"].tolist() == ["000001", "000975"]
