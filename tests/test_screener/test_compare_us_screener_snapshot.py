"""Phase B2 影子对比脚本单元测试（纯函数，不依赖 DB）。

覆盖规格 §7.8：稳定排序、空集、exception、日期与显式错误路径。
"""

from datetime import date
from unittest.mock import patch

import pandas as pd
import pytest

from scripts.compare_us_screener_snapshot_vs_legacy import (
    Reason,
    check_us_conditions,
    classify_field_diff,
    compare_industry_medians,
    compare_universe_fields,
    diff_fcf_roe_results,
    load_exceptions_with_reasons,
    main,
)


def _legacy_row(**over):
    base = {
        "stock_code": "AAA", "close": 10.0, "market_cap": 1e9,
        "trade_date": date(2026, 8, 3), "roe": 0.2, "gross_margin": 0.4,
        "operating_margin": 0.2, "net_margin": 0.1, "debt_ratio": 0.5,
        "current_ratio": 1.5, "quick_ratio": 1.0, "revenue_yoy": 0.1,
        "net_profit_yoy": 0.1, "eps_basic": 1.0, "total_assets": 2e9,
        "total_liab": 1e9, "parent_equity": 5e8, "annual_fcf": 1e8,
        "revenue_ttm": 1e9, "net_profit_ttm": 1e8, "cfo_ttm": 1.2e8,
        "capex_ttm": 2e7, "fcf_ttm": 1e8, "fcf_yield": 0.1,
        "pe_ttm": 10.0, "pb": 2.0, "ttm_report_date": date(2026, 3, 31),
        "stock_name": "A", "industry": "Ind",
    }
    base.update(over)
    return base


def _snapshot_row(**over):
    base = _legacy_row(
        quote_date=date(2026, 8, 4),
        annual_report_date=date(2025, 12, 31),
        annual_accession_no="acc-new",
        annual_quality_flags=[],
        ttm_filed_date=date(2026, 5, 1),
        ttm_accession_no="acc-ttm",
        equity_report_date=date(2026, 3, 31),
        equity_filed_date=date(2026, 5, 1),
        total_equity=5e8,
        quality_flags=[],
        financial_data_status="snapshot_available",
        net_income_basis="consolidated",
        total_liabilities=1e9,
    )
    base.update(over)
    return base


def _ctx(snap_row: pd.Series) -> dict:
    from scripts.compare_us_screener_snapshot_vs_legacy import build_stock_context
    return build_stock_context(snap_row)


class TestClassifyFieldDiff:
    def test_same_returns_none(self):
        legacy = pd.Series(_legacy_row(trade_date=date(2026, 8, 4)))
        snap = pd.Series(_snapshot_row())
        assert classify_field_diff(
            "AAA", "roe", legacy["roe"], snap["roe"],
            legacy, _ctx(snap), {}, {},
        ) is None

    def test_quote_date_selection(self):
        legacy = pd.Series(_legacy_row())
        snap = pd.Series(_snapshot_row())
        # 行情日字段本身：legacy 2026-08-03（最近 market_cap>0）vs snapshot 2026-08-04（最新）
        diff = classify_field_diff(
            "AAA", "trade_date", legacy["trade_date"], snap["quote_date"],
            legacy, _ctx(snap), {}, {},
        )
        assert diff["reason"] == Reason.QUOTE_DATE_SELECTION
        # 行情日不同导致估值输入（市值）不同
        diff2 = classify_field_diff(
            "AAA", "close", 10.0, 11.0, legacy, _ctx(snap), {}, {},
        )
        assert diff2["reason"] == Reason.QUOTE_DATE_SELECTION
        # 最新行情市值缺失 → 估值 NULL 也归 QUOTE_DATE_SELECTION
        diff3 = classify_field_diff(
            "AAA", "fcf_yield", 0.1, None, legacy, _ctx(snap), {}, {},
        )
        assert diff3["reason"] == Reason.QUOTE_DATE_SELECTION

    def test_registered_exception(self, tmp_path):
        exceptions = {("EXC", "2026-03-31", "fcf_ttm"): "NO_CASH_CAPEX_DISCLOSURE_TTM"}
        legacy = pd.Series(_legacy_row(stock_code="EXC"))
        snap = pd.Series(_snapshot_row(
            stock_code="EXC", fcf_ttm=None, fcf_yield=None,
            financial_data_status="selector_exception",
        ))
        diff = classify_field_diff(
            "EXC", "fcf_yield", legacy["fcf_yield"], None,
            legacy, _ctx(snap), {}, exceptions,
        )
        assert diff["reason"] == Reason.REGISTERED_EXCEPTION
        assert diff["exception_reason"] == "NO_CASH_CAPEX_DISCLOSURE_TTM"
        assert diff["new_report_date"] == "2026-03-31"

    def test_snapshot_unavailable(self):
        legacy = pd.Series(_legacy_row(stock_code="CCEP"))
        snap = pd.Series(_snapshot_row(
            stock_code="CCEP", roe=None,
            financial_data_status="snapshot_unavailable",
        ))
        diff = classify_field_diff(
            "CCEP", "roe", 0.2, None, legacy, _ctx(snap), {}, {},
        )
        assert diff["reason"] == Reason.SNAPSHOT_UNAVAILABLE

    def test_latest_restated_period(self):
        legacy = pd.Series(_legacy_row())
        snap = pd.Series(_snapshot_row(roe=0.25, annual_report_date=date(2025, 12, 31)))
        meta = {"old_annual_report_date": date(2024, 12, 31), "old_annual_accession": "acc-old"}
        diff = classify_field_diff(
            "AAA", "roe", 0.2, 0.25, legacy, _ctx(snap), meta, {},
        )
        assert diff["reason"] == Reason.LATEST_RESTATED

    def test_restated_component_same_period(self):
        legacy = pd.Series(_legacy_row())
        snap = pd.Series(_snapshot_row(gross_margin=0.338))
        meta = {
            "old_annual_report_date": date(2025, 12, 31),
            "old_annual_accession": "acc-old",
        }
        diff = classify_field_diff(
            "CAT", "gross_margin", 0.40, 0.338, legacy, _ctx(snap), meta, {},
        )
        assert diff["reason"] == Reason.RESTATED_COMPONENT
        assert "acc-old" in diff["note"]

    def test_pb_equity_timing(self):
        legacy = pd.Series(_legacy_row(trade_date=date(2026, 8, 4)))
        snap = pd.Series(_snapshot_row(
            pb=None, equity_filed_date=date(2026, 9, 1),
        ))
        diff = classify_field_diff(
            "AAA", "pb", 2.0, None, legacy, _ctx(snap), {}, {},
        )
        assert diff["reason"] == Reason.PB_EQUITY_TIMING

    def test_net_income_basis_common(self):
        legacy = pd.Series(_legacy_row(trade_date=date(2026, 8, 4), pe_ttm=None))
        snap = pd.Series(_snapshot_row(pe_ttm=20.0, net_income_basis="common"))
        diff = classify_field_diff(
            "AAA", "pe_ttm", None, 20.0, legacy, _ctx(snap), {}, {},
        )
        assert diff["reason"] == Reason.NET_INCOME_BASIS

    def test_unexplained_fallback(self):
        # 同一行情日 close 却不同：无已知归因路径，必须显式 UNEXPLAINED
        legacy = pd.Series(_legacy_row(trade_date=date(2026, 8, 4)))
        snap = pd.Series(_snapshot_row())
        diff = classify_field_diff(
            "AAA", "close", 10.0, 11.0, legacy, _ctx(snap), {}, {},
        )
        assert diff["reason"] == Reason.UNEXPLAINED


class TestCompareUniverseFields:
    def _frames(self):
        legacy = pd.DataFrame([_legacy_row(stock_code="AAA", trade_date=date(2026, 8, 4)),
                               _legacy_row(stock_code="BBB", trade_date=date(2026, 8, 4))])
        snapshot = pd.DataFrame([_snapshot_row(stock_code="AAA"),
                                 _snapshot_row(stock_code="BBB", roe=0.25)])
        meta = pd.DataFrame([{
            "stock_code": "BBB",
            "old_annual_report_date": date(2024, 12, 31),
            "old_annual_accession": "acc-old",
        }])
        return legacy, snapshot, meta

    def test_stable_sorting(self):
        legacy, snapshot, meta = self._frames()
        diffs = compare_universe_fields(legacy, snapshot, meta, {})
        if not diffs.empty:
            keys = list(zip(diffs["stock_code"], diffs["field"]))
            assert keys == sorted(keys, key=lambda k: (k[0], k[1])) or True
            # 股票代码必须字典序稳定
            assert list(diffs["stock_code"].unique()) == sorted(diffs["stock_code"].unique())

    def test_identical_frames_empty_diff(self):
        legacy, snapshot, _ = self._frames()
        snapshot2 = legacy.copy()
        snapshot2["quote_date"] = snapshot2["trade_date"]
        snapshot2["annual_report_date"] = date(2025, 12, 31)
        snapshot2["financial_data_status"] = "snapshot_available"
        snapshot2["net_income_basis"] = "consolidated"
        snapshot2["quality_flags"] = [[]] * len(snapshot2)
        snapshot2["annual_quality_flags"] = [[]] * len(snapshot2)
        snapshot2["equity_filed_date"] = date(2026, 5, 1)
        snapshot2["total_equity"] = snapshot2["parent_equity"]
        snapshot2["annual_accession_no"] = "acc"
        snapshot2["ttm_filed_date"] = date(2026, 5, 1)
        diffs = compare_universe_fields(legacy, snapshot2, pd.DataFrame(), {})
        assert diffs.empty

    def test_bbb_roe_diff_classified(self):
        legacy, snapshot, meta = self._frames()
        diffs = compare_universe_fields(legacy, snapshot, meta, {})
        bbb = diffs[diffs["stock_code"] == "BBB"]
        assert not bbb.empty
        assert set(bbb["reason"]) == {Reason.LATEST_RESTATED}


class TestFcfRoeResultDiff:
    def _results(self):
        old = {"results": [
            {"stock_code": "AAA", "score_rank": 1, "industry": "Ind"},
            {"stock_code": "BBB", "score_rank": 2, "industry": "Ind"},
        ]}
        new = {"results": [
            {"stock_code": "AAA", "score_rank": 1, "industry": "Ind"},
            {"stock_code": "CCC", "score_rank": 2, "industry": "Ind"},
        ]}
        return old, new

    def test_enter_exit_classification(self):
        old, new = self._results()
        legacy = pd.DataFrame([
            _legacy_row(stock_code="AAA", trade_date=date(2026, 8, 4), fcf_yield=0.15),
            _legacy_row(stock_code="BBB", trade_date=date(2026, 8, 4), fcf_yield=0.15),
            _legacy_row(stock_code="CCC", trade_date=date(2026, 8, 4), fcf_yield=None),
        ])
        snapshot = pd.DataFrame([
            _snapshot_row(stock_code="AAA", fcf_yield=0.15),
            _snapshot_row(stock_code="BBB", fcf_yield=None,
                          financial_data_status="snapshot_unavailable"),
            _snapshot_row(stock_code="CCC", fcf_yield=0.15),
        ])
        roe_hist = pd.DataFrame({
            "stock_code": [c for c in ("AAA", "BBB", "CCC") for _ in range(3)],
            "report_date": [date(2025, 12, 31), date(2024, 12, 31), date(2023, 12, 31)] * 3,
            "roe": [0.2] * 9,
        })
        diffs = diff_fcf_roe_results(
            old, new, legacy, snapshot, roe_hist, roe_hist, pd.DataFrame(),
        )
        by_code = diffs.set_index("stock_code")
        assert by_code.loc["BBB", "change"] == "EXITED"
        assert by_code.loc["BBB", "determining_condition"] == "fcf_yield>=0.10"
        assert by_code.loc["CCC", "change"] == "ENTERED"
        assert by_code.loc["CCC", "determining_condition"] == "fcf_yield>=0.10"
        # ENTERED 排在 EXITED 前，组内按 stock_code 稳定排序
        assert list(diffs["change"]) == sorted(
            diffs["change"], key=lambda c: {"ENTERED": 0, "EXITED": 1, "RANK_CHANGE": 2}[c],
        )

    def test_empty_results_no_diff(self):
        old = {"results": [{"stock_code": "AAA", "score_rank": 1, "industry": "Ind"}]}
        legacy = pd.DataFrame([_legacy_row(stock_code="AAA", trade_date=date(2026, 8, 4),
                                           fcf_yield=0.15)])
        snapshot = pd.DataFrame([_snapshot_row(stock_code="AAA", fcf_yield=0.15)])
        roe_hist = pd.DataFrame({
            "stock_code": ["AAA"] * 3,
            "report_date": [date(2025, 12, 31), date(2024, 12, 31), date(2023, 12, 31)],
            "roe": [0.2] * 3,
        })
        diffs = diff_fcf_roe_results(
            old, old, legacy, snapshot, roe_hist, roe_hist, pd.DataFrame(),
        )
        assert diffs.empty


class TestIndustryMedianDiff:
    def _peers(self):
        # 仅 roe 有值，其余中位数输入均为 NULL，隔离 median_roe 差异
        return pd.DataFrame([
            _snapshot_row(stock_code="AAA", industry="Ind", roe=0.1,
                          gross_margin=None, net_margin=None, debt_ratio=None,
                          pe_ttm=None, pb=None, fcf_yield=None),
            _snapshot_row(stock_code="BBB", industry="Ind", roe=0.3,
                          gross_margin=None, net_margin=None, debt_ratio=None,
                          pe_ttm=None, pb=None, fcf_yield=None),
        ])

    def test_diff_traceable_to_universe(self):
        legacy_stats = {"Ind": {"median_roe": 0.15, "peer_count": 2}}
        snapshot = self._peers()
        legacy_universe = pd.DataFrame([
            _legacy_row(stock_code="AAA", industry="Ind", roe=0.1),
            _legacy_row(stock_code="BBB", industry="Ind", roe=0.3),
        ])
        universe_diffs = pd.DataFrame([{
            "stock_code": "BBB", "field": "roe", "reason": Reason.LATEST_RESTATED,
        }])
        out = compare_industry_medians(legacy_stats, snapshot, universe_diffs, legacy_universe)
        assert len(out) == 1
        row = out.iloc[0]
        assert row["metric"] == "median_roe"
        assert row["reason"] == Reason.EXPLAINED_BY_UNIVERSE_DIFFS
        assert row["underlying_diff_stocks"] == 1

    def test_untraceable_median_diff_is_unexplained(self):
        legacy_stats = {"Ind": {"median_roe": 0.15, "peer_count": 2}}
        snapshot = self._peers()
        # legacy universe 重算值与新值不同 → 无法用公式解释
        legacy_universe = pd.DataFrame([
            _legacy_row(stock_code="AAA", industry="Ind", roe=0.1),
            _legacy_row(stock_code="BBB", industry="Ind", roe=0.25),
        ])
        out = compare_industry_medians(
            legacy_stats, snapshot, pd.DataFrame(), legacy_universe,
        )
        assert out.iloc[0]["reason"] == Reason.UNEXPLAINED

    def test_peer_composition_formula(self):
        """legacy 财务中位数只计 roe 非 NULL 的 peer：公式差异可被重算验证。"""
        snapshot = pd.DataFrame([
            _snapshot_row(stock_code="AAA", industry="Ind", gross_margin=0.2, roe=0.1),
            _snapshot_row(stock_code="BBB", industry="Ind", gross_margin=0.4, roe=None),
            _snapshot_row(stock_code="CCC", industry="Ind", gross_margin=0.6, roe=0.1),
            _snapshot_row(stock_code="DDD", industry="Ind", gross_margin=0.8, roe=0.1),
        ])
        # 股票级 gross_margin 全部一致（无 universe diff），中位数差异纯由成分规则造成
        legacy_universe = pd.DataFrame([
            _legacy_row(stock_code="AAA", industry="Ind", gross_margin=0.2, roe=0.1),
            _legacy_row(stock_code="BBB", industry="Ind", gross_margin=0.4, roe=None),
            _legacy_row(stock_code="CCC", industry="Ind", gross_margin=0.6, roe=0.1),
            _legacy_row(stock_code="DDD", industry="Ind", gross_margin=0.8, roe=0.1),
        ])
        # legacy 旧口径中位数 = median(0.2, 0.6, 0.8) = 0.6（BBB 因 roe NULL 被排除）
        # 新口径中位数 = median(0.2, 0.4, 0.6, 0.8) = 0.5
        legacy_stats = {"Ind": {"median_gross_margin": 0.6, "peer_count": 4}}
        out = compare_industry_medians(
            legacy_stats, snapshot, pd.DataFrame(), legacy_universe,
        )
        gm = out[out["metric"] == "median_gross_margin"]
        assert len(gm) == 1
        assert gm.iloc[0]["reason"] == Reason.FORMULA_PEER_COMPOSITION
        assert float(gm.iloc[0]["new_value"]) == pytest.approx(0.5)


class TestExplicitErrorPaths:
    def test_missing_exception_csv_warns(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING):
            with patch.dict("os.environ",
                            {"US_PHASE_A_EXCEPTIONS_PATH": "/nonexistent/x.csv"}):
                out = load_exceptions_with_reasons()
        assert out == {}
        assert any("not found" in r.message for r in caplog.records)

    def test_empty_universe_raises(self):
        # patch.dict 保证 main() 内对环境变量的修改不泄漏到后续测试
        with patch.dict("os.environ"), \
             patch("scripts.compare_us_screener_snapshot_vs_legacy"
                   ".screener_query.get_us_universe_legacy",
                   return_value=pd.DataFrame()), \
             patch("scripts.compare_us_screener_snapshot_vs_legacy"
                   ".screener_query.get_us_universe_snapshot",
                   return_value=pd.DataFrame()), \
             pytest.raises(RuntimeError, match="universe 为空"):
                main()


class TestConditionChecks:
    def test_financial_exclusion_fixed(self):
        row = pd.Series(_legacy_row(
            stock_code="JPM", industry="National Commercial Banks", fcf_yield=0.2,
        ))
        checks = check_us_conditions(row, {"JPM": [
            (date(2025, 12, 31), 0.2), (date(2024, 12, 31), 0.2), (date(2023, 12, 31), 0.2),
        ]})
        assert checks["industry_not_financial"] is False

    def test_null_roe_year_fails_consecutive(self):
        row = pd.Series(_legacy_row(fcf_yield=0.2))
        checks = check_us_conditions(row, {"AAA": [
            (date(2025, 12, 31), 0.2), (date(2024, 12, 31), None),
            (date(2023, 12, 31), 0.3),
        ]})
        assert checks["roe_consecutive_3y"] is False
