"""Phase B2 美股筛选 snapshot 路径单元测试（mock DB，不依赖实库）。

覆盖规格 §7：
1. 开关分发（legacy / snapshot；CN/AH 不变）；
2. 新 SQL 静态与运行时不读六个旧对象、不读供应商 PE/PB；
5. gross_margin=NULL 的硬过滤与打分权重归一化；
6. 连续 ROE 的 NULL 不顶替、金融行业排除；
7. 行业中位数排除自身、亏损 PE / 非正 PB / 缺失 FCF 不进中位数。
"""

from datetime import date
from unittest.mock import patch

import pandas as pd
import pytest

from quant.analyzer import query_us
from quant.screener import query as screener_query
from quant.screener import presets
from quant.screener.filters import apply_hard_filters, filter_consecutive_roe
from quant.screener.scorer import rank_factors


class _DummyConn:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


FORBIDDEN = query_us._LEGACY_FORBIDDEN_OBJECTS


def _raw_universe_rows() -> pd.DataFrame:
    """load_us_snapshot_universe 的 SQL 原始返回形状（估值/状态列尚未计算）。"""
    return pd.DataFrame({
        "stock_code": ["AAA", "LOSS", "NOEQ", "EXC", "NOSNAP"],
        "stock_name": ["A", "L", "N", "E", "S"],
        "market": ["US"] * 5,
        "industry": ["Ind"] * 5,
        "list_date": [date(2000, 1, 1)] * 5,
        "days_since_list": [9000] * 5,
        "quote_date": [date(2026, 8, 4)] * 5,
        "close": [10.0] * 5,
        "market_cap": [1.0e9, 1.0e9, 1.0e9, 1.0e9, 1.0e9],
        "quote_currency": ["USD"] * 5,
        "annual_report_date": [date(2025, 12, 31)] * 4 + [None],
        "annual_filed_date": [date(2026, 2, 1)] * 4 + [None],
        "annual_accession_no": ["acc"] * 4 + [None],
        "roe": [0.2, -0.1, 0.15, 0.18, None],
        "gross_margin": [0.4, 0.3, 0.2, 0.5, None],
        "operating_margin": [0.2] * 4 + [None],
        "net_margin": [0.1] * 4 + [None],
        "debt_ratio": [0.5] * 4 + [None],
        "current_ratio": [1.5] * 4 + [None],
        "quick_ratio": [1.0] * 4 + [None],
        "revenue_yoy": [0.1] * 4 + [None],
        "net_profit_yoy": [0.1] * 4 + [None],
        "eps_basic": [1.0] * 4 + [None],
        "revenues": [1.0e9] * 4 + [None],
        "total_assets": [2.0e9] * 4 + [None],
        "total_liabilities": [1.0e9] * 4 + [None],
        "annual_total_equity": [1.0e9] * 4 + [None],
        "annual_fcf": [1.0e8] * 4 + [None],
        "annual_quality_flags": [[]] * 5,
        "ttm_report_date": [date(2026, 3, 31)] * 4 + [None],
        "ttm_filed_date": [date(2026, 5, 1)] * 4 + [None],
        "ttm_accession_no": ["acc-ttm"] * 4 + [None],
        "revenue_ttm": [1.0e9] * 4 + [None],
        "net_income_ttm": [1.0e8, -5.0e7, 5.0e7, 8.0e7, None],
        "net_income_common_ttm": [None] * 5,
        "cfo_ttm": [1.2e8] * 4 + [None],
        "capex_ttm": [2.0e7] * 4 + [None],
        "fcf_ttm": [1.0e8, 5.0e7, 3.0e7, None, None],
        "equity_report_date": [date(2026, 3, 31)] * 4 + [None],
        # NOEQ：equity 在行情日之后才披露 → PB 必须为 NULL
        "equity_filed_date": [date(2026, 5, 1), date(2026, 5, 1),
                              date(2026, 9, 1), date(2026, 5, 1), None],
        "total_equity": [5.0e8, 5.0e8, 5.0e8, 5.0e8, None],
        "quality_flags": [[]] * 5,
    })


def _load_universe(raw: pd.DataFrame, exceptions_path: str | None = None):
    env = {}
    if exceptions_path is not None:
        env["US_PHASE_A_EXCEPTIONS_PATH"] = exceptions_path
    with patch.dict("os.environ", env), \
         patch("quant.analyzer.query_us.Connection", return_value=_DummyConn()), \
         patch("quant.analyzer.query_us.pd.read_sql", return_value=raw.copy()) as read_sql:
        out = query_us.load_us_snapshot_universe()
    return out, read_sql


# ── 1. 开关分发 ──────────────────────────────────────────────


class TestFlagDispatch:
    def test_universe_flag_off_uses_legacy(self, monkeypatch):
        monkeypatch.delenv("US_SCREENER_SNAPSHOT_CURRENT", raising=False)
        with patch.object(screener_query, "get_us_universe_legacy") as legacy, \
             patch.object(screener_query, "get_us_universe_snapshot") as snap:
            screener_query.get_us_universe()
        legacy.assert_called_once()
        snap.assert_not_called()

    def test_universe_flag_on_uses_snapshot(self, monkeypatch):
        monkeypatch.setenv("US_SCREENER_SNAPSHOT_CURRENT", "1")
        with patch.object(screener_query, "get_us_universe_legacy") as legacy, \
             patch.object(screener_query, "get_us_universe_snapshot") as snap:
            screener_query.get_us_universe()
        snap.assert_called_once()
        legacy.assert_not_called()

    def test_roe_history_us_flag_on_reads_snapshot(self, monkeypatch):
        monkeypatch.setenv("US_SCREENER_SNAPSHOT_CURRENT", "1")
        captured = []

        def fake_read_sql(sql, conn, params=None):
            captured.append(sql)
            return pd.DataFrame({"stock_code": [], "report_date": [], "roe": []})

        with patch("quant.screener.query.Connection", return_value=_DummyConn()), \
             patch("quant.screener.query.pd.read_sql", side_effect=fake_read_sql):
            screener_query.get_roe_history("US", 3)
        assert len(captured) == 1
        assert "us_financial_current_annual" in captured[0]
        for obj in FORBIDDEN:
            assert obj not in captured[0]

    def test_roe_history_us_flag_off_uses_legacy(self, monkeypatch):
        monkeypatch.delenv("US_SCREENER_SNAPSHOT_CURRENT", raising=False)
        captured = []
        with patch("quant.screener.query.Connection", return_value=_DummyConn()), \
             patch("quant.screener.query.pd.read_sql",
                   side_effect=lambda sql, conn, params=None: captured.append(sql)
                   or pd.DataFrame({"stock_code": [], "report_date": [], "roe": []})):
            screener_query.get_roe_history("US", 3)
        assert "mv_us_financial_indicator" in captured[0]

    def test_roe_history_cn_unchanged_by_flag(self, monkeypatch):
        monkeypatch.setenv("US_SCREENER_SNAPSHOT_CURRENT", "1")
        captured = []
        with patch("quant.screener.query.Connection", return_value=_DummyConn()), \
             patch("quant.screener.query.pd.read_sql",
                   side_effect=lambda sql, conn, params=None: captured.append(sql)
                   or pd.DataFrame({"stock_code": [], "report_date": [], "roe": []})):
            screener_query.get_roe_history("CN_A", 3)
        assert len(captured) == 1
        assert "mv_financial_indicator" in captured[0]
        assert "us_financial_current_annual" not in captured[0]

    def test_roe_history_all_appends_us_snapshot_only_when_flag_on(self, monkeypatch):
        monkeypatch.setenv("US_SCREENER_SNAPSHOT_CURRENT", "1")
        cn = pd.DataFrame({
            "stock_code": ["000001"], "report_date": [date(2025, 12, 31)], "roe": [0.2],
        })
        us = pd.DataFrame({
            "stock_code": ["AAPL"], "report_date": [date(2025, 12, 31)], "roe": [0.3],
        })
        calls = []

        def fake_read_sql(sql, conn, params=None):
            calls.append(sql)
            return us.copy() if "us_financial_current_annual" in sql else cn.copy()

        with patch("quant.screener.query.Connection", return_value=_DummyConn()), \
             patch("quant.screener.query.pd.read_sql", side_effect=fake_read_sql):
            out = screener_query.get_roe_history(None, 3)
        assert len(calls) == 2
        assert set(out["stock_code"]) == {"000001", "AAPL"}

    def test_industry_stats_dispatch(self, monkeypatch):
        monkeypatch.setenv("US_SCREENER_SNAPSHOT_CURRENT", "1")
        with patch("quant.analyzer.query_us._snapshot_industry_stats") as snap, \
             patch("quant.analyzer.query_us.pd.read_sql") as legacy_sql:
            query_us.get_industry_stats("Ind", "US", "AAA")
        snap.assert_called_once_with("Ind", "AAA")
        legacy_sql.assert_not_called()

    def test_strategy_wrapper_uses_snapshot_universe(self, monkeypatch):
        """strategy wrapper 经 get_us_universe 自动分发，固定权重/行业排除不变。"""
        monkeypatch.setenv("US_SCREENER_SNAPSHOT_CURRENT", "1")
        from web.wrappers import strategy_wrapper

        universe = pd.DataFrame({
            "stock_code": ["AAA", "JPM"],
            "stock_name": ["A", "JPMorgan"],
            "market": ["US", "US"],
            "industry": ["Ind", "National Commercial Banks"],
            "market_cap": [5e9, 5e9],
            "pe_ttm": [10.0, 8.0],
            "pb": [2.0, 1.0],
            "fcf_yield": [0.15, 0.20],
            "roe": [0.20, 0.15],
            "gross_margin": [0.4, None],
            "net_margin": [0.1, None],
            "debt_ratio": [0.4, None],
            "revenue_yoy": [0.1, 0.1],
            "cfo_ttm": [1.2e8, 1.0e8],
            "net_profit_ttm": [1.0e8, 1.0e8],
            "ttm_report_date": [date(2026, 3, 31)] * 2,
            "quote_date": [date(2026, 8, 4)] * 2,
            "financial_data_status": ["snapshot_available"] * 2,
            "net_income_basis": ["consolidated"] * 2,
        })
        roe_hist = pd.DataFrame({
            "stock_code": ["AAA"] * 3 + ["JPM"] * 3,
            "report_date": [date(2025, 12, 31), date(2024, 12, 31), date(2023, 12, 31)] * 2,
            "roe": [0.2, 0.2, 0.2, 0.15, 0.15, 0.15],
        })
        with patch("web.wrappers.strategy_wrapper.get_us_universe", return_value=universe), \
             patch("web.wrappers.strategy_wrapper.get_roe_history", return_value=roe_hist):
            result = strategy_wrapper.run_fcf_roe_strategy(market="US")

        codes = [r["stock_code"] for r in result["results"]]
        assert codes == ["AAA"], "金融行业（National Commercial Banks）必须被固定排除"
        first = result["results"][0]
        assert first["financial_data_status"] == "snapshot_available"
        assert first["net_income_basis"] == "consolidated"
        assert first["quote_date"] is not None
        # 固定权重不得改变
        assert result["weights"] == strategy_wrapper.FIXED_WEIGHTS_FLAT
        assert strategy_wrapper.FIXED_WEIGHTS["fcf_yield"]["weight"] == 0.30


# ── 2. 新 SQL 静态与运行时禁读校验 ──────────────────────────


class TestSqlForbiddenObjects:
    def test_universe_sql_static(self):
        sql = query_us._SQL_US_SNAPSHOT_UNIVERSE
        for obj in FORBIDDEN:
            assert obj not in sql
        assert "us_financial_current_annual" in sql
        assert "us_financial_current_ttm" in sql
        assert "daily_quote" in sql
        # 不读供应商估值
        assert "pe_ttm" not in sql
        assert "q.pb" not in sql

    def test_roe_history_sql_static(self):
        sql = screener_query._SQL_US_ROE_HISTORY_SNAPSHOT
        for obj in FORBIDDEN:
            assert obj not in sql
        assert "us_financial_current_annual" in sql
        # 连续 ROE：不得过滤 NULL（先取行后判断）
        assert "IS NOT NULL" not in sql

    def test_universe_sql_runtime(self):
        _, read_sql = _load_universe(_raw_universe_rows())
        sql = read_sql.call_args[0][0]
        for obj in FORBIDDEN:
            assert obj not in sql
        assert "pe_ttm" not in sql


# ── 3/4. universe 估值与状态（合成数据口径） ─────────────────


class TestUniverseValuation:
    def test_pe_pb_fcf_and_status(self):
        out, _ = _load_universe(_raw_universe_rows())
        by = out.set_index("stock_code")
        # PE = 市值 / TTM 净利润（仅正利润）
        assert by.loc["AAA", "pe_ttm"] == pytest.approx(10.0)
        assert pd.isna(by.loc["LOSS", "pe_ttm"]), "净亏损 PE 必须为 NULL"
        # 负 FCF Yield 保留负值；正常值 = fcf_ttm / market_cap
        assert by.loc["AAA", "fcf_yield"] == pytest.approx(0.1)
        assert by.loc["LOSS", "fcf_yield"] == pytest.approx(0.05)
        # PB = 市值 / parent equity；NOEQ 权益披露日晚于行情日 → NULL
        assert by.loc["AAA", "pb"] == pytest.approx(2.0)
        assert pd.isna(by.loc["NOEQ", "pb"])
        assert by.loc["NOEQ", "pb_equity_date"] is None or pd.isna(by.loc["NOEQ", "pb_equity_date"])
        # 无 snapshot：财务/估值 NULL，status 显式
        assert by.loc["NOSNAP", "financial_data_status"] == query_us.STATUS_SNAPSHOT_UNAVAILABLE
        assert pd.isna(by.loc["NOSNAP", "pe_ttm"])
        assert by.loc["NOSNAP", "net_income_basis"] == "unavailable"
        # 溯源列存在
        for col in ("ttm_report_date", "ttm_filed_date", "ttm_accession_no",
                    "quote_date", "equity_report_date", "quality_flags"):
            assert col in out.columns

    def test_selector_exception_status(self, tmp_path):
        csv = tmp_path / "exc.csv"
        csv.write_text(
            "stock_code,report_date,field,reason,allowed_base_reason,evidence_ref,registered_at\n"
            "EXC,2026-03-31,fcf_ttm,NO_CASH_CAPEX_DISCLOSURE_TTM,MISSING_COMPONENT,ref,2026-08-05\n"
        )
        out, _ = _load_universe(_raw_universe_rows(), exceptions_path=str(csv))
        by = out.set_index("stock_code")
        assert by.loc["EXC", "financial_data_status"] == query_us.STATUS_SELECTOR_EXCEPTION
        assert pd.isna(by.loc["EXC", "fcf_yield"]), "exception 的 FCF Yield 必须为 NULL"

    def test_missing_exception_csv_warns(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING, logger="quant.analyzer.query_us"):
            query_us._load_registered_exceptions.cache_clear() if hasattr(
                query_us._load_registered_exceptions, "cache_clear") else None
            with patch.dict("os.environ",
                            {"US_PHASE_A_EXCEPTIONS_PATH": "/nonexistent/exc.csv"}):
                keys = query_us._load_registered_exceptions()
        assert keys == frozenset()
        assert any("not found" in r.message for r in caplog.records)

    def test_effective_net_income_common_fallback(self):
        raw = _raw_universe_rows()
        raw.loc[0, "net_income_ttm"] = None
        raw.loc[0, "net_income_common_ttm"] = 5.0e7
        out, _ = _load_universe(raw)
        by = out.set_index("stock_code")
        assert by.loc["AAA", "net_income_basis"] == "common"
        assert by.loc["AAA", "net_profit_ttm"] == pytest.approx(5.0e7)
        assert by.loc["AAA", "pe_ttm"] == pytest.approx(20.0)


# ── 5. gross_margin=NULL 语义 ────────────────────────────────


class TestGrossMarginNull:
    def test_hard_filter_rejects_null_gross_margin(self):
        df = pd.DataFrame({
            "stock_code": ["CCI", "AAA"],
            "stock_name": ["Crown", "A"],
            "market": ["US", "US"],
            "industry": ["Ind", "Ind"],
            "market_cap": [5e9, 5e9],
            "pe_ttm": [30.0, 10.0],
            "debt_ratio": [0.5, 0.5],
            "gross_margin": [None, 0.3],
        })
        filtered, _, n_after = apply_hard_filters(
            df, presets.PRESETS["classic_value"]["filters"],
        )
        assert n_after == 1
        assert filtered["stock_code"].tolist() == ["AAA"], \
            "gross_margin=NULL 在硬阈值中不通过，不得填 0 或行业均值"

    def test_scorer_reweights_valid_factors_only(self):
        df = pd.DataFrame({
            "stock_code": ["CCI", "AAA", "BBB"],
            "industry": ["Ind"] * 3,
            "fcf_yield": [0.15, 0.12, 0.10],
            "cfo_ttm": [1.2e8, 1.1e8, 1.0e8],
            "net_profit_ttm": [1.0e8, 1.0e8, 1.0e8],
            "pb": [2.0, 3.0, 4.0],
            "revenue_yoy": [0.1, 0.2, 0.3],
            "gross_margin": [None, 0.3, 0.4],
        })
        from web.wrappers.strategy_wrapper import FIXED_WEIGHTS
        scored = rank_factors(df, FIXED_WEIGHTS)
        cci = scored[scored["stock_code"] == "CCI"].iloc[0]
        assert pd.isna(cci["gross_margin_rank"]), "NULL 毛利率不得填值参与排名"
        assert cci["score"] > 0, "其余有效因子权重归一化后仍有得分"


# ── 6. 连续 ROE NULL 不顶替 ──────────────────────────────────


class TestConsecutiveRoeNull:
    def test_null_year_eliminates(self):
        df = pd.DataFrame({"stock_code": ["PSKY"], "roe": [0.20]})
        roe_hist = pd.DataFrame({
            "stock_code": ["PSKY"] * 3,
            "report_date": [date(2025, 12, 31), date(2024, 12, 31), date(2023, 12, 31)],
            "roe": [0.20, None, 0.30],
        })
        filtered, _, n_after = filter_consecutive_roe(df, roe_hist, 3, 0.12)
        assert n_after == 0, "最近 N 年任一 ROE 缺失即淘汰，不得以更早年度顶替"


# ── 7. 行业中位数语义 ────────────────────────────────────────


class TestIndustryMedian:
    def _peers(self) -> pd.DataFrame:
        return pd.DataFrame({
            "stock_code": ["P1", "P2", "P3", "P4"],
            "roe": [0.10, 0.20, None, 0.30],
            "gross_margin": [0.30, 0.40, 0.50, None],
            "net_margin": [0.05, 0.10, 0.15, None],
            "debt_ratio": [0.40, 0.50, 0.60, None],
            # compute_pe 已对亏损返回 None；此处再验证 >0 过滤
            "pe_ttm": [10.0, 20.0, None, 30.0],
            "pb": [1.0, -2.0, 3.0, None],
            "fcf_yield": [0.10, -0.05, None, 0.20],
        })

    def test_median_excludes_invalid(self, monkeypatch):
        monkeypatch.setenv("US_SCREENER_SNAPSHOT_CURRENT", "1")
        with patch("quant.analyzer.query_us.load_us_snapshot_universe",
                   return_value=self._peers()) as loader:
            out = query_us.get_industry_stats("Ind", "US", "SELF")
        # 排除自身透传
        assert loader.call_args.kwargs["exclude_code"] == "SELF"
        assert loader.call_args.kwargs["industry"] == "Ind"
        row = out.iloc[0]
        assert row["peer_count"] == 4
        assert row["median_roe"] == pytest.approx(0.20)          # NULL 跳过
        assert row["median_pe"] == pytest.approx(20.0)           # 仅正值
        assert row["median_pb"] == pytest.approx(2.0), "非正 PB 不得入中位数"
        assert row["median_fcf_yield"] == pytest.approx(0.10), \
            "负 FCF Yield 保留，缺失跳过: median(-0.05, 0.10, 0.20) = 0.10"

    def test_empty_industry_returns_explicit_null(self, monkeypatch):
        monkeypatch.setenv("US_SCREENER_SNAPSHOT_CURRENT", "1")
        with patch("quant.analyzer.query_us.load_us_snapshot_universe",
                   return_value=pd.DataFrame()):
            out = query_us.get_industry_stats("NoSuchIndustry", "US", "")
        row = out.iloc[0]
        assert row["peer_count"] == 0
        for col in ("median_roe", "median_gross_margin", "median_net_margin",
                    "median_debt_ratio", "median_pe", "median_pb", "median_fcf_yield"):
            assert row[col] is None or pd.isna(row[col])

    def test_insufficient_positive_samples_null(self, monkeypatch):
        monkeypatch.setenv("US_SCREENER_SNAPSHOT_CURRENT", "1")
        peers = self._peers()
        peers["pe_ttm"] = [None, None, None, None]
        peers["pb"] = [None, None, None, None]
        with patch("quant.analyzer.query_us.load_us_snapshot_universe", return_value=peers):
            out = query_us.get_industry_stats("Ind", "US", "")
        row = out.iloc[0]
        assert row["median_pe"] is None or pd.isna(row["median_pe"])
        assert row["median_pb"] is None or pd.isna(row["median_pb"])
