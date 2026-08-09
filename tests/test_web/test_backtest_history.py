"""回测历史持久化服务测试。"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from web.services.backtest_history_service import (
    create_run,
    delete_run,
    get_run,
    list_runs,
    migrate_backtest_runs,
    update_run,
)
from web.services.backtest_service import _prepare_run_fields, _detect_preset_type, _tasks, update_task


@patch("web.services.backtest_history_service.execute")
def test_migrate_backtest_runs(mock_execute):
    """迁移函数应执行 DDL。"""
    migrate_backtest_runs()
    assert mock_execute.called
    sql = mock_execute.call_args[0][0]
    assert "CREATE TABLE IF NOT EXISTS backtest_runs" in sql


@patch("web.services.backtest_history_service.execute")
def test_create_run(mock_execute):
    """创建 run 记录时应写入 CREATED 状态。"""
    run_id = str(uuid.uuid4())
    create_run(
        run_id=run_id,
        preset_name="fcf_roe_value",
        preset_type="normal",
        market="CN_A",
        start_month="2022-01",
        end_month="2024-12",
        rebalance_months=6,
        top_n=10,
        initial_capital=1_000_000,
        benchmark="000300",
        timing=False,
        params={"preset_name": "fcf_roe_value", "market": "CN_A"},
    )
    assert mock_execute.called
    args = mock_execute.call_args[0]
    assert args[1][0] == run_id
    assert args[1][11] == "CREATED"


@patch("web.services.backtest_history_service.execute")
def test_update_run(mock_execute):
    """更新 run 时应只更新非 None 字段。"""
    run_id = str(uuid.uuid4())
    update_run(run_id, status="RUNNING", progress_pct=10.0, progress_label="running...")
    assert mock_execute.called
    sql = mock_execute.call_args[0][0]
    assert "status = %s" in sql
    assert "progress_pct = %s" in sql
    assert "progress_label = %s" in sql


@patch("web.services.backtest_history_service.execute")
def test_get_run_from_db(mock_execute):
    """从数据库读取的运行记录应与内存结构兼容。"""
    run_id = uuid.uuid4()
    now = datetime.now()
    mock_execute.return_value = [
        (
            run_id,
            "fcf_roe_value",
            "normal",
            "CN_A",
            "2022-01",
            "2024-12",
            6,
            10,
            1_000_000.0,
            "000300",
            False,
            "DONE",
            100.0,
            "完成",
            None,
            json.dumps({"total_return": 0.1}),
            json.dumps({"preset_name": "fcf_roe_value"}),
            json.dumps({"final_value": 1_100_000}),
            now,
            now,
            now,
            5000,
        )
    ]
    task = get_run(str(run_id))
    assert task is not None
    assert task["task_id"] == str(run_id)
    assert task["status"] == "DONE"
    assert task["metrics"]["total_return"] == 0.1
    assert task["result"]["final_value"] == 1_100_000


@patch("web.services.backtest_history_service.execute")
def test_list_runs_excludes_result(mock_execute):
    """列表查询不应读取 result 字段。"""
    mock_execute.return_value = []
    list_runs(limit=20, offset=0)
    calls = mock_execute.call_args_list
    # 第二个调用是 SELECT 列表
    select_sql = calls[1][0][0]
    assert "result" not in select_sql


@patch("web.services.backtest_history_service.execute")
def test_list_runs_with_rows(mock_execute):
    """列表查询有数据时应返回摘要，且不依赖 result 列。"""
    run_id = uuid.uuid4()
    now = datetime.now()
    mock_execute.side_effect = [
        [(1,)],
        [
            (
                run_id,
                "fcf_roe_value",
                "normal",
                "US",
                "2024-01",
                None,
                6,
                10,
                1_000_000.0,
                "SPY",
                False,
                "DONE",
                100.0,
                "完成",
                None,
                json.dumps({"total_return": 0.12}),
                json.dumps({"preset_name": "fcf_roe_value", "market": "US"}),
                now,
                now,
                now,
                1234,
            )
        ],
    ]

    items, total = list_runs(limit=20, offset=0)

    assert total == 1
    assert items[0]["run_id"] == str(run_id)
    assert "result" not in items[0]
    assert items[0]["metrics"]["total_return"] == 0.12


@patch("web.services.backtest_history_service.execute")
def test_delete_run(mock_execute):
    """删除单条记录。"""
    run_id = str(uuid.uuid4())
    delete_run(run_id)
    assert mock_execute.called
    sql = mock_execute.call_args[0][0]
    assert "DELETE FROM backtest_runs" in sql


def test_detect_preset_type():
    """应能根据 preset_name 识别复合策略。"""
    assert _detect_preset_type({"preset_name": "unknown_value"}) == "normal"


def test_prepare_run_fields_normal():
    """普通策略参数应保留 months/top_n/timing。"""
    params = {
        "preset_name": "fcf_roe_value",
        "market": "CN_A",
        "start": "2022-01",
        "end": "2024-12",
        "months": 6,
        "top_n": 10,
        "initial_capital": 1_000_000,
        "benchmark": "000300",
        "timing": True,
    }
    fields = _prepare_run_fields(params)
    assert fields["preset_type"] == "normal"
    assert fields["rebalance_months"] == 6
    assert fields["top_n"] == 10
    assert fields["timing"] is True


def test_prepare_run_fields_composite():
    """复合策略参数应清空 months/top_n/timing。"""
    params = {
        "preset_name": "commodity_rotation",
        "market": "CN_A",
        "start": "2022-01",
        "end": "2024-12",
        "months": 6,
        "top_n": 10,
        "initial_capital": 1_000_000,
        "benchmark": "000300",
        "timing": True,
    }
    fields = _prepare_run_fields(params)
    assert fields["preset_type"] == "composite"
    assert fields["rebalance_months"] is None
    assert fields["top_n"] is None
    assert fields["timing"] is False


@patch("web.services.backtest_service.update_run")
def test_update_task_sync_db_does_not_duplicate_task_id(mock_update_run):
    """内存 task 同步数据库时不应把 task_id 作为重复关键字传入。"""
    task_id = str(uuid.uuid4())
    _tasks[task_id] = {
        "task_id": task_id,
        "status": "CREATED",
        "progress_pct": 0.0,
        "progress_label": "等待开始...",
        "params": {},
        "result": None,
        "error": None,
        "created_at": datetime.now().isoformat(),
        "started_at": None,
        "completed_at": None,
        "elapsed_ms": None,
    }

    try:
        update_task(task_id, status="RUNNING", progress_label="开始回测...")
    finally:
        _tasks.pop(task_id, None)

    assert mock_update_run.called
    assert mock_update_run.call_args.kwargs["status"] == "RUNNING"


# ── SQL_ASCII 非 ASCII 文本清洗(US 服务器)────────────────────

class TestSafeText:
    def test_sql_ascii_replaces_non_ascii(self):
        from web.services import backtest_history_service as svc
        with patch.object(svc, "_check_db_encoding", return_value="SQL_ASCII"):
            assert svc._safe_text("等待开始...") == "????..."
            assert svc._safe_text("plain ascii") == "plain ascii"
            assert svc._safe_text(None) is None
            assert svc._safe_text(123) == 123

    def test_utf8_passthrough(self):
        from web.services import backtest_history_service as svc
        with patch.object(svc, "_check_db_encoding", return_value="UTF8"):
            assert svc._safe_text("等待开始...") == "等待开始..."

    @patch("web.services.backtest_history_service.execute")
    def test_create_run_sanitizes_label_on_sql_ascii(self, mock_execute):
        """SQL_ASCII 库:中文 progress_label 不得让 create_run 崩溃。"""
        from web.services import backtest_history_service as svc
        with patch.object(svc, "_check_db_encoding", return_value="SQL_ASCII"):
            svc.create_run(
                run_id=str(uuid.uuid4()),
                preset_name="fcf_roe_value",
                preset_type="normal",
                market="US",
                start_month="2025-07",
                end_month="2025-12",
                rebalance_months=6,
                top_n=None,
                initial_capital=1_000_000,
                benchmark=None,
                timing=False,
                params={"preset_name": "fcf_roe_value"},
            )
        label = mock_execute.call_args[0][1][12]
        assert label == "????..."
        label.encode("ascii")  # 必须可被 ASCII 编码

    @patch("web.services.backtest_history_service.execute")
    def test_update_run_sanitizes_error_on_sql_ascii(self, mock_execute):
        from web.services import backtest_history_service as svc
        with patch.object(svc, "_check_db_encoding", return_value="SQL_ASCII"):
            svc.update_run(str(uuid.uuid4()), error="失败:非 ASCII")
            svc.update_run(str(uuid.uuid4()), progress_label="调仓 1/4")
        for call in mock_execute.call_args_list:
            for v in call[0][1]:
                if isinstance(v, str):
                    v.encode("ascii")

    def test_to_jsonb_sanitizes_on_sql_ascii(self):
        from web.services import backtest_history_service as svc
        with patch.object(svc, "_check_db_encoding", return_value="SQL_ASCII"):
            out = svc._to_jsonb({"msg": "数据预加载完成", "n": 1})
        out.encode("ascii")
        assert json.loads(out)["n"] == 1
