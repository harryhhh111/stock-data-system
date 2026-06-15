"""回测结果序列化测试。"""

from __future__ import annotations

from datetime import date
from unittest.mock import patch

from quant.backtest.types import (
    BacktestResult,
    BenchmarkComparison,
    CompositeDetails,
    CompositeRebalanceRecord,
    PerformanceMetrics,
    Snapshot,
)
from web.wrappers.backtest_wrapper import _serialize


def test_serialize_composite_details():
    """复合策略结果应包含 signals / allocation / 子策略持仓 / NAV / 最终占比。"""
    result = BacktestResult(
        preset_name="commodity_rotation",
        start_date=date(2024, 1, 1),
        end_date=date(2024, 2, 1),
        rebalance_months=1,
        initial_capital=1_000_000,
        final_value=1_000_000,
        metrics=PerformanceMetrics(0, 0, 0, 0, 0, 0, 0, 0),
        rebalance_history=[
            Snapshot(date=date(2024, 1, 1), total_value=1_000_000, positions=[], turnover=0.0),
        ],
        final_holdings=[],
        benchmark_comparison=None,
        composite_details=CompositeDetails(
            records=[
                CompositeRebalanceRecord(
                    date=date(2024, 1, 1),
                    signals={"XAU": "bull", "HG": "bull", "market": "bull"},
                    allocation={"gold": 0.15, "copper": 0.10, "base": 0.75},
                    sub_holdings={"gold": ["A"], "copper": ["B"], "base": ["C", "D"]},
                    sub_navs={"gold": 150_000, "copper": 100_000, "base": 750_000},
                ),
            ],
            final_sub_contributions={"gold": 0.15, "copper": 0.10, "base": 0.75},
            final_sub_allocation={"gold": 0.15, "copper": 0.10, "base": 0.75},
        ),
    )

    with patch("web.wrappers.backtest_wrapper._load_stock_names", return_value={}):
        data = _serialize(result, market="CN_A")

    assert data["preset_type"] == "composite"
    details = data["composite_details"]
    assert details is not None
    assert len(details["records"]) == 1
    rec = details["records"][0]
    assert rec["date"] == "2024-01-01"
    assert rec["signals"] == {"XAU": "bull", "HG": "bull", "market": "bull"}
    assert rec["allocation"] == {"gold": 0.15, "copper": 0.10, "base": 0.75}
    assert rec["sub_holdings"] == {"gold": ["A"], "copper": ["B"], "base": ["C", "D"]}
    assert rec["sub_navs"] == {"gold": 150_000, "copper": 100_000, "base": 750_000}
    assert details["final_sub_contributions"] == {"gold": 0.15, "copper": 0.10, "base": 0.75}
    assert details["final_sub_allocation"] == {"gold": 0.15, "copper": 0.10, "base": 0.75}
