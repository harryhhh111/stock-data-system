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
        assert result["not_in_stock_info"] == 1


# ── 美股行业分类测试 ──────────────────────────────────────


class TestFetchUsIndustry:
    """测试 fetch_us_industry 核心逻辑。"""

    @patch("fetchers.industry.requests.Session")
    def test_basic_fetch(self, mock_session_cls):
        """测试基本拉取流程：2 只美股。"""
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session

        def mock_get(url, timeout=10):
            resp = MagicMock()
            resp.status_code = 200
            if "0000320193" in url:  # AAPL
                resp.json.return_value = {
                    "sic": "3571",
                    "sicDescription": "Electronic Computers",
                }
            elif "0001067983" in url:  # BRK
                resp.json.return_value = {
                    "sic": "6331",
                    "sicDescription": "Fire, Marine & Casualty Insurance",
                }
            else:
                resp.json.return_value = {"sicDescription": ""}
            resp.raise_for_status = MagicMock()
            return resp

        mock_session.get.side_effect = mock_get

        from fetchers.industry import fetch_us_industry
        stocks = [
            {"stock_code": "AAPL", "cik": "0000320193"},
            {"stock_code": "BRK.B", "cik": "0001067983"},
        ]
        results = fetch_us_industry(stocks, delay=0, max_retries=1)

        assert len(results) == 2
        assert results[0]["stock_code"] == "AAPL"
        assert results[0]["industry_name"] == "Electronic Computers"
        assert results[1]["stock_code"] == "BRK.B"
        assert results[1]["industry_name"] == "Fire, Marine & Casualty Insurance"

    @patch("fetchers.industry.requests.Session")
    def test_empty_sic_description(self, mock_session_cls):
        """测试 sicDescription 为空时仍记录空字符串。"""
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session

        resp = MagicMock()
        resp.json.return_value = {"sic": "1234"}  # no sicDescription
        resp.raise_for_status = MagicMock()
        mock_session.get.return_value = resp

        from fetchers.industry import fetch_us_industry
        results = fetch_us_industry(
            [{"stock_code": "TEST", "cik": "0000000001"}],
            delay=0, max_retries=1,
        )

        assert len(results) == 1
        assert results[0]["industry_name"] == ""

    @patch("fetchers.industry.requests.Session")
    def test_retry_on_failure(self, mock_session_cls):
        """测试请求失败时重试。"""
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session

        call_count = {"n": 0}

        def mock_get(url, timeout=10):
            call_count["n"] += 1
            if call_count["n"] <= 2:
                raise ConnectionError("模拟超时")
            resp = MagicMock()
            resp.json.return_value = {"sicDescription": "Software"}
            resp.raise_for_status = MagicMock()
            return resp

        mock_session.get.side_effect = mock_get

        from fetchers.industry import fetch_us_industry
        results = fetch_us_industry(
            [{"stock_code": "MSFT", "cik": "0000789019"}],
            delay=0, max_retries=3,
        )

        assert len(results) == 1
        assert results[0]["industry_name"] == "Software"
        assert call_count["n"] == 3

    @patch("fetchers.industry.requests.Session")
    def test_all_retries_fail(self, mock_session_cls):
        """测试所有重试均失败时跳过该股票。"""
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        mock_session.get.side_effect = ConnectionError("持续失败")

        from fetchers.industry import fetch_us_industry
        results = fetch_us_industry(
            [{"stock_code": "FAIL", "cik": "0000000001"}],
            delay=0, max_retries=2,
        )

        assert len(results) == 0

    @patch("fetchers.industry.requests.Session")
    def test_cik_zero_padding(self, mock_session_cls):
        """测试 CIK 自动补零到 10 位。"""
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session

        captured_urls = []

        def mock_get(url, timeout=10):
            captured_urls.append(url)
            resp = MagicMock()
            resp.json.return_value = {"sicDescription": "Test Industry"}
            resp.raise_for_status = MagicMock()
            return resp

        mock_session.get.side_effect = mock_get

        from fetchers.industry import fetch_us_industry
        fetch_us_industry(
            [{"stock_code": "TEST", "cik": "320193"}],
            delay=0, max_retries=1,
        )

        assert len(captured_urls) == 1
        assert "CIK0000320193" in captured_urls[0]


class TestSyncUsIndustry:
    """测试 SyncManager.sync_us_industry 方法。"""

    @patch("sync.execute")
    @patch("fetchers.industry.requests.Session")
    def test_sync_us_industry_updates_stock_info(
        self, mock_session_cls, mock_execute
    ):
        """测试 sync_us_industry 正确 UPDATE stock_info。"""
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session

        def mock_get(url, timeout=10):
            resp = MagicMock()
            resp.json.return_value = {"sicDescription": "Electronic Computers"}
            resp.raise_for_status = MagicMock()
            return resp

        mock_session.get.side_effect = mock_get

        # Mock execute: 返回美股列表 + 允许 UPDATE
        def execute_side_effect(sql, params=None, **kwargs):
            if "SELECT stock_code, cik FROM stock_info" in sql:
                return [("AAPL", "0000320193"), ("MSFT", "0000789019")]
            return None

        mock_execute.side_effect = execute_side_effect

        from sync import SyncManager
        manager = SyncManager()
        result = manager.sync_us_industry()

        assert result["total"] == 2
        assert result["updated"] == 2
        assert result["empty_industry"] == 0
        assert result["industry_count"] == 1  # 都是 Electronic Computers

        # 验证 execute 被调用（UPDATE SQL）
        update_calls = [
            c for c in mock_execute.call_args_list
            if c.args and "UPDATE stock_info" in str(c.args[0])
        ]
        assert len(update_calls) > 0

    @patch("sync.execute")
    def test_sync_us_industry_no_stocks(self, mock_execute):
        """测试没有美股时返回空结果。"""
        mock_execute.return_value = []

        from sync import SyncManager
        manager = SyncManager()
        result = manager.sync_us_industry()

        assert result["total"] == 0
        assert result["updated"] == 0
