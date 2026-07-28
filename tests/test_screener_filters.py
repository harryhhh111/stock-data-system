"""选股过滤器单元测试。"""

import pandas as pd
from unittest.mock import patch

from quant.screener.filters import apply_commodity_filter, apply_hard_filters
from quant.screener.presets import PRESETS


class TestFcfRoeValueFinancialExclusion:
    """FCF+ROE 深度价值策略必须在全部市场排除固定金融行业。"""

    def test_excludes_us_financial_sic_industries(self):
        df = pd.DataFrame({
            "stock_code": ["ADBE", "PGR", "AMP", "OTHER"],
            "stock_name": ["Adobe", "Progressive", "Ameriprise", "Other"],
            "market": ["US", "US", "US", "US"],
            "industry": [
                "Services-Prepackaged Software",
                "Fire, Marine & Casualty Insurance",
                "Investment Advice",
                "Unclassified",
            ],
            "market_cap": [100e9, 100e9, 100e9, 100e9],
            "fcf_yield": [0.12, 0.12, 0.12, 0.12],
            "roe": [0.20, 0.20, 0.20, 0.20],
        })

        result, _, _ = apply_hard_filters(
            df, PRESETS["fcf_roe_value"]["filters"]
        )

        assert result["stock_code"].tolist() == ["ADBE", "OTHER"]


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
