"""
tests/test_industry.py — 申万一级行业分类拉取测试
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest


# ── fetchers/industry.py 单元测试 ──────────────────────────


class TestFetchSwIndustry:
    """测试 fetch_sw_industry 核心逻辑。"""

    @pytest.fixture
    def mock_l1_df(self):
        """模拟 sw_index_first_info 返回的一级行业 DataFrame。"""
        return pd.DataFrame({
            "行业代码": ["801010.SI", "801040.SI", "801780.SI"],
            "行业名称": ["农林牧渔", "钢铁", "银行"],
            "成份个数": [3, 2, 2],
        })

    @pytest.fixture
    def mock_cons_dfs(self):
        """模拟各行业的成分股 DataFrame。"""
        return {
            "801010": pd.DataFrame({
                "证券代码": ["000505", "000592", "000702"],
                "证券名称": ["京粮控股", "平潭发展", "正虹科技"],
            }),
            "801040": pd.DataFrame({
                "证券代码": ["000708", "000717"],
                "证券名称": ["中信特钢", "韶钢松山"],
            }),
            "801780": pd.DataFrame({
                "证券代码": ["000001", "002142"],
                "证券名称": ["平安银行", "宁波银行"],
            }),
        }

    @patch("fetchers.industry._fetch_index_component")
    @patch("fetchers.industry._fetch_sw_first_info")
    def test_basic_fetch(self, mock_l1, mock_cons, mock_l1_df, mock_cons_dfs):
        """测试基本拉取流程：3 个行业，7 只股票。"""
        mock_l1.return_value = mock_l1_df
        mock_cons.side_effect = lambda symbol: mock_cons_dfs[symbol]

        from fetchers.industry import fetch_sw_industry
        results = fetch_sw_industry(delay=0, max_retries=1)

        assert len(results) == 7
        # 验证结构
        assert all("stock_code" in r and "industry_name" in r for r in results)

        # 验证行业映射正确
        code_to_industry = {r["stock_code"]: r["industry_name"] for r in results}
        assert code_to_industry["000505"] == "农林牧渔"
        assert code_to_industry["000708"] == "钢铁"
        assert code_to_industry["000001"] == "银行"

    @patch("fetchers.industry._fetch_index_component")
    @patch("fetchers.industry._fetch_sw_first_info")
    def test_partial_failure(self, mock_l1, mock_cons, mock_l1_df):
        """测试部分行业拉取失败时不影响其他行业。"""
        mock_l1.return_value = mock_l1_df

        call_count = {"n": 0}

        def cons_side_effect(symbol):
            if symbol == "801040":
                call_count["n"] += 1
                raise ConnectionError("模拟连接失败")
            if symbol == "801010":
                return pd.DataFrame({
                    "证券代码": ["000505"],
                    "证券名称": ["京粮控股"],
                })
            if symbol == "801780":
                return pd.DataFrame({
                    "证券代码": ["000001"],
                    "证券名称": ["平安银行"],
                })

        mock_cons.side_effect = cons_side_effect

        from fetchers.industry import fetch_sw_industry
        results = fetch_sw_industry(delay=0, max_retries=1)

        # 钢铁行业失败，但农林牧渔和银行成功
        assert len(results) == 2
        codes = {r["stock_code"] for r in results}
        assert "000505" in codes
        assert "000001" in codes
        assert "000708" not in codes  # 钢铁行业失败

    @patch("fetchers.industry._fetch_sw_first_info")
    def test_l1_fetch_failure(self, mock_l1):
        """测试一级行业列表拉取失败时抛出异常。"""
        mock_l1.side_effect = ConnectionError("无法连接")

        from fetchers.industry import fetch_sw_industry
        with pytest.raises(ConnectionError):
            fetch_sw_industry(delay=0)


class TestIndustryDistribution:
    """测试行业分布统计。"""

    def test_normal_distribution(self):
        """测试正常分布统计。"""
        from fetchers.industry import get_industry_distribution
        results = [
            {"stock_code": "000001", "industry_name": "银行"},
            {"stock_code": "002142", "industry_name": "银行"},
            {"stock_code": "000505", "industry_name": "农林牧渔"},
        ]
        dist = get_industry_distribution(results)
        assert len(dist) == 2
        bank_row = dist[dist["industry_name"] == "银行"]
        assert bank_row["stock_count"].values[0] == 2
        farm_row = dist[dist["industry_name"] == "农林牧渔"]
        assert farm_row["stock_count"].values[0] == 1

    def test_empty_results(self):
        """测试空结果。"""
        from fetchers.industry import get_industry_distribution
        dist = get_industry_distribution([])
        assert len(dist) == 0


# ── SyncManager.sync_industry 集成测试 ─────────────────────


class TestSyncIndustry:
    """测试 SyncManager.sync_industry 方法。"""

    @patch("sync.execute")
    @patch("fetchers.industry._fetch_index_component")
    @patch("fetchers.industry._fetch_sw_first_info")
    def test_sync_industry_updates_stock_info(
        self, mock_l1, mock_cons, mock_execute
    ):
        """测试 sync_industry 正确 UPDATE stock_info。"""
        # Mock 一级行业列表
        mock_l1.return_value = pd.DataFrame({
            "行业代码": ["801780.SI"],
            "行业名称": ["银行"],
            "成份个数": [2],
        })

        # Mock 成分股
        mock_cons.return_value = pd.DataFrame({
            "证券代码": ["000001", "002142"],
            "证券名称": ["平安银行", "宁波银行"],
        })

        # Mock execute: stock_info 中有这两只 A 股
        def execute_side_effect(sql, params=None, **kwargs):
            if "SELECT stock_code FROM stock_info" in sql:
                return [("000001",), ("002142",)]
            return None

        mock_execute.side_effect = execute_side_effect

        from sync import SyncManager
        manager = SyncManager()
        result = manager.sync_industry()

        assert result["total"] == 2
        assert result["updated"] == 2
        assert result["not_in_stock_info"] == 0
        assert result["industry_count"] == 1

        # 验证 execute 被调用（UPDATE SQL）
        update_calls = [
            c for c in mock_execute.call_args_list
            if c.args and "UPDATE stock_info" in str(c.args[0])
        ]
        assert len(update_calls) > 0

    @patch("sync.execute")
    @patch("fetchers.industry._fetch_index_component")
    @patch("fetchers.industry._fetch_sw_first_info")
    def test_sync_industry_skips_unknown_stocks(
        self, mock_l1, mock_cons, mock_execute
    ):
        """测试不在 stock_info 中的股票被跳过。"""
        mock_l1.return_value = pd.DataFrame({
            "行业代码": ["801780.SI"],
            "行业名称": ["银行"],
            "成份个数": [2],
        })

        mock_cons.return_value = pd.DataFrame({
            "证券代码": ["000001", "999999"],
            "证券名称": ["平安银行", "未知股票"],
        })

        # stock_info 中只有 000001
        def execute_side_effect(sql, params=None, **kwargs):
            if "SELECT stock_code FROM stock_info" in sql:
                return [("000001",)]
            return None

        mock_execute.side_effect = execute_side_effect

        from sync import SyncManager
        manager = SyncManager()
        result = manager.sync_industry()

        assert result["total"] == 2
        assert result["not_in_stock_info"] == 1  # 999999 不在 stock_info
        assert result["updated"] == 1
