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
    sync_result.setdefault("raw_index_tickers", set(sync_result.get("index_tickers", set())))
    planned = sorted(sync_result.get("index_tickers", set()))
    sync_result.setdefault("directory", {
        "status": "success", "complete": True,
        "planned_scope": planned, "collectable_scope": planned,
        "blocked_tickers": [], "items": [], "inserted": [], "rejected": [],
    })
    monkeypatch.setattr(sched, "_sync_us", lambda: sync_result)
    monkeypatch.setattr(sched, "_load_expected_skips", lambda today=None: {})
    monkeypatch.setattr(sched, "_load_index_only_registry", lambda: {"Z9"})
    import core.us_security_identity as ident_mod
    monkeypatch.setattr(ident_mod, "validate_us_security_symbols", lambda symbols=None: [])
    monkeypatch.setattr(ident_mod, "resolve_us_symbols_batch",
                        lambda tickers, symbols=None: {t: t for t in tickers})
    monkeypatch.setattr(sched, "_reconcile_us_universe",
                        lambda scope, idx, es, raw_index_tickers=None, resolved_map=None: {
        "universe_count": 1003, "index_ticker_count": 1000, "scope_ticker_count": 1033,
        "raw_index_ticker_count": 1000,
        "out_of_sync_scope": ["Z1"], "index_only_tickers": ["Z9"],
        "universe_not_in_sync_scope": [], "expected_skip_in_universe": ["Z1"],
    })
    monkeypatch.setattr(sched, "_check_zero_write_baseline", lambda: [])
    monkeypatch.setattr(sched, "_missing_current_ttm_scope", lambda scope: [])
    monkeypatch.setattr(sched, "_PHASE_C_SUMMARY_DIR", tmp_path)

    import scripts.project_us_financial_snapshots as proj_mod

    monkeypatch.setattr(
        proj_mod, "run_projection",
        lambda **kw: calls.append("projection") or {"projection_run_id": "r1"},
    )
    import core.validate as validate_mod
    monkeypatch.setattr(
        validate_mod, "run_after_sync",
        lambda market="": calls.append("validate") or {"success": True, "errors": 0},
    )
    return calls


class TestOrchestration:
    def test_success_order_projection_once_then_validate(self, monkeypatch, tmp_path):
        calls = _orch_mocks(monkeypatch, tmp_path, {
            "success": 21, "failed": 0, "skipped": 982, "no_write": [],
            "errors": [], "index_tickers": {"A", "B"},
        })
        result = sched._run_us_financial_orchestration(0.0)
        # E-1 后 compare 步骤已摘除(旧对象已删除):projection → validate
        assert calls == ["projection", "validate"]
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
        assert calls == []  # projection/validate 均未运行

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

    def test_index_error_fails_before_any_ticker_sync(self, monkeypatch):
        """指数范围无法解析时，不得只同步 supplement 后再在编排层阻断。"""
        class BrokenFetcher:
            def get_tickers_by_index(self, index):
                raise RuntimeError("constituent source unavailable")

            def get_index_source_status(self, index):
                return {"mode": "stale_cache_expired"}

        called = []
        monkeypatch.setenv("STOCK_US_INDEXES", "RUSSELL1000")
        monkeypatch.setattr("core.fetchers.us_financial.USFinancialFetcher", BrokenFetcher)
        monkeypatch.setattr("core.sync.sync_us_market", lambda args: called.append(args))

        out = sched._sync_us()
        assert out["index_errors"]
        assert out["index_sources"]["RUSSELL1000"]["mode"] == "stale_cache_expired"
        assert called == []

    def test_unregistered_index_only_blocks(self, monkeypatch, tmp_path):
        """验收阻断项 4:未登记的 index-only ticker 阻断发布。"""
        calls = _orch_mocks(monkeypatch, tmp_path, {
            "success": 20, "failed": 0, "skipped": 982, "no_write": [],
            "errors": [], "index_tickers": {"A"},
        })
        monkeypatch.setattr(sched, "_reconcile_us_universe", lambda scope, idx, es, raw_index_tickers=None, resolved_map=None: {
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
        assert calls == ["projection", "validate"]

    def test_zero_write_violation_stops(self, monkeypatch, tmp_path):
        calls = _orch_mocks(monkeypatch, tmp_path, {
            "success": 21, "failed": 0, "skipped": 982, "no_write": [],
            "errors": [], "index_tickers": {"A"},
        })
        monkeypatch.setattr(sched, "_check_zero_write_baseline",
                            lambda: [{"object": "us_income_statement", "error": "已退役对象仍存在"}])
        with pytest.raises(RuntimeError, match="已退役对象仍存在"):
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


class TestDirectoryOrchestration:
    @staticmethod
    def summary(tmp_path):
        runs = list(tmp_path.iterdir())
        assert len(runs) == 1
        return json.loads((runs[0] / "summary.json").read_text())

    @staticmethod
    def rejected(code="Z9"):
        return {
            "canonical_code": code, "raw_ticker": code, "raw_tickers": [code],
            "decision": "rejected", "reasons": ["fixture identity conflict"],
        }

    @pytest.mark.parametrize("registry", [{"Z9"}, set()])
    def test_directory_rejection_cannot_be_cleared_by_csv_or_r2(
            self, monkeypatch, tmp_path, registry):
        calls = _orch_mocks(monkeypatch, tmp_path, {
            "success": 20, "failed": 0, "skipped": 0, "no_write": [],
            "errors": [], "index_tickers": {"A"},
            "directory": {
                "status": "partial", "complete": True,
                "planned_scope": ["A", "Z9"], "collectable_scope": ["A"],
                "blocked_tickers": ["Z9"], "items": [self.rejected()],
                "rejected": [self.rejected()], "inserted": [],
            },
        })
        monkeypatch.setattr(sched, "_load_index_only_registry", lambda: registry)
        import core.us_universe_registry as reg_mod
        registered = []
        monkeypatch.setattr(reg_mod, "auto_verify_and_register",
                            lambda *a, **kw: registered.append(a) or {
                                "index_only_registered": ["Z9"],
                                "index_only_failed": [],
                                "supplement_registered": []})
        with pytest.raises(RuntimeError, match="证券目录 partial"):
            sched._run_us_financial_orchestration(0.0)
        assert calls == [] and registered == []
        summary = self.summary(tmp_path)
        assert summary["status"] == "blocked"
        assert summary["projection"] == "not_run"
        assert summary["auto_registry"]["skipped_by_directory"][0]["stock_code"] == "Z9"

    def test_r2_keeps_unaffected_duties_but_cannot_publish(self, monkeypatch, tmp_path):
        calls = _orch_mocks(monkeypatch, tmp_path, {
            "success": 1, "failed": 0, "skipped": 0, "no_write": [],
            "errors": [], "index_tickers": {"A"},
            "directory": {
                "status": "partial", "complete": True,
                "planned_scope": ["A", "Z9"], "collectable_scope": ["A"],
                "blocked_tickers": ["Z9"], "items": [self.rejected()],
                "rejected": [self.rejected()], "inserted": [],
            },
        })
        monkeypatch.setattr(sched, "_load_index_only_registry", lambda: set())
        monkeypatch.setattr(sched, "_reconcile_us_universe",
                            lambda scope, idx, es, raw_index_tickers=None, resolved_map=None: {
            "universe_count": 1, "index_ticker_count": 2, "scope_ticker_count": 2,
            "raw_index_ticker_count": 2, "out_of_sync_scope": [],
            "index_only_tickers": ["Z9", "SAFE"],
            "universe_not_in_sync_scope": [], "expected_skip_in_universe": [],
        })
        import core.us_universe_registry as reg_mod
        received = []

        def auto(index_only, uncovered, **kw):
            received.append((index_only, uncovered))
            return {"index_only_registered": ["SAFE"], "index_only_failed": [],
                    "supplement_registered": []}

        monkeypatch.setattr(reg_mod, "auto_verify_and_register", auto)
        with pytest.raises(RuntimeError, match="证券目录 partial"):
            sched._run_us_financial_orchestration(0.0)
        assert received == [(["SAFE"], [])]
        assert calls == []
        summary = self.summary(tmp_path)
        assert summary["auto_registry"]["index_only_registered"] == ["SAFE"]
        assert summary["status"] == "blocked"

    def test_directory_insert_forces_projection_with_existing_facts(self, monkeypatch, tmp_path):
        calls = _orch_mocks(monkeypatch, tmp_path, {
            "success": 0, "failed": 0, "skipped": 1, "no_write": [],
            "errors": [], "index_tickers": {"CRWV"},
            "directory": {
                "status": "success", "complete": True,
                "planned_scope": ["CRWV"], "collectable_scope": ["CRWV"],
                "blocked_tickers": [], "items": [], "inserted": ["CRWV"], "rejected": [],
            },
        })
        sched._run_us_financial_orchestration(0.0)
        assert calls == ["projection", "validate"]
        assert self.summary(tmp_path)["projection_trigger"]["directory_inserted"] == ["CRWV"]

    def test_missing_snapshot_from_prior_insert_triggers_recovery_projection(
            self, monkeypatch, tmp_path):
        calls = _orch_mocks(monkeypatch, tmp_path, {
            "success": 0, "failed": 0, "skipped": 1, "no_write": [],
            "errors": [], "index_tickers": {"CRWV"},
            "directory": {
                "status": "success", "complete": True,
                "planned_scope": ["CRWV"], "collectable_scope": ["CRWV"],
                "blocked_tickers": [], "items": [], "inserted": [], "rejected": [],
            },
        })
        monkeypatch.setattr(sched, "_missing_current_ttm_scope", lambda scope: ["CRWV"])
        sched._run_us_financial_orchestration(0.0)
        assert calls == ["projection", "validate"]
        assert self.summary(tmp_path)["projection_trigger"]["missing_current_ttm"] == ["CRWV"]

    def test_failed_directory_with_all_financial_skipped_is_not_no_new_filings(
            self, monkeypatch, tmp_path):
        calls = _orch_mocks(monkeypatch, tmp_path, {
            "success": 0, "failed": 0, "skipped": 1000, "no_write": [],
            "errors": [], "index_tickers": {"A"},
            "directory": {
                "status": "failed", "complete": False, "planned_scope": ["A"],
                "collectable_scope": ["A"], "blocked_tickers": [], "items": [],
                "rejected": [], "inserted": [], "fatal_error": "SEC unavailable",
            },
        })
        with pytest.raises(RuntimeError, match="证券目录 failed"):
            sched._run_us_financial_orchestration(0.0)
        assert calls == []
        summary = self.summary(tmp_path)
        assert summary["status"] == "blocked" and summary["projection"] == "not_run"

    def test_outer_sync_job_logs_failed_after_directory_block(self, monkeypatch, tmp_path):
        _orch_mocks(monkeypatch, tmp_path, {
            "success": 20, "failed": 0, "skipped": 0, "no_write": [],
            "errors": [], "index_tickers": {"A"},
            "directory": {
                "status": "partial", "complete": True, "planned_scope": ["A"],
                "collectable_scope": ["A"], "blocked_tickers": ["Z9"],
                "items": [self.rejected()], "rejected": [self.rejected()], "inserted": [],
            },
        })
        monkeypatch.setattr(sched.config.scheduler, "max_retries", 1)
        import core.sync._utils as utils
        logged = []
        monkeypatch.setattr(utils, "log_sync_result",
                            lambda **kw: logged.append(kw))
        result = sched._run_sync_job("US")
        assert result["success"] is False
        assert logged[-1]["status"] == "failed"
        assert self.summary(tmp_path)["status"] == "blocked"

    def test_sync_us_uses_safe_scope_without_shrinking_plan(self, monkeypatch):
        planned = ["A", "B"]
        scope_result = {
            "total": 0, "success": 0, "failed": 0, "skipped": 0, "elapsed": 0,
            "indexes_synced": ["TEST"], "errors": [], "failures": [],
            "index_errors": [], "no_write": [], "index_sources": {},
            "index_tickers": {"A", "B"}, "raw_index_tickers": {"A", "B"},
            "supplement_tickers": [],
        }
        monkeypatch.setattr(sched, "build_us_sync_scope", lambda: {
            "scope_result": scope_result, "scope": planned,
            "raw_index": {"A", "B"}, "resolved_map": {"A": "A", "B": "B"},
        })
        import core.sync.us_security_directory as directory_mod
        import core.sync as sync_mod
        calls = []

        def fake_directory(**kw):
            calls.append("directory")
            return {"status": "partial", "complete": True,
                    "planned_scope": planned, "collectable_scope": ["A"],
                    "blocked_tickers": ["B"], "items": [], "rejected": []}

        def fake_financial(args):
            calls.append(f"financial:{args.us_tickers}")
            return {"total": 1, "success": 1, "failed": 0, "skipped": 0,
                    "elapsed": 0, "no_write": [], "errors": [], "failures": []}

        monkeypatch.setattr(directory_mod, "sync_us_security_directory", fake_directory)
        monkeypatch.setattr(sync_mod, "sync_us_market", fake_financial)
        result = sched._sync_us()
        assert calls == ["directory", "financial:A"]
        assert result["planned_scope"] == planned
        assert result["collectable_scope"] == ["A"]

    def test_identity_preflight_failure_writes_not_run_summary(self, monkeypatch, tmp_path):
        import core.us_security_identity as ident_mod
        monkeypatch.setattr(ident_mod, "validate_us_security_symbols",
                            lambda symbols=None: ["broken"])
        monkeypatch.setattr(sched, "_PHASE_C_SUMMARY_DIR", tmp_path)
        with pytest.raises(RuntimeError, match="身份表冲突"):
            sched._run_us_financial_orchestration(0.0)
        summary = self.summary(tmp_path)
        assert summary["status"] == "blocked"
        assert summary["sync"]["directory"]["status"] == "not_run"
        assert summary["projection"] == "not_run"

    def test_reconciliation_failure_preserves_completed_sync_summary(self, monkeypatch, tmp_path):
        _orch_mocks(monkeypatch, tmp_path, {
            "success": 1, "failed": 0, "skipped": 0, "no_write": [],
            "errors": [], "index_tickers": {"CRWV"},
        })
        monkeypatch.setattr(sched, "_reconcile_us_universe",
                            lambda *a, **kw: (_ for _ in ()).throw(
                                RuntimeError("reconciliation connection lost")))
        with pytest.raises(RuntimeError, match="connection lost"):
            sched._run_us_financial_orchestration(0.0)
        summary = self.summary(tmp_path)
        assert summary["status"] == "failed"
        assert summary["failure_stage"] == "reconciliation"
        assert summary["sync"]["directory"]["status"] == "success"
        assert summary["sync"]["success"] == 1

    def test_projection_failure_records_stage_and_keeps_sync_summary(self, monkeypatch, tmp_path):
        _orch_mocks(monkeypatch, tmp_path, {
            "success": 1, "failed": 0, "skipped": 0, "no_write": [],
            "errors": [], "index_tickers": {"CRWV"},
        })
        import scripts.project_us_financial_snapshots as proj_mod
        monkeypatch.setattr(proj_mod, "run_projection",
                            lambda **kw: (_ for _ in ()).throw(
                                RuntimeError("projection connection lost")))
        with pytest.raises(RuntimeError, match="projection connection lost"):
            sched._run_us_financial_orchestration(0.0)
        summary = self.summary(tmp_path)
        assert summary["status"] == "failed"
        assert summary["failure_stage"] == "projection"
        assert summary["sync"]["directory"]["status"] == "success"
        assert summary["sync"]["success"] == 1


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

    def test_resurrected_object_is_violation(self, monkeypatch):
        """E-1 后:任何已退役对象仍存在即违规(防复活)。"""

        def fake_execute(sql, params=None, **kw):
            exists = params and params[0] == "us_income_statement"
            return [(params[0] if exists else None,)]

        monkeypatch.setattr(sched, "execute", fake_execute)
        violations = sched._check_zero_write_baseline()
        assert [v["object"] for v in violations] == ["us_income_statement"]

    def test_all_retired_passes(self, monkeypatch):
        monkeypatch.setattr(sched, "execute", lambda sql, params=None, **kw: [(None,)])
        assert sched._check_zero_write_baseline() == []




# ── 11. 静态禁扫:六对象生产引用收敛 ──────────────────────────

class TestStaticScan:
    RETIRING = (
        "us_income_statement", "us_balance_sheet", "us_cash_flow_statement",
        "mv_us_financial_indicator", "mv_us_indicator_ttm", "mv_us_fcf_yield",
    )

    # 允许引用六对象的文件(受控 legacy fallback 分支/审计校验模块/同步配置),
    # 每个都必须能回答"为什么还在";新文件出现引用即失败。
    # 2026-09-03 退役清理:core/validate.py、core/us_financial_chain_audit.py、
    # quant/backtest/universe.py 的旧路径已删除,移出本清单。
    ALLOWLIST = {
        "core/sync/_utils.py",          # 仅注释说明旧 MV 停刷(US special 拒绝守卫)
        "core/sync/us_market.py",       # 仅模块 docstring 说明退役范围
        "core/scheduler.py",            # 仅注释说明停刷
        "core/us_financial_verify.py",       # 退役断言模块(按名称防复活)
        "quant/analyzer/query_us.py",   # B1 受控 legacy fallback 分支
        "quant/screener/query.py",      # B2 受控 legacy fallback 分支
        "quant/backtest/preloader.py",  # B4 受控 legacy fallback 分支
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
        """C2 §4.1.5 旁证:universe 股票既不在 scope 又非 expected_skip → 阻断。

        自动登记（US_UNIVERSE_REGISTRY_AUTOMATION_PLAN）默认会尝试放行；
        本测试把自动化 mock 为全部未通过，验证未通过项维持阻断。
        """
        calls = _orch_mocks(monkeypatch, tmp_path, {
            "success": 20, "failed": 0, "skipped": 982, "no_write": [],
            "errors": [], "index_tickers": {"A"},
        })
        monkeypatch.setattr(sched, "_reconcile_us_universe", lambda scope, idx, es, raw_index_tickers=None, resolved_map=None: {
            "universe_count": 1003, "index_ticker_count": 1000, "scope_ticker_count": 1032,
            "out_of_sync_scope": ["NEWSTOCK"], "index_only_tickers": ["Z9"],
            "universe_not_in_sync_scope": ["NEWSTOCK"], "expected_skip_in_universe": [],
        })
        import core.us_universe_registry as reg_mod
        monkeypatch.setattr(reg_mod, "auto_verify_and_register",
                            lambda *a, **kw: {"index_only_registered": [],
                                              "index_only_failed": [],
                                              "supplement_registered": []})
        with pytest.raises(RuntimeError, match="未分类 universe"):
            sched._run_us_financial_orchestration(0.0)
        assert calls == []

    def test_auto_registration_lifts_blocking(self, monkeypatch, tmp_path):
        """自动验证通过的 index-only / 未分类 universe 解除阻断并写台账。

        台账写入 tmp_path 副本，不碰仓库内真实 CSV。
        """
        calls = _orch_mocks(monkeypatch, tmp_path, {
            "success": 20, "failed": 0, "skipped": 982, "no_write": [],
            "errors": [], "index_tickers": {"A"},
        })
        monkeypatch.setattr(sched, "_load_index_only_registry", lambda: set())
        monkeypatch.setattr(sched, "_reconcile_us_universe", lambda scope, idx, es, raw_index_tickers=None, resolved_map=None: {
            "universe_count": 1003, "index_ticker_count": 1000, "scope_ticker_count": 1032,
            "out_of_sync_scope": ["NEWSTOCK"], "index_only_tickers": ["Z9"],
            "universe_not_in_sync_scope": ["NEWSTOCK"], "expected_skip_in_universe": [],
        })
        # 身份解析：Z9 → CIK 无冲突；companyfacts 200
        import core.us_universe_registry as reg_mod
        from core.us_security_identity import SymbolRow
        from core import sec_client
        monkeypatch.setattr(reg_mod, "_load_identity", lambda: (
            [SymbolRow("Z9", "Z9", "0007777777", "current")], {}, {}))

        class Resp:
            status_code = 200
        monkeypatch.setattr(sec_client, "get", lambda url, **kw: Resp())
        monkeypatch.setattr(sched, "_PHASE_C_INDEX_ONLY_CSV",
                            tmp_path / "index_only.csv")
        monkeypatch.setattr(sched, "_PHASE_C_SUPPLEMENT_CSV",
                            tmp_path / "supplement.csv")
        result = sched._run_us_financial_orchestration(0.0)
        assert calls == ["projection", "validate"]  # 阻断解除，流程走完
        import csv as _csv
        with open(tmp_path / "index_only.csv", newline="") as f:
            rows = list(_csv.DictReader(f))
        assert [r["stock_code"] for r in rows] == ["Z9"]
        assert rows[0]["registered_by"] == "auto"
        with open(tmp_path / "supplement.csv", newline="") as f:
            srows = list(_csv.DictReader(f))
        assert [r["stock_code"] for r in srows] == ["NEWSTOCK"]
        assert srows[0]["registered_by"] == "auto"


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
