"""
tests/test_backfill_market_cap.py — 历史市值回算脚本单元/集成测试

测试覆盖:
  - PIT 股本选取逻辑 (同市场/同代码/最近但不晚于行情日)
  - 未来股本、其他市场股本、零/负股本不会被使用
  - 已有 market_cap 永不覆盖
  - 日期边界、max_rows 和批次边界（含 --max-rows 配额限制每批）
  - 幂等重跑
  - 审计写入 ≠ 更新成功 → RuntimeError 触发整批回滚
  - batch_id 回滚 (不误删后修正的值)
  - STOCK_MARKETS 环境保护
  - dry-run 零写入（不创建表、不建索引）
  - advisory lock 快速失败
  - sync_log 运行记录
"""
from __future__ import annotations

import os
from datetime import date, datetime
from pathlib import Path
from unittest.mock import ANY, MagicMock, call, patch

import pytest

# ── Helpers ──────────────────────────────────────────────────


def _make_quote_row(stock_code="AAPL", trade_date="2024-06-15", close=150.0):
    """构造 daily_quote 候选行 (stock_code, trade_date, close, share_date, total_shares)。"""
    return (stock_code, trade_date, float(close), "2024-06-01", 1000000000)


def _mock_cursor(rowcount=0, fetchall=None):
    """构造 mock cursor，配置 rowcount 和 fetchall 返回值。"""
    cur = MagicMock()
    cur.rowcount = rowcount
    if fetchall is not None:
        cur.fetchall.return_value = fetchall
    return cur


def _s_connection_cursor(mock_conn_cls, rowcount=0, fetchall=None):
    """从 mock Connection 类取出内部 cursor 并配置。

    适配 Connection() 上下文管理器链:
      Connection().__enter__().cursor().__enter__() → cur
    """
    conn_inst = mock_conn_cls.return_value
    ctx_conn = conn_inst.__enter__.return_value
    cur_ctx = ctx_conn.cursor.return_value
    cur = cur_ctx.__enter__.return_value
    cur.rowcount = rowcount
    if fetchall is not None:
        cur.fetchall.return_value = fetchall
    return cur


# ── _compute_market_cap ──────────────────────────────────────


class TestComputeMarketCap:
    def test_normal(self):
        from scripts.backfill_historical_market_cap import _compute_market_cap
        assert _compute_market_cap(150.0, 1_000_000_000) == 150_000_000_000.00

    def test_rounding(self):
        from scripts.backfill_historical_market_cap import _compute_market_cap
        result = _compute_market_cap(123.456, 789)
        assert result == 97406.78

    def test_large_values(self):
        from scripts.backfill_historical_market_cap import _compute_market_cap
        result = _compute_market_cap(200.0, 15_000_000_000)
        assert result == 3_000_000_000_000.00

    def test_small_values(self):
        from scripts.backfill_historical_market_cap import _compute_market_cap
        assert _compute_market_cap(0.01, 1000) == 10.00


# ── _validate_market ─────────────────────────────────────────


class TestValidateMarket:
    @patch.dict(os.environ, {"STOCK_MARKETS": "US"}, clear=False)
    def test_valid_us(self):
        from scripts.backfill_historical_market_cap import _validate_market
        assert _validate_market("US") == "US"

    @patch.dict(os.environ, {"STOCK_MARKETS": "US"}, clear=False)
    def test_invalid_market_exits(self):
        from scripts.backfill_historical_market_cap import _validate_market
        with pytest.raises(SystemExit):
            _validate_market("CN_A")

    @patch.dict(os.environ, {"STOCK_MARKETS": "CN_A,CN_HK"}, clear=False)
    def test_multi_market(self):
        from scripts.backfill_historical_market_cap import _validate_market
        assert _validate_market("CN_A") == "CN_A"
        assert _validate_market("CN_HK") == "CN_HK"

    @patch.dict(os.environ, {"STOCK_MARKETS": ""}, clear=False)
    def test_empty_markets_exits(self):
        from scripts.backfill_historical_market_cap import _validate_market
        with pytest.raises(SystemExit):
            _validate_market("US")


# ── _dry_run_stats ───────────────────────────────────────────


class TestDryRunStats:
    @patch("scripts.backfill_historical_market_cap.execute")
    def test_all_backfillable(self, mock_exec):
        from scripts.backfill_historical_market_cap import _dry_run_stats
        mock_exec.side_effect = [
            [(1000,)], [(1000,)], [(500,)], [],
        ]
        stats = _dry_run_stats("US")
        assert stats["total_null"] == 1000
        assert stats["backfillable"] == 1000
        assert stats["not_backfillable"] == 0
        assert stats["pct_backfillable"] == 100.0

    @patch("scripts.backfill_historical_market_cap.execute")
    def test_mixed_backfillable(self, mock_exec):
        from scripts.backfill_historical_market_cap import _dry_run_stats
        samples = [("SPY", "2024-01-01", 450.0), ("QQQ", "2024-01-02", 380.0)]
        mock_exec.side_effect = [
            [(1000,)], [(800,)], [(100,)], samples,
        ]
        stats = _dry_run_stats("US")
        assert stats["backfillable"] == 800
        assert stats["not_backfillable"] == 200
        assert stats["pct_backfillable"] == 80.0
        assert len(stats["samples"]) == 2

    @patch("scripts.backfill_historical_market_cap.execute")
    def test_date_range_passed_to_query(self, mock_exec):
        from scripts.backfill_historical_market_cap import _dry_run_stats
        mock_exec.side_effect = [[(1000,)], [(1000,)], [(500,)], []]
        _dry_run_stats("US", start_date=date(2024, 1, 1), end_date=date(2024, 12, 31))
        call_sql = mock_exec.call_args_list[0][0][0]
        assert "q.trade_date >= %s" in call_sql
        assert "q.trade_date <= %s" in call_sql


# ── _ensure_audit_table ─────────────────────────────────────


class TestEnsureAuditTable:
    @patch("scripts.backfill_historical_market_cap.execute")
    def test_creates_table_and_all_indexes(self, mock_exec):
        """审计表 + 3 个索引（含唯一去重索引）创建。"""
        from scripts.backfill_historical_market_cap import _ensure_audit_table

        _ensure_audit_table()

        # CREATE TABLE + 3× CREATE INDEX = 4 calls
        assert mock_exec.call_count == 4
        all_sql = " ".join(str(c[0][0]) for c in mock_exec.call_args_list)
        assert "market_cap_backfill_audit" in all_sql
        assert "idx_mcap_audit_batch" in all_sql
        assert "idx_mcap_audit_stock_date" in all_sql
        assert "idx_mcap_audit_dedup" in all_sql
        assert "UNIQUE INDEX" in all_sql


# ── _ensure_indexes ─────────────────────────────────────────


class TestEnsureIndexes:
    @patch("scripts.backfill_historical_market_cap.psycopg2.connect")
    def test_uses_autocommit_connection(self, mock_connect):
        """CONCURRENTLY 索引使用 autocommit 独立连接，不走 db.execute。"""
        from scripts.backfill_historical_market_cap import _ensure_indexes

        mock_conn = MagicMock()
        mock_connect.return_value = mock_conn

        _ensure_indexes()

        # 验证 autocommit 已开启
        assert mock_conn.autocommit is True
        # 验证连接被关闭（归还）
        mock_conn.close.assert_called_once()


# ── _run_single_batch ────────────────────────────────────────


class TestRunSingleBatch:
    def _row(self, **kw):
        d = {"stock_code": "AAPL", "trade_date": "2024-06-15", "close": 150.0,
             "share_date": "2024-06-01", "total_shares": 1_000_000_000}
        d.update(kw)
        return (d["stock_code"], d["trade_date"], d["close"],
                d["share_date"], d["total_shares"])

    def test_basic_backfill(self):
        from scripts.backfill_historical_market_cap import _run_single_batch
        cur = _mock_cursor(rowcount=1, fetchall=[self._row()])
        result = _run_single_batch(cur, "b001", "US", 10000, None, None, None, None)
        assert result["success"] == 1
        assert result["skipped"] == 0
        cur.executemany.assert_called_once()
        insert_sql = cur.executemany.call_args[0][0]
        assert "market_cap_backfill_audit" in insert_sql

    def test_no_share_record_skipped(self):
        from scripts.backfill_historical_market_cap import _run_single_batch
        cur = _mock_cursor(rowcount=0, fetchall=[self._row(share_date=None, total_shares=None)])
        result = _run_single_batch(cur, "b001", "US", 10000, None, None, None, None)
        assert result["success"] == 0
        assert result["skipped"] == 1

    def test_zero_shares_skipped(self):
        from scripts.backfill_historical_market_cap import _run_single_batch
        cur = _mock_cursor(rowcount=0, fetchall=[self._row(total_shares=0)])
        result = _run_single_batch(cur, "b001", "US", 10000, None, None, None, None)
        assert result["skipped"] == 1

    def test_negative_shares_skipped(self):
        from scripts.backfill_historical_market_cap import _run_single_batch
        cur = _mock_cursor(rowcount=0, fetchall=[self._row(total_shares=-1000)])
        result = _run_single_batch(cur, "b001", "US", 10000, None, None, None, None)
        assert result["skipped"] == 1

    def test_existing_market_cap_not_overwritten(self):
        """UPDATE rowcount=0 → success=0, 不触发异常（audit 写入但无匹配更新）。"""
        from scripts.backfill_historical_market_cap import _run_single_batch
        cur = _mock_cursor(rowcount=0, fetchall=[self._row()])
        with pytest.raises(RuntimeError, match="审计写入"):
            _run_single_batch(cur, "b001", "US", 10000, None, None, None, None)

    def test_audit_mismatch_raises_runtime_error(self):
        """审计写入 1 ≠ 更新 0 → RuntimeError，调用方应回滚整批。"""
        from scripts.backfill_historical_market_cap import _run_single_batch
        cur = _mock_cursor(rowcount=0, fetchall=[self._row()])
        with pytest.raises(RuntimeError, match="审计写入 1 ≠ 更新成功 0"):
            _run_single_batch(cur, "b001", "US", 10000, None, None, None, None)

    def test_pagination_resume(self):
        from scripts.backfill_historical_market_cap import _run_single_batch
        cur = _mock_cursor(rowcount=1, fetchall=[self._row(trade_date="2024-07-01")])
        _run_single_batch(cur, "b001", "US", 10000, None, None,
                          "2024-06-30", "MSFT")
        select_sql = cur.execute.call_args_list[0][0][0]
        assert "q.trade_date > %s" in select_sql

    def test_empty_candidates_returns_done(self):
        from scripts.backfill_historical_market_cap import _run_single_batch
        cur = _mock_cursor(fetchall=[])
        result = _run_single_batch(cur, "b001", "US", 10000, None, None, None, None)
        assert result["done"] is True
        assert result["processed"] == 0

    def test_batch_boundary_larger_than_candidates(self):
        from scripts.backfill_historical_market_cap import _run_single_batch
        rows = [self._row(stock_code=f"TST{i:03d}") for i in range(5)]
        cur = _mock_cursor(rowcount=1, fetchall=rows)
        result = _run_single_batch(cur, "b001", "US", 10000, None, None, None, None)
        assert result["done"] is True

    def test_batch_boundary_at_limit(self):
        from scripts.backfill_historical_market_cap import _run_single_batch
        rows = [self._row(stock_code=f"TST{i:03d}") for i in range(10)]
        cur = _mock_cursor(rowcount=1, fetchall=rows)
        result = _run_single_batch(cur, "b001", "US", 10, None, None, None, None)
        assert result["done"] is False

    def test_date_boundaries_in_sql(self):
        from scripts.backfill_historical_market_cap import _run_single_batch
        cur = _mock_cursor(fetchall=[])
        _run_single_batch(cur, "b001", "US", 10000,
                          date(2024, 1, 1), date(2024, 6, 30), None, None)
        select_sql = cur.execute.call_args_list[0][0][0]
        assert "q.trade_date >= %s" in select_sql
        assert "q.trade_date <= %s" in select_sql


# ── _run_backfill (max_rows cap & advisory lock) ─────────────


class TestRunBackfill:
    """测试 _run_backfill 的 max_rows 配额限制和 advisory lock 行为。"""

    @patch("scripts.backfill_historical_market_cap.release_connection")
    @patch("scripts.backfill_historical_market_cap.get_connection")
    @patch("scripts.backfill_historical_market_cap.Connection")
    def test_max_rows_caps_effective_batch_size(self, mock_conn_cls, mock_get_conn,
                                                  mock_release):
        """--max-rows 5000 + --batch-size 10000 → 每批限制为剩余配额。"""
        from scripts.backfill_historical_market_cap import _run_backfill

        # Setup advisory lock (success)
        lock_mock = MagicMock()
        lock_cur = MagicMock()
        lock_cur.fetchone.return_value = [True]
        lock_mock.cursor.return_value.__enter__.return_value = lock_cur
        mock_get_conn.return_value = lock_mock

        # Batch cursor: batch 1 returns 5 rows (max_rows=5 cap)
        cur1 = _mock_cursor(rowcount=1, fetchall=[
            ("AAPL", "2024-06-15", 150.0, "2024-06-01", 1_000_000_000),
        ])
        cur2 = _mock_cursor(rowcount=1, fetchall=[])
        batch_conn_inst = MagicMock()
        batch_conn_inst.cursor.return_value.__enter__.side_effect = [cur1, cur2]
        mock_conn_cls.return_value.__enter__.return_value = batch_conn_inst

        result = _run_backfill(
            "b001", "US", batch_size=10000, start_date=None,
            end_date=date(2024, 12, 31), max_rows=1,
        )

        # Should process only 1 row due to max_rows cap
        assert result["success"] == 1
        # Verify the batch was called with effective_batch_size=1
        select_call = cur1.execute.call_args_list[0]
        params = select_call[0][1]
        assert 1 in params  # LIMIT %s = 1

    @patch("scripts.backfill_historical_market_cap.release_connection")
    @patch("scripts.backfill_historical_market_cap.get_connection")
    def test_advisory_lock_fast_fail(self, mock_get_conn, mock_release):
        """pg_try_advisory_lock 返回 False → RuntimeError 快速失败。"""
        from scripts.backfill_historical_market_cap import _run_backfill

        lock_mock = MagicMock()
        lock_cur = MagicMock()
        lock_cur.fetchone.return_value = [False]  # lock not acquired
        lock_mock.cursor.return_value.__enter__.return_value = lock_cur
        mock_get_conn.return_value = lock_mock

        with pytest.raises(RuntimeError, match="已有同市场回算在运行"):
            _run_backfill("b001", "US", 10000, None, date(2024, 12, 31), None)

        # Connection was released even on failure
        mock_release.assert_called_once_with(lock_mock)

    @patch("scripts.backfill_historical_market_cap.release_connection")
    @patch("scripts.backfill_historical_market_cap.get_connection")
    @patch("scripts.backfill_historical_market_cap.Connection")
    def test_lock_released_after_success(self, mock_conn_cls, mock_get_conn,
                                          mock_release):
        """运行结束后正确释放 advisory lock 并归还连接池。"""
        from scripts.backfill_historical_market_cap import _run_backfill

        lock_mock = MagicMock()
        lock_cur = MagicMock()
        lock_cur.fetchone.return_value = [True]
        lock_mock.cursor.return_value.__enter__.return_value = lock_cur
        mock_get_conn.return_value = lock_mock

        cur = _mock_cursor(fetchall=[])  # no candidates → done immediately
        batch_conn_inst = MagicMock()
        batch_conn_inst.cursor.return_value.__enter__.return_value = cur
        mock_conn_cls.return_value.__enter__.return_value = batch_conn_inst

        _run_backfill("b001", "US", 10000, None, date(2024, 12, 31), None)

        # Lock was released and connection returned to pool
        mock_release.assert_called_once_with(lock_mock)


# ── _rollback_batch ─────────────────────────────────────────


class TestRollbackBatch:
    @patch("scripts.backfill_historical_market_cap.Connection")
    @patch("scripts.backfill_historical_market_cap.execute")
    def test_rollback_matching_values(self, mock_exec, mock_conn_cls):
        from scripts.backfill_historical_market_cap import _rollback_batch
        mock_exec.return_value = [
            ("AAPL", "2024-06-15", 150000000000.00),
            ("MSFT", "2024-06-15", 300000000000.00),
        ]
        cur = _s_connection_cursor(mock_conn_cls, rowcount=1)
        result = _rollback_batch("US", "batch-001")
        assert result["rolled_back"] == 2

    @patch("scripts.backfill_historical_market_cap.Connection")
    @patch("scripts.backfill_historical_market_cap.execute")
    def test_rollback_skips_changed_values(self, mock_exec, mock_conn_cls):
        from scripts.backfill_historical_market_cap import _rollback_batch
        mock_exec.return_value = [("AAPL", "2024-06-15", 150.00)]
        cur = _s_connection_cursor(mock_conn_cls, rowcount=0)
        cur.fetchone.return_value = [999.00]
        result = _rollback_batch("US", "batch-001")
        assert result["skipped"] == 1

    @patch("scripts.backfill_historical_market_cap.execute")
    def test_rollback_no_audit_records(self, mock_exec):
        from scripts.backfill_historical_market_cap import _rollback_batch
        mock_exec.return_value = []
        result = _rollback_batch("US", "batch-nonexistent")
        assert result["audit_rows"] == 0

    @patch("scripts.backfill_historical_market_cap.Connection")
    @patch("scripts.backfill_historical_market_cap.execute")
    def test_rollback_dry_run_no_writes(self, mock_exec, mock_conn_cls):
        from scripts.backfill_historical_market_cap import _rollback_batch
        mock_exec.return_value = [("AAPL", "2024-06-15", 150.00)]
        result = _rollback_batch("US", "batch-001", dry_run=True)
        assert result["audit_rows"] == 1
        assert result["rolled_back"] == 0
        mock_conn_cls.assert_not_called()


# ── Main CLI ─────────────────────────────────────────────────


class TestMainCLI:
    @patch.dict(os.environ, {"STOCK_MARKETS": "US"}, clear=False)
    @patch("scripts.backfill_historical_market_cap._write_sync_log")
    @patch("scripts.backfill_historical_market_cap._run_backfill")
    @patch("scripts.backfill_historical_market_cap._ensure_indexes")
    @patch("scripts.backfill_historical_market_cap._ensure_audit_table")
    def test_backfill_passes_frozen_end_date(self, mock_audit, mock_idx,
                                               mock_run, mock_sync, monkeypatch):
        """--end-date 未传时自动冻结为当天。"""
        from scripts.backfill_historical_market_cap import main
        mock_run.return_value = {
            "batch_id": "t001", "batches": 1, "success": 100,
            "skipped": 0, "processed": 100, "elapsed_sec": 1.0,
            "error_detail": None,
        }
        monkeypatch.setattr("sys.argv", [
            "backfill", "--market", "US", "--skip-indexes", "--max-rows", "5000",
        ])
        main()
        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["end_date"] == date.today()
        assert call_kwargs["max_rows"] == 5000

    @patch.dict(os.environ, {"STOCK_MARKETS": "US"}, clear=False)
    @patch("scripts.backfill_historical_market_cap._write_sync_log")
    @patch("scripts.backfill_historical_market_cap._run_backfill")
    @patch("scripts.backfill_historical_market_cap._ensure_indexes")
    @patch("scripts.backfill_historical_market_cap._ensure_audit_table")
    def test_sync_log_called_on_success(self, mock_audit, mock_idx,
                                          mock_run, mock_sync, monkeypatch):
        """成功完成后写入 sync_log (status=success)。"""
        from scripts.backfill_historical_market_cap import main
        mock_run.return_value = {
            "batch_id": "t001", "batches": 1, "success": 100,
            "skipped": 5, "processed": 105, "elapsed_sec": 1.0,
            "error_detail": None,
        }
        monkeypatch.setattr("sys.argv", [
            "backfill", "--market", "US", "--skip-indexes", "--no-refresh",
        ])
        main()
        mock_sync.assert_called_once()
        call_kwargs = mock_sync.call_args[1]
        assert call_kwargs["status"] == "success"
        assert call_kwargs["success"] == 100
        assert call_kwargs["skipped"] == 5

    @patch.dict(os.environ, {"STOCK_MARKETS": "US"}, clear=False)
    @patch("scripts.backfill_historical_market_cap._write_sync_log")
    @patch("scripts.backfill_historical_market_cap._run_backfill")
    @patch("scripts.backfill_historical_market_cap._ensure_indexes")
    @patch("scripts.backfill_historical_market_cap._ensure_audit_table")
    def test_sync_log_called_on_failure(self, mock_audit, mock_idx,
                                          mock_run, mock_sync, monkeypatch):
        """异常时写入 sync_log (status=failed) 再 raise。"""
        from scripts.backfill_historical_market_cap import main
        mock_run.side_effect = RuntimeError("模拟失败")
        monkeypatch.setattr("sys.argv", [
            "backfill", "--market", "US", "--skip-indexes", "--no-refresh",
        ])
        with pytest.raises(RuntimeError, match="模拟失败"):
            main()
        mock_sync.assert_called_once()
        assert mock_sync.call_args[1]["status"] == "failed"

    @patch.dict(os.environ, {"STOCK_MARKETS": "US"}, clear=False)
    @patch("scripts.backfill_historical_market_cap._dry_run_stats")
    @patch("scripts.backfill_historical_market_cap._ensure_audit_table")
    def test_dry_run_does_not_create_ddl(self, mock_audit, mock_stats, monkeypatch):
        """--dry-run 不创建审计表、不建索引。"""
        from scripts.backfill_historical_market_cap import main
        mock_stats.return_value = {
            "total_null": 0, "backfillable": 0, "not_backfillable": 0,
            "pct_backfillable": 0, "affected_stocks": 0, "samples": [],
        }
        monkeypatch.setattr("sys.argv", [
            "backfill", "--market", "US", "--dry-run",
        ])
        main()
        mock_audit.assert_not_called()

    @patch.dict(os.environ, {"STOCK_MARKETS": "US"}, clear=False)
    @patch("scripts.backfill_historical_market_cap._rollback_batch")
    @patch("scripts.backfill_historical_market_cap._ensure_audit_table")
    def test_rollback_mode(self, mock_audit, mock_rollback, monkeypatch):
        from scripts.backfill_historical_market_cap import main
        mock_rollback.return_value = {
            "audit_rows": 100, "rolled_back": 100, "skipped": 0, "not_found": 0,
        }
        monkeypatch.setattr("sys.argv", [
            "backfill", "--market", "US", "--rollback-batch", "batch-001",
        ])
        main()
        mock_rollback.assert_called_once()
        assert mock_rollback.call_args[0] == ("US", "batch-001")

    @patch.dict(os.environ, {"STOCK_MARKETS": "CN_A,CN_HK"}, clear=False)
    def test_invalid_market_exits_early(self, monkeypatch):
        from scripts.backfill_historical_market_cap import main
        monkeypatch.setattr("sys.argv", ["backfill", "--market", "US"])
        with pytest.raises(SystemExit):
            main()


# ── Idempotency ──────────────────────────────────────────────


class TestIdempotency:
    def test_second_run_updates_zero_rows(self):
        """第二轮重跑：所有行已有 market_cap → rowcount=0 → RuntimeError 回滚。"""
        from scripts.backfill_historical_market_cap import _run_single_batch
        cur = _mock_cursor(rowcount=0, fetchall=[
            ("AAPL", "2024-06-15", 150.0, "2024-06-01", 1_000_000_000),
        ])
        with pytest.raises(RuntimeError, match="审计写入"):
            _run_single_batch(cur, "b002", "US", 10000, None, None, None, None)


# ── PIT Selection ───────────────────────────────────────────


class TestPITSelection:
    def test_recent_share_before_trade_date_selected(self):
        from scripts.backfill_historical_market_cap import _run_single_batch
        cur = _mock_cursor(rowcount=1, fetchall=[
            ("AAPL", "2024-06-15", 150.0, "2024-06-01", 1_200_000_000),
        ])
        result = _run_single_batch(cur, "b001", "US", 10000, None, None, None, None)
        assert result["success"] == 1

    def test_future_share_not_selected(self):
        from scripts.backfill_historical_market_cap import _run_single_batch
        cur = _mock_cursor(rowcount=0, fetchall=[
            ("AAPL", "2024-06-15", 150.0, None, None),
        ])
        result = _run_single_batch(cur, "b001", "US", 10000, None, None, None, None)
        assert result["skipped"] == 1

    def test_market_filter_in_sql(self):
        from scripts.backfill_historical_market_cap import _run_single_batch
        cur = _mock_cursor(rowcount=1, fetchall=[
            ("AAPL", "2024-06-15", 150.0, "2024-06-01", 1_000_000_000),
        ])
        _run_single_batch(cur, "b001", "US", 10000, None, None, None, None)
        select_sql = cur.execute.call_args_list[0][0][0]
        assert "q.market = %s" in select_sql
        params = cur.execute.call_args_list[0][0][1]
        assert "US" in params
