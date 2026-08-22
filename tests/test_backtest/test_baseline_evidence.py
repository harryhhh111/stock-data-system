"""US PIT baseline 证据格式的纯函数测试。"""

from datetime import date

from quant.backtest.baseline_evidence import (
    comparison_key,
    rebalance_records,
    sha256_rows,
    sha256_value,
)
from quant.backtest.types import BacktestResult, PerformanceMetrics, Snapshot


def test_hash_is_stable_and_input_sensitive():
    first = sha256_rows([(date(2024, 1, 1), "A", 1.0), (date(2024, 1, 2), "B", 2.0)])
    second = sha256_rows([(date(2024, 1, 1), "A", 1.0), (date(2024, 1, 2), "B", 2.0)])
    changed = sha256_rows([(date(2024, 1, 1), "A", 1.1), (date(2024, 1, 2), "B", 2.0)])
    assert first == second
    assert first != changed


def test_comparison_key_requires_same_parameters_and_inputs():
    params = {"market": "US", "months": 6}
    inputs = {"pit": {"watermark": "v1"}, "quotes": {"sha256": "a"}}
    key = comparison_key(params, inputs)
    assert key == comparison_key(dict(params), dict(inputs))
    assert key != comparison_key(params, {"pit": {"watermark": "v2"}})
    assert sha256_value(params) != sha256_value({"market": "US", "months": 3})


def test_rebalance_records_sort_holdings_and_carry_costs():
    result = BacktestResult(
        preset_name="fcf_roe_value",
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 31),
        rebalance_months=6,
        initial_capital=1_000_000,
        final_value=1_010_000,
        metrics=PerformanceMetrics(0.01, 0.01, 0.01, 1, 0.1, 1, 2, 2),
        rebalance_history=[Snapshot(
            date=date(2024, 1, 31), total_value=1_000_000, positions=["B", "A"],
            turnover=0.0, cash=0.0, holdings={"B": 2.0, "A": 1.0},
        )],
        final_holdings=["A", "B"],
        total_costs=123.45,
    )
    record = rebalance_records([(10.0, result)])[0]
    assert record["strategy"] == "fcf_roe_value"
    assert record["single_side_cost_bps"] == 10.0
    assert record["cumulative_costs"] == 123.45
    assert record["holdings_json"] == '{"A":"1","B":"2"}'
    assert record["holdings_sha256"] == sha256_value({"A": 1.0, "B": 2.0})
