"""tests/test_phase_c_cutover.py

Phase C1(US 同步切换至版本层)单元测试。
规格:docs/core/US_PHASE_C_SYNC_CUTOVER_TASK.md §4。
"""
from __future__ import annotations

import json
import re
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import core.scheduler as sched
from core.sync import us_market


# ── 1/2. version-only ingest 语义 ─────────────────────────────

class TestVersionOnlyIngest:
    def test_success_returns_version_layer_tables(self):
        fetcher = MagicMock()
        fetcher.ingest_version_layer.return_value = {
            "income": {"facts_inserted": 10, "facts_repeated": 2, "facts_conflicted": 0,
                       "facts_staged": 0, "run_id": 1},
            "balance": {"facts_inserted": 5, "facts_repeated": 0, "facts_conflicted": 0,
                        "facts_staged": 0, "run_id": 2},
            "cashflow": {"facts_inserted": 3, "facts_repeated": 0, "facts_conflicted": 0,
                         "facts_staged": 0, "run_id": 3},
        }
        ctx = SimpleNamespace(stock_code="X", cik="1", snapshot_id=1, content_hash="h")
        tables = us_market._process_us_company_data(fetcher, {"facts": {}}, ctx)
        assert tables == us_market.VERSION_LAYER_TABLES
        assert "us_income_statement" not in tables

    def test_zero_write_returns_empty(self):
        fetcher = MagicMock()
        fetcher.ingest_version_layer.return_value = {
            s: {"facts_inserted": 0, "facts_repeated": 0, "facts_conflicted": 0,
                "facts_staged": 0, "run_id": i}
            for i, s in enumerate(("income", "balance", "cashflow"))
        }
        ctx = SimpleNamespace(stock_code="X", cik="1", snapshot_id=1, content_hash="h")
        assert us_market._process_us_company_data(fetcher, {"facts": {}}, ctx) == []

    def test_version_layer_failure_raises_not_caught(self):
        """版本层写入失败必须抛出(记 ticker 失败),不得像旧双写一样吞掉。"""
        fetcher = MagicMock()
        fetcher.ingest_version_layer.side_effect = RuntimeError("X income (snapshot 9): boom")
        ctx = SimpleNamespace(stock_code="X", cik="1", snapshot_id=9, content_hash="h")
        with pytest.raises(RuntimeError, match="boom"):
            us_market._process_us_company_data(fetcher, {"facts": {}}, ctx)

    def test_write_version_layer_empty_returns_zero_dict(self):
        """零事实(如 IFRS-only 发行人)不得返回 None/崩溃,而是零计数字典。"""
        from core.fetchers.us_financial import USFinancialFetcher
        fetcher = USFinancialFetcher()
        ctx = SimpleNamespace(stock_code="GFS", cik="1", snapshot_id=1, content_hash="h")
        out = fetcher._write_version_layer([], [], "income", ctx)
        assert out["facts_inserted"] == 0
        assert out["facts_repeated"] == 0
        assert out["run_id"] is None

    def test_reparse_without_snapshot_refuses_old_table_fallback(self):
        """reparse 无 raw_snapshot_version 链时拒绝执行,不退化为旧表写入。"""
        fetcher = MagicMock()
        with pytest.raises(ValueError, match="FetchContext"):
            us_market._process_us_company_data(fetcher, {"facts": {}}, None)

    def test_us_market_module_has_no_old_table_upsert(self):
        """BXP 型护栏的静态面:在线模块不再含旧宽表 upsert 路径。"""
        src = Path("core/sync/us_market.py").read_text()
        assert "upsert(" not in src
        for table in ("us_income_statement", "us_balance_sheet", "us_cash_flow_statement"):
            assert f'"{table}"' not in src


# ── 3. expected_skip / blocking 分类(kind 匹配)───────────────

class TestClassification:
    def test_registered_skip_allows_matching_kind(self):
        sync_result = {
            "no_write": [], "index_errors": [],
            "failures": [{"ticker": "OZK", "kind": "fetch_404",
                          "error": "HTTPError: 404 ..."}],
        }
        skips = {"OZK": {"stock_code": "OZK", "reason_code": "COMPANYFACTS_PERMANENT_404"}}
        out = sched._classify_us_sync_outcome(sync_result, skips)
        assert out["expected_skip"] == ["OZK"]
        assert out["blocking_failure"] == []

    def test_kind_mismatch_blocks_even_if_registered(self):
        """验收阻断项 1:OZK 报 version writer 失败不得被 404 台账放行。"""
        sync_result = {
            "no_write": [], "index_errors": [],
            "failures": [{"ticker": "OZK", "kind": "ingest",
                          "error": "RuntimeError: version writer failed"}],
        }
        skips = {"OZK": {"stock_code": "OZK", "reason_code": "COMPANYFACTS_PERMANENT_404"}}
        out = sched._classify_us_sync_outcome(sync_result, skips)
        assert out["expected_skip"] == []
        assert out["blocking_failure"] == ["OZK"]

    def test_ifrs_skip_matches_zero_facts_only(self):
        skips = {"GFS": {"stock_code": "GFS", "reason_code": "FOREIGN_IFRS_NO_USGAAP_FACTS"}}
        ok = sched._classify_us_sync_outcome(
            {"no_write": ["GFS"], "index_errors": [], "failures": []}, skips)
        assert ok["expected_skip"] == ["GFS"]
        bad = sched._classify_us_sync_outcome(
            {"no_write": [], "index_errors": [],
             "failures": [{"ticker": "GFS", "kind": "fetch_other", "error": "timeout"}]}, skips)
        assert bad["blocking_failure"] == ["GFS"]

    def test_unregistered_failure_blocks(self):
        sync_result = {
            "no_write": ["FOO"], "index_errors": [],
            "failures": [{"ticker": "BAR", "kind": "cik_mapping", "error": "无法解析 CIK"}],
        }
        out = sched._classify_us_sync_outcome(sync_result, {})
        assert out["expected_skip"] == []
        assert out["blocking_failure"] == ["BAR", "FOO"]

    def test_index_error_always_blocking(self):
        """验收阻断项 2:指数级失败(如公司列表不可用)必须阻断。"""
        sync_result = {"no_write": [], "failures": [],
                       "index_errors": ["RUSSELL1000: company list unavailable"]}
        out = sched._classify_us_sync_outcome(sync_result, {})
        assert out["index_errors"] == ["RUSSELL1000: company list unavailable"]

    def test_expired_skip_blocks(self, tmp_path, monkeypatch):
        csv_path = tmp_path / "skips.csv"
        csv_path.write_text(
            "stock_code,reason_code,evidence_ref,first_confirmed,review_by\n"
            "OZK,COMPANYFACTS_PERMANENT_404,proof,2026-01-01,2026-06-01\n"
        )
        monkeypatch.setattr(sched, "_PHASE_C_SKIPS_CSV", csv_path)
        skips = sched._load_expected_skips(today=date(2026, 8, 11))
        assert skips == {}  # 已过期
        out = sched._classify_us_sync_outcome(
            {"no_write": [], "index_errors": [],
             "failures": [{"ticker": "OZK", "kind": "fetch_404", "error": "404"}]}, skips)
        assert out["blocking_failure"] == ["OZK"]


# ── 4/5. scheduler 原子编排 ───────────────────────────────────

def _orch_mocks(monkeypatch, tmp_path, sync_result):
    calls: list[str] = []
    sync_result.setdefault("failures", [])
    sync_result.setdefault("index_errors", [])
    monkeypatch.setattr(sched, "_sync_us", lambda: sync_result)
    monkeypatch.setattr(sched, "_load_expected_skips", lambda today=None: {})
    monkeypatch.setattr(sched, "_load_index_only_registry", lambda: {"Z9"})
    monkeypatch.setattr(sched, "_reconcile_us_universe", lambda scope, idx, es: {
        "universe_count": 1003, "index_ticker_count": 1000, "scope_ticker_count": 1033,
        "out_of_sync_scope": ["Z1"], "index_only_tickers": ["Z9"],
        "universe_not_in_sync_scope": [], "expected_skip_in_universe": ["Z1"],
    })
    monkeypatch.setattr(sched, "_check_zero_write_baseline", lambda: [])
    monkeypatch.setattr(sched, "_PHASE_C_SUMMARY_DIR", tmp_path)

    import scripts.project_us_financial_snapshots as proj_mod
    import scripts.compare_us_snapshot_vs_old as cmp_mod

    monkeypatch.setattr(
        proj_mod, "run_projection",
        lambda **kw: calls.append("projection") or {"projection_run_id": "r1"},
    )
    monkeypatch.setattr(
        cmp_mod, "run_comparison",
        lambda **kw: calls.append("compare") or SimpleNamespace(
            stats_by_reason=lambda: {cmp_mod.Reason.UNEXPLAINED: 0}),
    )
    monkeypatch.setattr(cmp_mod, "load_registered_exceptions", lambda p: {})
    import core.validate as validate_mod
    monkeypatch.setattr(
        validate_mod, "run_after_sync",
        lambda market="": calls.append("validate") or {"success": True, "errors": 0},
    )
    return calls


class TestOrchestration:
    def test_success_order_projection_once_then_compare_then_validate(self, monkeypatch, tmp_path):
        calls = _orch_mocks(monkeypatch, tmp_path, {
            "success": 21, "failed": 0, "skipped": 982, "no_write": [],
            "errors": [], "index_tickers": {"A", "B"},
        })
        result = sched._run_us_financial_orchestration(0.0)
        assert calls == ["projection", "compare", "validate"]
        assert result["projection"] == {"projection_run_id": "r1"}

    def test_blocking_failure_stops_before_projection(self, monkeypatch, tmp_path):
        calls = _orch_mocks(monkeypatch, tmp_path, {
            "success": 20, "failed": 1, "skipped": 982, "no_write": [],
            "errors": ["FOO: boom"],
            "failures": [{"ticker": "FOO", "kind": "ingest", "error": "boom"}],
            "index_tickers": {"A"},
        })
        with pytest.raises(RuntimeError, match="blocking"):
            sched._run_us_financial_orchestration(0.0)
        assert calls == []  # projection/compare/validate 均未运行

    def test_index_error_stops_before_projection(self, monkeypatch, tmp_path):
        """验收阻断项 2:指数级失败不得继续发布。"""
        calls = _orch_mocks(monkeypatch, tmp_path, {
            "success": 0, "failed": 0, "skipped": 0, "no_write": [], "errors": [],
            "index_errors": ["RUSSELL1000: company list unavailable"],
            "index_tickers": set(),
        })
        with pytest.raises(RuntimeError, match="指数级失败"):
            sched._run_us_financial_orchestration(0.0)
        assert calls == []

    def test_unregistered_index_only_blocks(self, monkeypatch, tmp_path):
        """验收阻断项 4:未登记的 index-only ticker 阻断发布。"""
        calls = _orch_mocks(monkeypatch, tmp_path, {
            "success": 20, "failed": 0, "skipped": 982, "no_write": [],
            "errors": [], "index_tickers": {"A"},
        })
        monkeypatch.setattr(sched, "_reconcile_us_universe", lambda scope, idx, es: {
            "universe_count": 1003, "index_ticker_count": 1001, "scope_ticker_count": 1001,
            "out_of_sync_scope": [], "index_only_tickers": ["Z9", "NEW1"],
            "universe_not_in_sync_scope": [], "expected_skip_in_universe": [],
        })
        with pytest.raises(RuntimeError, match="index-only"):
            sched._run_us_financial_orchestration(0.0)
        assert calls == []

    def test_validate_failure_marks_job_failed(self, monkeypatch, tmp_path):
        """验收阻断项 3:validate 失败不得报成功。"""
        calls = _orch_mocks(monkeypatch, tmp_path, {
            "success": 20, "failed": 0, "skipped": 982, "no_write": [],
            "errors": [], "index_tickers": {"A"},
        })
        import core.validate as validate_mod
        monkeypatch.setattr(
            validate_mod, "run_after_sync",
            lambda market="": calls.append("validate") or {"success": False, "error": "boom"},
        )
        with pytest.raises(RuntimeError, match="validate"):
            sched._run_us_financial_orchestration(0.0)
        assert calls == ["projection", "compare", "validate"]

    def test_zero_write_violation_stops(self, monkeypatch, tmp_path):
        calls = _orch_mocks(monkeypatch, tmp_path, {
            "success": 21, "failed": 0, "skipped": 982, "no_write": [],
            "errors": [], "index_tickers": {"A"},
        })
        monkeypatch.setattr(sched, "_check_zero_write_baseline",
                            lambda: [{"object": "us_income_statement", "row_delta": 5}])
        with pytest.raises(RuntimeError, match="禁止的写入"):
            sched._run_us_financial_orchestration(0.0)
        assert calls == []

    def test_all_skipped_no_projection(self, monkeypatch, tmp_path):
        calls = _orch_mocks(monkeypatch, tmp_path, {
            "success": 0, "failed": 0, "skipped": 1000, "no_write": [],
            "errors": [], "index_tickers": {"A"},
        })
        result = sched._run_us_financial_orchestration(0.0)
        assert "projection" not in calls and "validate" not in calls
        assert result["projection"] is None  # no_new_filings


# ── 6. 停刷 US 物化视图 ───────────────────────────────────────

class TestNoUsMvRefresh:
    def test_us_financial_and_daily_quote_refresh_nothing(self, monkeypatch):
        executed: list[str] = []
        monkeypatch.setattr(sched, "execute", lambda sql, **kw: executed.append(sql))
        sched._refresh_materialized_views("financial", "US")
        sched._refresh_materialized_views("daily_quote_us", "US")
        assert executed == []

    def test_cn_refresh_unchanged(self, monkeypatch):
        executed: list[str] = []
        monkeypatch.setattr(sched, "execute", lambda sql, **kw: executed.append(sql))
        sched._refresh_materialized_views("financial", "CN_A")
        assert any("mv_financial_indicator" in s for s in executed)

    def test_utils_refresh_map_us_empty(self):
        from core.sync._utils import _REFRESH_MAP
        assert _REFRESH_MAP["US"]["financial"] == []
        assert _REFRESH_MAP["US"]["daily"] == []


# ── 7. incremental 版本层语义 ─────────────────────────────────

class TestIncrementalVersionSemantics:
    def test_tables_complete_us_version_layer(self):
        from core.incremental import _tables_complete
        assert _tables_complete("US", ["us_filing", "us_financial_fact_version"])
        assert not _tables_complete(
            "US", ["us_income_statement", "us_balance_sheet", "us_cash_flow_statement"])

    def test_update_last_report_date_us_uses_us_filing(self, monkeypatch):
        import core.incremental as inc
        seen: list[str] = []
        monkeypatch.setattr(inc, "execute", lambda sql, params=None, **kw: (
            seen.append(sql) or [(date(2026, 6, 30),)] if "MAX(report_date)" in sql else None))
        out = inc.update_last_report_date("X", ["us_filing", "us_financial_fact_version"])
        assert out == date(2026, 6, 30)
        assert "us_filing" in seen[0]
        assert "us_income_statement" not in seen[0]


# ── 8. 范围对账 ───────────────────────────────────────────────

class TestUniverseReconciliation:
    def test_out_of_sync_scope_includes_universe_skip(self, monkeypatch):
        monkeypatch.setattr(sched, "execute", lambda sql, **kw: [("A",), ("B",), ("C",)])
        out = sched._reconcile_us_universe({"A"}, {"A"}, ["B"])
        assert sorted(out["out_of_sync_scope"]) == ["B", "C"]
        assert out["universe_count"] == 3
        assert out["index_only_tickers"] == []
        assert out["universe_not_in_sync_scope"] == ["B", "C"]
        assert out["expected_skip_in_universe"] == ["B"]


# ── 9. 零写入护栏(全行 hash + 时间戳)─────────────────────────

def _baseline_payload(md5="abc", ts="2026-08-11T06:00:00"):
    obj = {"row_count": 100, "content_md5": md5,
           "max_updated_column": "updated_at", "max_updated_at": ts}
    return {"objects": {name: dict(obj) for name in (
        "us_income_statement", "us_balance_sheet", "us_cash_flow_statement",
        "mv_us_financial_indicator", "mv_us_indicator_ttm", "mv_us_fcf_yield")}}


class TestZeroWriteBaseline:
    def _setup(self, monkeypatch, tmp_path, stats_map):
        baseline = tmp_path / "baseline.json"
        baseline.write_text(json.dumps(_baseline_payload()))
        import scripts.phase_c_baseline as baseline_mod
        monkeypatch.setattr(baseline_mod, "OUT", baseline)
        monkeypatch.setattr(baseline_mod, "_object_stats", lambda obj: stats_map[obj])

    def _stats(self, rows=100, md5="abc", ts="2026-08-11T06:00:00"):
        return {"row_count": rows, "content_md5": md5,
                "max_updated_column": "updated_at", "max_updated_at": ts}

    def test_clean_passes(self, monkeypatch, tmp_path):
        self._setup(monkeypatch, tmp_path,
                    {o: self._stats() for o in _baseline_payload()["objects"]})
        import scripts.phase_c_baseline as baseline_mod
        assert baseline_mod.find_violations() == []

    def test_row_change_detected(self, monkeypatch, tmp_path):
        stats = {o: self._stats() for o in _baseline_payload()["objects"]}
        stats["us_income_statement"] = self._stats(rows=101)
        self._setup(monkeypatch, tmp_path, stats)
        import scripts.phase_c_baseline as baseline_mod
        violations = baseline_mod.find_violations()
        assert len(violations) == 1
        assert violations[0]["object"] == "us_income_statement"
        assert violations[0]["row_delta"] == 1

    def test_timestamp_change_detected(self, monkeypatch, tmp_path):
        """验收阻断项 5:仅时间戳变化(内容 hash 未变)也必须报。"""
        stats = {o: self._stats() for o in _baseline_payload()["objects"]}
        stats["mv_us_fcf_yield"] = self._stats(ts="2026-08-12T06:00:00")
        self._setup(monkeypatch, tmp_path, stats)
        import scripts.phase_c_baseline as baseline_mod
        violations = baseline_mod.find_violations()
        assert [v["object"] for v in violations] == ["mv_us_fcf_yield"]

    def test_missing_baseline_is_violation(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sched, "_PHASE_C_BASELINE", tmp_path / "nope.json")
        violations = sched._check_zero_write_baseline()
        assert violations and violations[0]["object"] == "baseline"


# ── 10. compare 跨期与双日期列 ────────────────────────────────

class TestCompareCrossPeriod:
    def test_same_value_different_period_is_unexplained(self):
        import pandas as pd
        import scripts.compare_us_snapshot_vs_old as cmp

        old_df = pd.DataFrame([{
            "stock_code": "BXP", "old_report_date": date(2026, 6, 30),
            "old_revenue": Decimal("100"), "old_net_profit": None,
            "old_accession": "a1", "old_filed": date(2026, 8, 6),
            "old_total_equity": None, "old_total_assets": None,
            "old_total_liabilities": None, "old_operating_cash_flow": None,
            "old_capex": None, "old_fcf": None, "old_roe": None, "old_roa": None,
            "old_gross_profit": None, "old_operating_income": None,
        }])
        new_df = pd.DataFrame([{
            "stock_code": "BXP", "new_report_date": date(2025, 12, 31),
            "new_revenue": Decimal("100"), "new_net_profit": None,
            "new_accession": "a2", "new_filed": date(2026, 3, 1), "new_form": "10-K",
            "new_total_equity": None, "new_total_assets": None,
            "new_total_liabilities": None, "new_operating_cash_flow": None,
            "new_capex": None, "new_fcf": None, "new_roe": None, "new_roa": None,
            "new_gross_margin": None, "new_operating_margin": None,
            "new_net_margin": None, "new_debt_ratio": None, "quality_flags": None,
        }])
        rows = cmp._compare_annual(old_df, new_df)
        rev = [r for r in rows if r.field == "revenue"][0]
        assert rev.reason == cmp.Reason.UNEXPLAINED
        assert rev.old_report_date == date(2026, 6, 30)
        assert rev.new_report_date == date(2025, 12, 31)

    def test_both_null_different_period_stays_same(self):
        """跨期但双侧均为 NULL 不算"同值",保持 SAME(DXC 型,不得误报 UNEXPLAINED)。"""
        import pandas as pd
        import scripts.compare_us_snapshot_vs_old as cmp

        old_df = pd.DataFrame([{
            "stock_code": "DXC", "old_report_date": date(2025, 3, 31),
            "old_revenue": None, "old_net_profit": None,
            "old_accession": "a1", "old_filed": date(2025, 5, 15),
            "old_total_equity": None, "old_total_assets": None,
            "old_total_liabilities": None, "old_operating_cash_flow": None,
            "old_capex": None, "old_fcf": None, "old_roe": None, "old_roa": None,
            "old_gross_profit": None, "old_operating_income": None,
        }])
        new_df = pd.DataFrame([{
            "stock_code": "DXC", "new_report_date": date(2026, 3, 31),
            "new_revenue": None, "new_net_profit": None,
            "new_accession": "a2", "new_filed": date(2026, 5, 8), "new_form": "10-K",
            "new_total_equity": None, "new_total_assets": None,
            "new_total_liabilities": None, "new_operating_cash_flow": None,
            "new_capex": None, "new_fcf": None, "new_roe": None, "new_roa": None,
            "new_gross_margin": None, "new_operating_margin": None,
            "new_net_margin": None, "new_debt_ratio": None, "quality_flags": None,
        }])
        rows = cmp._compare_annual(old_df, new_df)
        for r in rows:
            assert r.reason == cmp.Reason.SAME, f"{r.field}: {r.reason}"

    def test_csv_has_dual_report_date_columns(self, tmp_path):
        import scripts.compare_us_snapshot_vs_old as cmp

        result = cmp.ComparisonResult(rows=[cmp.ComparisonRow(
            stock_code="X", report_date=date(2025, 12, 31), field="revenue",
            old_value=Decimal("1"), new_value=Decimal("1"),
            abs_diff=None, rel_diff_pct=None, reason=cmp.Reason.SAME,
            old_report_date=date(2026, 6, 30), new_report_date=date(2025, 12, 31),
        )])
        out = tmp_path / "diffs.csv"
        result.to_csv(out)
        header = out.read_text().splitlines()[0]
        assert "old_report_date" in header and "new_report_date" in header


# ── 11. 静态禁扫:六对象生产引用收敛 ──────────────────────────

class TestStaticScan:
    RETIRING = (
        "us_income_statement", "us_balance_sheet", "us_cash_flow_statement",
        "mv_us_financial_indicator", "mv_us_indicator_ttm", "mv_us_fcf_yield",
    )

    # 允许引用六对象的文件(受控 legacy fallback 分支/审计校验模块/同步配置),
    # 每个都必须能回答"为什么还在";新文件出现引用即失败。
    ALLOWLIST = {
        "core/sync/_utils.py",          # MARKET_CONFIG(US special 拒绝)+ CN 视图配置
        "core/sync/us_market.py",       # 仅模块 docstring 说明退役范围
        "core/scheduler.py",            # 仅注释说明停刷
        "core/validate.py",             # 校验模块(版本层改造在 B3b,残留读取待清理)
        "core/us_financial_chain_audit.py",  # 审计模块
        "core/us_financial_verify.py",       # 审计模块
        "quant/analyzer/query_us.py",   # B1 受控 legacy fallback 分支
        "quant/screener/query.py",      # B2 受控 legacy fallback 分支
        "quant/backtest/preloader.py",  # B4 受控 legacy fallback 分支
        "quant/backtest/universe.py",   # B4 受控 legacy fallback 分支
        "quant/checks/fcf_roe_check.py",     # B3b 受控 legacy fallback 分支
        "quant/metrics/__init__.py",    # 受控 legacy fallback 分支
        "web/services/dashboard_service.py",  # B3a 受控 legacy fallback 分支
    }

    def test_no_new_production_references(self):
        offenders: list[str] = []
        pattern = re.compile("|".join(self.RETIRING))
        for root in ("core", "quant", "web"):
            for path in Path(root).rglob("*.py"):
                rel = str(path)
                if "__pycache__" in rel:
                    continue
                if pattern.search(path.read_text(errors="ignore")):
                    if rel not in self.ALLOWLIST:
                        offenders.append(rel)
        assert offenders == [], f"六对象出现未登记引用: {offenders}"


# ── Phase C2: universe 补充清单 ─────────────────────────────

class TestSupplementLoader:
    def _csv(self, tmp_path, rows: list[str]):
        p = tmp_path / "supp.csv"
        p.write_text(
            "stock_code,evidence_ref,first_confirmed,review_by\n" + "\n".join(rows) + "\n")
        return p

    def test_load_normalize_and_validate_membership(self, tmp_path, monkeypatch):
        p = self._csv(tmp_path, [
            "pdd,proof,2026-08-12,2026-11-12",
            "CWEN-A,proof,2026-08-12,2026-11-12",
        ])
        monkeypatch.setattr(sched, "_PHASE_C_SUPPLEMENT_CSV", p)
        monkeypatch.setattr(sched, "execute",
                            lambda sql, params=None, **kw: [("PDD",), ("CWEN-A",)])
        out = sched._load_supplement_tickers(today=date(2026, 8, 12))
        assert out == {"PDD", "CWEN-A"}

    def test_duplicate_ticker_rejected(self, tmp_path, monkeypatch):
        p = self._csv(tmp_path, [
            "PDD,proof,2026-08-12,2026-11-12",
            "pdd,proof,2026-08-12,2026-11-12",
        ])
        monkeypatch.setattr(sched, "_PHASE_C_SUPPLEMENT_CSV", p)
        monkeypatch.setattr(sched, "execute", lambda *a, **kw: [("PDD",)])
        with pytest.raises(ValueError, match="重复"):
            sched._load_supplement_tickers(today=date(2026, 8, 12))

    def test_non_universe_ticker_rejected(self, tmp_path, monkeypatch):
        p = self._csv(tmp_path, ["NOPE,proof,2026-08-12,2026-11-12"])
        monkeypatch.setattr(sched, "_PHASE_C_SUPPLEMENT_CSV", p)
        monkeypatch.setattr(sched, "execute", lambda *a, **kw: [])
        with pytest.raises(ValueError, match="非 US universe"):
            sched._load_supplement_tickers(today=date(2026, 8, 12))

    def test_empty_or_malformed_rejected(self, tmp_path, monkeypatch):
        p = self._csv(tmp_path, ["\"\",proof,2026-08-12,2026-11-12"])
        monkeypatch.setattr(sched, "_PHASE_C_SUPPLEMENT_CSV", p)
        with pytest.raises(ValueError, match="非法"):
            sched._load_supplement_tickers(today=date(2026, 8, 12))

    def test_expired_review_warns_but_keeps_scope(self, tmp_path, monkeypatch, caplog):
        p = self._csv(tmp_path, ["PDD,proof,2026-01-01,2026-06-01"])
        monkeypatch.setattr(sched, "_PHASE_C_SUPPLEMENT_CSV", p)
        monkeypatch.setattr(sched, "execute", lambda *a, **kw: [("PDD",)])
        with caplog.at_level("ERROR"):
            out = sched._load_supplement_tickers(today=date(2026, 8, 12))
        assert out == {"PDD"}  # 保留范围,不静默移出
        assert any("已过期" in r.message for r in caplog.records)


class TestSupplementScopeMerge:
    def test_scope_is_index_union_supplement(self, monkeypatch):
        """C2 §4.1.3:最终 scope = index ∪ supplement,universe 缺口为 0。"""
        monkeypatch.setattr(sched, "execute", lambda sql, **kw: [("A",), ("B",), ("C",)])
        out = sched._reconcile_us_universe({"A", "B", "C"}, {"A"}, [])
        assert out["universe_not_in_sync_scope"] == []
        assert out["scope_ticker_count"] == 3
        assert out["index_only_tickers"] == []

    def test_expected_skip_in_index_stays_out_of_scope(self, monkeypatch):
        """C2 §4.1.4:GFS/MASI 在指数内但因受控 skip 仍 out_of_sync_scope。"""
        monkeypatch.setattr(sched, "execute",
                            lambda sql, **kw: [("GFS",), ("A",)])
        out = sched._reconcile_us_universe({"GFS", "A"}, {"GFS", "A"}, ["GFS"])
        assert out["out_of_sync_scope"] == ["GFS"]
        assert out["expected_skip_in_universe"] == ["GFS"]

    def test_uncovered_universe_blocks(self, monkeypatch, tmp_path):
        """C2 §4.1.5 旁证:universe 股票既不在 scope 又非 expected_skip → 阻断。"""
        calls = _orch_mocks(monkeypatch, tmp_path, {
            "success": 20, "failed": 0, "skipped": 982, "no_write": [],
            "errors": [], "index_tickers": {"A"},
        })
        monkeypatch.setattr(sched, "_reconcile_us_universe", lambda scope, idx, es: {
            "universe_count": 1003, "index_ticker_count": 1000, "scope_ticker_count": 1032,
            "out_of_sync_scope": ["NEWSTOCK"], "index_only_tickers": ["Z9"],
            "universe_not_in_sync_scope": ["NEWSTOCK"], "expected_skip_in_universe": [],
        })
        with pytest.raises(RuntimeError, match="未分类 universe"):
            sched._run_us_financial_orchestration(0.0)
        assert calls == []


class TestSupplementSkipKindPrecision:
    def test_ccep_zero_facts_only(self):
        """CCEP 的 FOREIGN_IFRS 台账只放行 zero_facts;ingest 失败必须阻断。"""
        skips = {"CCEP": {"stock_code": "CCEP", "reason_code": "FOREIGN_IFRS_NO_USGAAP_FACTS"}}
        ok = sched._classify_us_sync_outcome(
            {"no_write": ["CCEP"], "index_errors": [], "failures": []}, skips)
        assert ok["expected_skip"] == ["CCEP"]
        bad = sched._classify_us_sync_outcome(
            {"no_write": [], "index_errors": [],
             "failures": [{"ticker": "CCEP", "kind": "ingest", "error": "writer boom"}]}, skips)
        assert bad["blocking_failure"] == ["CCEP"]

    def test_spy_404_only(self):
        """SPY 的 404 台账只放行 fetch_404。"""
        skips = {"SPY": {"stock_code": "SPY", "reason_code": "COMPANYFACTS_PERMANENT_404"}}
        ok = sched._classify_us_sync_outcome(
            {"no_write": [], "index_errors": [],
             "failures": [{"ticker": "SPY", "kind": "fetch_404", "error": "404"}]}, skips)
        assert ok["expected_skip"] == ["SPY"]
        bad = sched._classify_us_sync_outcome(
            {"no_write": ["SPY"], "index_errors": [], "failures": []}, skips)
        assert bad["blocking_failure"] == ["SPY"]

    def test_third_ticker_same_failure_not_covered(self):
        """台账只覆盖登记 ticker;第三只补充 ticker 相同失败仍阻断。"""
        skips = {"SPY": {"stock_code": "SPY", "reason_code": "COMPANYFACTS_PERMANENT_404"}}
        out = sched._classify_us_sync_outcome(
            {"no_write": [], "index_errors": [],
             "failures": [{"ticker": "BIDU", "kind": "fetch_404", "error": "404"}]}, skips)
        assert out["blocking_failure"] == ["BIDU"]
