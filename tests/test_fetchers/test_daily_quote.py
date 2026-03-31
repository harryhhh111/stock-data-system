"""
tests/test_fetchers/test_daily_quote.py — 日线行情 fetcher 测试
"""
from __future__ import annotations

import pytest
import pandas as pd
import numpy as np
from datetime import date, datetime
from unittest.mock import patch, MagicMock


# ── 测试数据构造 ────────────────────────────────────────

@pytest.fixture
def a_hist_df():
    """模拟 A 股历史日线 DataFrame。"""
    return pd.DataFrame({
        "日期": ["2025-01-02", "2025-01-03"],
        "股票代码": ["000001", "000001"],
        "开盘": [11.73, 11.44],
        "收盘": [11.43, 11.38],
        "最高": [11.77, 11.54],
        "最低": [11.39, 11.36],
        "成交量": [1819597, 1154680],
        "成交额": [2.102923e+09, 1.320521e+09],
        "振幅": [3.25, 1.57],
        "涨跌幅": [-2.31, -0.44],
        "涨跌额": [-0.27, -0.05],
        "换手率": [0.94, 0.60],
    })


@pytest.fixture
def a_spot_df():
    """模拟 A 股实时行情 DataFrame（含市值）。"""
    return pd.DataFrame({
        "序号": [1, 2],
        "代码": ["000001", "600519"],
        "名称": ["平安银行", "贵州茅台"],
        "最新价": [11.35, 1520.0],
        "涨跌幅": [-0.35, 1.20],
        "涨跌额": [-0.04, 18.0],
        "成交量": [644946, 12580],
        "成交额": [7.32e+08, 1.91e+10],
        "振幅": [0.53, 1.50],
        "最高": [11.40, 1535.0],
        "最低": [11.34, 1502.0],
        "今开": [11.39, 1510.0],
        "昨收": [11.39, 1502.0],
        "量比": [0.85, 1.10],
        "换手率": [0.33, 0.10],
        "市盈率-动态": [5.20, 22.50],
        "市净率": [0.50, 8.90],
        "总市值": [2.204e+11, 1.909e+12],
        "流通市值": [2.204e+11, 1.909e+12],
    })


@pytest.fixture
def hk_hist_df():
    """模拟港股历史日线 DataFrame。"""
    return pd.DataFrame({
        "日期": ["2025-01-02", "2025-01-03"],
        "开盘": [417.0, 418.0],
        "收盘": [416.0, 414.2],
        "最高": [425.0, 419.0],
        "最低": [413.6, 410.4],
        "成交量": [20733037, 16843241],
        "成交额": [8.664e+09, 6.982e+09],
        "振幅": [2.73, 2.07],
        "涨跌幅": [-0.24, -0.43],
        "涨跌额": [-1.0, -1.8],
        "换手率": [0.22, 0.18],
    })


@pytest.fixture
def hk_spot_df():
    """模拟港股实时行情 DataFrame（含市值，来自东方财富 API 直调）。"""
    return pd.DataFrame({
        "代码": ["00700", "09988"],
        "名称": ["腾讯控股", "阿里巴巴-W"],
        "最新价": [480.0, 105.0],
        "涨跌额": [5.0, 2.5],
        "涨跌幅": [1.05, 2.44],
        "今开": [475.0, 103.0],
        "最高": [482.0, 106.0],
        "最低": [474.0, 102.0],
        "昨收": [475.0, 102.5],
        "成交量": [20000000, 30000000],
        "成交额": [9.56e+09, 3.15e+09],
        "总市值": [4.5e+12, 2.0e+12],
        "市盈率-动态": [18.5, 22.0],
        "市净率": [4.2, 1.9],
        "行业": ["互联网服务", "电子商务"],
    })


# ── transform_a_hist_to_records 测试 ────────────────────

class TestTransformAHist:
    def test_basic_conversion(self, a_hist_df):
        from fetchers.daily_quote import transform_a_hist_to_records
        records = transform_a_hist_to_records(a_hist_df)
        assert len(records) == 2
        r = records[0]
        assert r["stock_code"] == "000001"
        assert r["trade_date"] == date(2025, 1, 2)
        assert r["open"] == 11.73
        assert r["close"] == 11.43
        assert r["high"] == 11.77
        assert r["low"] == 11.39
        assert r["volume"] == 1819597
        assert r["amount"] is not None
        assert r["turnover_rate"] == 0.94
        assert r["market_cap"] is None  # 历史数据无市值
        assert r["market"] == "CN_A"
        assert r["currency"] == "CNY"

    def test_empty_df(self):
        from fetchers.daily_quote import transform_a_hist_to_records
        df = pd.DataFrame()
        records = transform_a_hist_to_records(df)
        assert records == []


class TestTransformASpot:
    def test_basic_conversion(self, a_spot_df):
        from fetchers.daily_quote import transform_a_spot_to_records
        records = transform_a_spot_to_records(a_spot_df)
        assert len(records) == 2
        r0 = records[0]
        assert r0["stock_code"] == "000001"
        assert r0["close"] == 11.35
        assert r0["market_cap"] == pytest.approx(2.204e+11)
        assert r0["float_market_cap"] == pytest.approx(2.204e+11)
        assert r0["pe_ttm"] == pytest.approx(5.20)
        assert r0["pb"] == pytest.approx(0.50)
        assert r0["market"] == "CN_A"
        assert r0["trade_date"] == datetime.now().date()

    def test_market_cap_fields(self, a_spot_df):
        from fetchers.daily_quote import transform_a_spot_to_records
        records = transform_a_spot_to_records(a_spot_df)
        r1 = records[1]  # 贵州茅台
        assert r1["stock_code"] == "600519"
        assert r1["market_cap"] == pytest.approx(1.909e+12)


class TestTransformHkHist:
    def test_basic_conversion(self, hk_hist_df):
        from fetchers.daily_quote import transform_hk_hist_to_records
        records = transform_hk_hist_to_records(hk_hist_df, "00700")
        assert len(records) == 2
        r = records[0]
        assert r["stock_code"] == "00700"
        assert r["trade_date"] == date(2025, 1, 2)
        assert r["close"] == 416.0
        assert r["market_cap"] is None
        assert r["market"] == "CN_HK"
        assert r["currency"] == "HKD"

    def test_empty_df(self):
        from fetchers.daily_quote import transform_hk_hist_to_records
        df = pd.DataFrame()
        records = transform_hk_hist_to_records(df, "00700")
        assert records == []


class TestTransformHkSpot:
    def test_basic_conversion(self, hk_spot_df):
        from fetchers.daily_quote import transform_hk_spot_to_records
        records, industry_map = transform_hk_spot_to_records(hk_spot_df)
        assert len(records) == 2
        r = records[0]
        assert r["stock_code"] == "00700"
        assert r["close"] == 480.0
        assert r["market_cap"] == pytest.approx(4.5e+12)
        assert r["pe_ttm"] == pytest.approx(18.5)
        assert r["pb"] == pytest.approx(4.2)
        assert r["market"] == "CN_HK"
        assert r["currency"] == "HKD"
        # 行业映射
        assert industry_map == {"00700": "互联网服务", "09988": "电子商务"}

    def test_market_cap_fields(self, hk_spot_df):
        from fetchers.daily_quote import transform_hk_spot_to_records
        records, industry_map = transform_hk_spot_to_records(hk_spot_df)
        r1 = records[1]  # 阿里巴巴
        assert r1["stock_code"] == "09988"
        assert r1["market_cap"] == pytest.approx(2.0e+12)
        assert r1["pe_ttm"] == pytest.approx(22.0)
        assert r1["pb"] == pytest.approx(1.9)

    def test_backward_compat_no_cap(self):
        """旧格式（不含市值列）兼容测试。"""
        from fetchers.daily_quote import transform_hk_spot_to_records
        old_df = pd.DataFrame({
            "代码": ["00700"],
            "名称": ["腾讯控股"],
            "最新价": [480.0],
            "涨跌额": [5.0],
            "涨跌幅": [1.05],
            "今开": [475.0],
            "最高": [482.0],
            "最低": [474.0],
            "昨收": [475.0],
            "成交量": [20000000],
            "成交额": [9.56e+09],
        })
        records, industry_map = transform_hk_spot_to_records(old_df)
        assert len(records) == 1
        r = records[0]
        assert r["close"] == 480.0
        assert r["market_cap"] is None  # 旧格式无市值
        assert r["pe_ttm"] is None
        assert r["pb"] is None
        assert industry_map == {}  # 旧格式无行业


# ── 辅助函数测试 ────────────────────────────────────────

class TestSafeConversions:
    def test_safe_float_normal(self):
        from fetchers.daily_quote import _safe_float
        assert _safe_float(11.5) == 11.5
        assert _safe_float("3.14") == 3.14

    def test_safe_float_nan(self):
        from fetchers.daily_quote import _safe_float
        assert _safe_float(float("nan")) is None
        assert _safe_float(None) is None

    def test_safe_int_normal(self):
        from fetchers.daily_quote import _safe_int
        assert _safe_int(100) == 100
        assert _safe_int("200") == 200

    def test_safe_int_nan(self):
        from fetchers.daily_quote import _safe_int
        assert _safe_int(float("nan")) is None
        assert _safe_int(None) is None


# ── Fetcher mock 测试 ───────────────────────────────────

class TestDailyQuoteFetcherMocked:
    """测试 fetcher 的 API 调用逻辑（mock akshare）。"""

    @patch("fetchers.daily_quote.ak.stock_zh_a_hist")
    def test_fetch_a_hist(self, mock_hist, a_hist_df):
        mock_hist.return_value = a_hist_df
        from fetchers.daily_quote import DailyQuoteFetcher
        fetcher = DailyQuoteFetcher()
        df = fetcher.fetch_a_hist("000001", start_date="20250101", end_date="20250110")
        assert len(df) == 2
        mock_hist.assert_called_once()

    @patch("fetchers.daily_quote.ak.stock_hk_hist")
    def test_fetch_hk_hist(self, mock_hist, hk_hist_df):
        mock_hist.return_value = hk_hist_df
        from fetchers.daily_quote import DailyQuoteFetcher
        fetcher = DailyQuoteFetcher()
        df = fetcher.fetch_hk_hist("00700", start_date="20250101", end_date="20250110")
        assert len(df) == 2
        mock_hist.assert_called_once()

    @patch("fetchers.daily_quote.ak.stock_zh_a_spot_em")
    def test_fetch_a_spot_eastmoney(self, mock_spot, a_spot_df):
        """东方财富正常时直接返回。"""
        mock_spot.return_value = a_spot_df
        from fetchers.daily_quote import DailyQuoteFetcher
        fetcher = DailyQuoteFetcher()
        df = fetcher.fetch_a_spot()
        assert len(df) == 2
        mock_spot.assert_called_once()

    @patch("fetchers.daily_quote.requests.get")
    @patch("fetchers.daily_quote.ak.stock_info_a_code_name")
    @patch("fetchers.daily_quote.ak.stock_zh_a_spot_em")
    def test_fetch_a_spot_tencent_fallback(self, mock_em, mock_codes, mock_get):
        """东方财富失败时 fallback 到腾讯接口。"""
        mock_em.side_effect = ConnectionError("eastmoney unreachable")

        # 模拟 A 股代码列表
        mock_codes.return_value = pd.DataFrame({
            "code": ["000001", "600519", "300001"],
            "name": ["平安银行", "贵州茅台", "特锐德"],
        })

        # 模拟腾讯接口响应
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = (
            'v_sz000001="51~平安银行~000001~11.08~10.99~11.00~1164565~698118~466448~11.08~7424~11.07~2293~11.06~4178~11.05~6597~11.04~1043~11.09~69~11.10~3037~11.11~2561~11.12~3242~11.13~3491~~20260331161500~0.09~0.82~11.18~10.99~11.08/1164565/1294675716~1164565~129468~0.60~5.04~~11.18~10.99~1.73~2150.14~2150.18~0.48~12.09~9.89~1.24~9135~11.12~5.04~5.04~~~0.56~129467.5716~0.0000~0~ ~GP-A~-2.89~1.84~5.40~7.73~0.72~13.09~9.88~0.45~1.84~-4.15~19405600653~19405918198~26.92~-8.31~19405600653~~~3.92~0.09~~CNY~0~~11.18~-31206~";'
            'v_sh600519="1~贵州茅台~600519~1450.00~1420.00~1468.00~61691~32574~29117~1450.00~518~1449.99~1~1449.94~1~1449.90~2~1449.88~1~1453.00~1~1454.00~1~1454.96~1~1455.00~18~1455.99~3~~20260331161419~30.00~2.11~1479.93~1448.41~1450.00/61691/9015486797~61691~901549~0.49~20.17~~1479.93~1448.41~2.22~18157.92~18157.92~8.00~1562.00~1278.00~2.26~499~1461.40~21.07~21.06~~~0.53~901548.6797~0.0000~0~ ~GP-A~5.29~3.03~3.56~35.02~30.58~1593.44~1322.01~-2.36~1.67~2.53~1252270215~1252270215~91.22~4.64~1252270215~~~-3.94~-0.28~~CNY~0~___D__F__N~1456.00~-144~";'
            'v_sz300001="51~特锐德~300001~25.50~25.00~25.20~50000~25000~25000~25.50~100~25.49~200~25.48~300~25.47~400~25.46~500~25.51~100~25.52~200~25.53~300~25.54~400~25.55~500~~20260331161500~0.50~2.00~25.80~24.90~25.50/50000/127500000~50000~1275~1.50~15.00~~25.80~24.90~3.60~250.00~250.00~3.00~27.50~22.50~1.50~100~25.40~15.00~15.00~~~1.55~12750.0000~0.0000~0~ ~GP-A~-1.00~2.50~3.80~12.00~1.20~28.50~23.00~-2.00~2.50~-1.50~1000000000~1000000000~15.00~3.50~1000000000~~~-2.00~0.10~~CNY~0~~25.55~-500~";'
        )
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        from fetchers.daily_quote import DailyQuoteFetcher
        fetcher = DailyQuoteFetcher()
        df = fetcher.fetch_a_spot()

        assert len(df) == 3
        # 验证列与东方财富格式一致
        for col in ["代码", "名称", "最新价", "今开", "最高", "最低", "昨收",
                     "成交量", "成交额", "总市值", "流通市值", "市盈率-动态", "市净率", "换手率"]:
            assert col in df.columns, f"缺少列: {col}"

        # 验证第一只股票（平安银行）数据
        row = df[df["代码"] == "000001"].iloc[0]
        assert row["最新价"] == 11.08
        assert row["昨收"] == 10.99
        assert row["今开"] == 11.00
        assert row["成交量"] == 1164565 * 100  # 手 → 股
        assert row["涨跌额"] == pytest.approx(0.09)
        assert row["涨跌幅"] == pytest.approx(0.82)
        assert row["市净率"] == pytest.approx(0.48)
        # 总市值: 2150.18亿 → 2.15018e11
        assert row["总市值"] == pytest.approx(2150.18e8, rel=1e-3)

        # 验证贵州茅台
        row2 = df[df["代码"] == "600519"].iloc[0]
        assert row2["最新价"] == 1450.00
        assert row2["市净率"] == pytest.approx(8.00)
        assert row2["总市值"] == pytest.approx(18157.92e8, rel=1e-3)

        # 验证成交额单位转换（万 → 元）
        # 129467.5716万 = 1294675716 元
        assert row["成交额"] == pytest.approx(129467.5716e4, rel=1e-3)

    @patch("fetchers.daily_quote.requests.get")
    @patch("fetchers.daily_quote.ak.stock_info_a_code_name")
    @patch("fetchers.daily_quote.ak.stock_zh_a_spot_em")
    def test_fetch_a_spot_tencent_unit_conversion(self, mock_em, mock_codes, mock_get):
        """测试腾讯 fallback 的单位转换。"""
        mock_em.side_effect = ConnectionError("fail")
        mock_codes.return_value = pd.DataFrame({
            "code": ["600519"],
            "name": ["贵州茅台"],
        })

        # 用茅台真实数据验证
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = (
            'v_sh600519="1~贵州茅台~600519~1450.00~1420.00~1468.00~61691~32574~29117~1450.00~518~1449.99~1~1449.94~1~1449.90~2~1449.88~1~1453.00~1~1454.00~1~1454.96~1~1455.00~18~1455.99~3~~20260331161419~30.00~2.11~1479.93~1448.41~1450.00/61691/9015486797~61691~901549~0.49~20.17~~1479.93~1448.41~2.22~18157.92~18157.92~8.00~1562.00~1278.00~2.26~499~1461.40~21.07~21.06~~~0.53~901548.6797~0.0000~0~ ~GP-A~5.29~3.03~3.56~35.02~30.58~1593.44~1322.01~-2.36~1.67~2.53~1252270215~1252270215~91.22~4.64~1252270215~~~-3.94~-0.28~~CNY~0~___D__F__N~1456.00~-144~";'
        )
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        from fetchers.daily_quote import DailyQuoteFetcher
        fetcher = DailyQuoteFetcher()
        df = fetcher.fetch_a_spot()

        assert len(df) == 1
        row = df.iloc[0]
        # 成交量: 61691手 = 6169100股
        assert row["成交量"] == 61691 * 100
        # 成交额: 901548.6797万 ≈ 9015486797元
        assert row["成交额"] == pytest.approx(901548.6797e4, rel=1e-4)
        # 总市值: 18157.92亿 = 1.815792e12元
        assert row["总市值"] == pytest.approx(18157.92e8, rel=1e-3)
        # 流通市值: 18157.92亿
        assert row["流通市值"] == pytest.approx(18157.92e8, rel=1e-3)

    @patch("fetchers.daily_quote.requests.get")
    def test_fetch_hk_spot(self, mock_get, hk_spot_df):
        """测试港股行情拉取（mock requests.get）。"""
        from fetchers.daily_quote import DailyQuoteFetcher
        fetcher = DailyQuoteFetcher()

        # 模拟东方财富 API 响应
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "data": {
                "total": 2,
                "diff": [
                    {"f12": "00700", "f14": "腾讯控股", "f2": 480.0, "f4": 5.0, "f3": 1.05,
                     "f17": 475.0, "f15": 482.0, "f16": 474.0, "f18": 475.0,
                     "f5": 20000000, "f6": 9.56e+09, "f20": 4.5e+12, "f9": 18.5, "f23": 4.2},
                    {"f12": "09988", "f14": "阿里巴巴-W", "f2": 105.0, "f4": 2.5, "f3": 2.44,
                     "f17": 103.0, "f15": 106.0, "f16": 102.0, "f18": 102.5,
                     "f5": 30000000, "f6": 3.15e+09, "f20": 2.0e+12, "f9": 22.0, "f23": 1.9},
                ]
            }
        }
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        df = fetcher.fetch_hk_spot()
        assert len(df) == 2
        assert "总市值" in df.columns
        assert "市盈率-动态" in df.columns
        assert "市净率" in df.columns
        mock_get.assert_called_once()
