"""US 固定权重复合候选 A/B 回测的单元测试。

对应 docs/quant/US_COMPOSITE_CANDIDATE_AB_BACKTEST_TASK.md §5.1 的测试清单：
配置校验、固定 allocation 与零信号读取、六场景共享预加载与 Portfolio 隔离、
全成本档复利资金语义与守恒、重叠持仓保留与合并、rebalance_months 记录、
跨档表述纪律、缺基准/缺行情显式失败。
"""

from __future__ import annotations

import json
import sys
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest

from quant.backtest import composite as composite_mod
from quant.backtest.composite import (
    _migrate_capital_pool,
    run_composite_backtest,
    validate_research_composite_config,
)
from quant.backtest.portfolio import Portfolio
from quant.backtest.types import Snapshot
from quant.screener.presets import PRESETS
from scripts import run_us_composite_candidate_backtest as cand_mod

D1 = date(2024, 1, 31)
D2 = date(2024, 7, 31)
DATES = [D1, D2]

# A 在第二期价格翻倍，B 持平：用于区分复利（当期 NAV×权重）
# 与旧路径（初始资金×权重）的资金归一语义。
PRICES = {"A": {D1: 10.0, D2: 20.0}, "B": {D1: 10.0, D2: 10.0}}


def _candidate_cfg():
    return cand_mod.build_candidate_config("A")


def _patch_engine_data(monkeypatch, targets_by_sub=None, raise_on_signals=True):
    """隔离 DB/行情/选股：固定目标、固定价格、信号函数直接炸。"""
    targets_by_sub = targets_by_sub or {"quality": ["A"], "growth": ["B"]}
    monkeypatch.setattr(
        composite_mod, "get_nearest_trade_date", lambda d, **kw: d
    )
    monkeypatch.setattr(
        composite_mod, "get_sell_prices_mixed",
        lambda d, codes, bench, mkt: {c: PRICES[c][d] for c in codes},
    )
    monkeypatch.setattr(
        composite_mod, "_select_sub_targets",
        lambda sub, sig, d, mkt, pre, qbd: list(targets_by_sub[sub["name"]]),
    )
    daily = {}
    for code, series in PRICES.items():
        for d, px in series.items():
            daily[(code, d)] = px
    monkeypatch.setattr(
        composite_mod, "load_daily_quotes_for_codes",
        lambda codes, mkt, s, e: dict(daily),
    )
    monkeypatch.setattr(
        composite_mod, "load_benchmark_prices", lambda *a, **kw: {}
    )
    if raise_on_signals:
        def _boom(*a, **kw):
            raise AssertionError("固定权重候选不得读取商品/200MA 信号")
        monkeypatch.setattr(composite_mod, "commodity_signal", _boom)
        monkeypatch.setattr(composite_mod, "check_200ma_signal", _boom)
        monkeypatch.setattr(composite_mod, "_check_all_signals", _boom)


def _run_candidate(slippage_bps=0.0, compounding=True, targets_by_sub=None):
    return run_composite_backtest(
        preset_name="us_candidate_A",
        start=D1,
        end=D2,
        market="US",
        initial_capital=1_000_000,
        benchmark="",
        rebalance_dates=list(DATES),
        preloader=MagicMock(),
        quote_by_date={},
        rebalance_months=6,
        allocation_override={"quality": 0.5, "growth": 0.5},
        fee_rate=0.0,
        slippage_bps=slippage_bps,
        config=_candidate_cfg(),
        compounding_rebalance=compounding,
    )


# ── 5.1.1 候选配置校验 ─────────────────────────────────────

class TestCandidateConfigValidation:
    def test_candidates_registered_presets_and_weights(self):
        for cand in ("A", "B"):
            cfg = cand_mod.build_candidate_config(cand)
            validate_research_composite_config(cfg)
            weights = cand_mod.CANDIDATES[cand]
            assert sum(weights.values()) == pytest.approx(1.0)
            for name in weights:
                assert cand_mod.SUB_PRESET[name] in PRESETS

    def test_duplicate_names_rejected(self):
        cfg = _candidate_cfg()
        cfg["sub_strategies"][1]["name"] = "quality"
        with pytest.raises(ValueError, match="重复"):
            validate_research_composite_config(cfg)

    def test_unknown_preset_rejected(self):
        cfg = _candidate_cfg()
        cfg["sub_strategies"][0]["strategy"] = "no_such_preset"
        with pytest.raises(ValueError, match="未知 preset"):
            validate_research_composite_config(cfg)

    def test_negative_weight_rejected(self):
        cfg = _candidate_cfg()
        cfg["sub_strategies"][0]["weight_bull"] = -0.1
        with pytest.raises(ValueError, match="有限非负数"):
            validate_research_composite_config(cfg)

    def test_weight_sum_not_one_rejected(self):
        cfg = _candidate_cfg()
        cfg["sub_strategies"][0]["weight_bull"] = 0.4
        with pytest.raises(ValueError, match="!= 1.0"):
            validate_research_composite_config(cfg)

    @pytest.mark.parametrize("invalid", [float("nan"), float("inf"), -float("inf")])
    def test_non_finite_weight_rejected(self, invalid):
        cfg = _candidate_cfg()
        cfg["sub_strategies"][0]["weight_bull"] = invalid
        with pytest.raises(ValueError, match="有限非负数"):
            validate_research_composite_config(cfg)


@pytest.mark.parametrize(
    ("override", "match"),
    [
        ({"quality": 1.0}, "missing="),
        ({"quality": 0.5, "growth": 0.5, "extra": 0.0}, "unexpected="),
        ({"quality": float("nan"), "growth": 0.5}, "有限非负数"),
        ({"quality": float("inf"), "growth": 0.5}, "有限非负数"),
        ({"quality": -0.1, "growth": 1.1}, "有限非负数"),
        ({"quality": 0.4, "growth": 0.5}, "权重合计"),
    ],
)
def test_invalid_allocation_override_rejected_before_loading_data(override, match):
    """显式研究入口不能接受漏项、额外项或非有限/非守恒权重。"""
    with pytest.raises(ValueError, match=match):
        run_composite_backtest(
            preset_name="us_candidate_A",
            start=D1,
            end=D2,
            market="US",
            benchmark="",
            config=_candidate_cfg(),
            allocation_override=override,
        )


# ── 5.1.2 固定 allocation + 零信号读取 ──────────────────────

def test_allocation_fixed_and_no_signal_reads(monkeypatch):
    _patch_engine_data(monkeypatch)
    result = _run_candidate()

    override = {"quality": 0.5, "growth": 0.5}
    for rec in result.composite_details.records:
        assert rec.allocation == override  # 每个调仓日逐项相等
        assert rec.signals == {}

    # 初始切片同样来自 override：首期各子组合 NAV = 初始资金 × 权重
    first = result.composite_details.records[0]
    assert first.sub_navs["quality"] == pytest.approx(500_000.0, rel=1e-9)
    assert first.sub_navs["growth"] == pytest.approx(500_000.0, rel=1e-9)


def test_init_sub_portfolios_override_skips_cfg_weights(monkeypatch):
    """override 是唯一权重来源：cfg 的 weight_bull 不得影响初始切片。"""
    monkeypatch.setattr(
        composite_mod, "_check_all_signals",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("不应读信号")),
    )
    cfg = _candidate_cfg()
    for sub in cfg["sub_strategies"]:
        sub["weight_bull"] = 0.99  # 故意破坏 cfg 权重；override 仍应胜出
    cfg["sub_strategies"][1]["weight_bull"] = 0.01
    sub_pfs, _ = composite_mod._init_sub_portfolios(
        cfg, 1000.0, [D1], "US", allocation_override={"quality": 0.5, "growth": 0.5}
    )
    assert sub_pfs["quality"].cash == pytest.approx(500.0)
    assert sub_pfs["growth"].cash == pytest.approx(500.0)


# ── 5.1.3 六场景共享预加载 + Portfolio 隔离 ─────────────────

def test_portfolios_isolated_between_scenarios(monkeypatch):
    _patch_engine_data(monkeypatch)
    created = []
    real_portfolio = composite_mod.Portfolio

    def factory(*a, **kw):
        pf = real_portfolio(*a, **kw)
        created.append(pf)
        return pf

    monkeypatch.setattr(composite_mod, "Portfolio", factory)
    _run_candidate()
    first_run = set(created)
    _run_candidate()
    second_run = set(created) - first_run
    assert first_run and second_run
    assert first_run.isdisjoint(second_run)


def _fake_result(cand: str):
    metrics = SimpleNamespace(
        annualized_return=0.1, max_drawdown=-0.2, sharpe_ratio=1.0,
        volatility=0.15, total_trades=4,
    )
    rec = SimpleNamespace(
        date=D1,
        allocation=dict(cand_mod.CANDIDATES[cand]),
        sub_holdings={n: ["A", "B"] for n in cand_mod.CANDIDATES[cand]},
        sub_navs={n: 500_000.0 for n in cand_mod.CANDIDATES[cand]},
    )
    return SimpleNamespace(
        preset_name=f"us_candidate_{cand}",
        start_date=D1, end_date=D2, rebalance_months=6,
        initial_capital=1_000_000, total_costs=0.0,
        metrics=metrics, benchmark_comparison=None,
        composite_details=SimpleNamespace(
            records=[rec],
            final_sub_contributions={n: 0.5 for n in cand_mod.CANDIDATES[cand]},
            final_sub_allocation=dict(cand_mod.CANDIDATES[cand]),
        ),
        rebalance_history=[Snapshot(
            date=D1, total_value=1_000_000.0, positions=["A", "B"],
            turnover=1.0, cash=0.0, holdings={"A": 1.0, "B": 1.0}, costs={},
        )],
        strategy_daily_nav={}, benchmark_daily_nav={},
        final_holdings=["A", "B"],
    )


def test_script_reuses_shared_preload_for_six_scenarios(monkeypatch, tmp_path):
    """main() 全流程（mock 数据层）：同一预加载对象传入全部六场景。"""
    parent_manifest = {
        "run_id": cand_mod.PARENT_RUN_ID,
        "comparison_key": cand_mod.PARENT_COMPARISON_KEY,
        "parameters": {
            "start": "2021-06-01", "end": "2026-07-16",
            "rebalance_months": 6, "benchmark": "SPY",
            "initial_capital": 1_000_000,
            "rebalance_dates": ["2021-06-30", "2021-12-31"],
        },
        "inputs": {"pit": {"min_report_date": "2018-01-01",
                           "max_filed_date": "2026-07-16"}},
    }
    monkeypatch.setattr(cand_mod, "_load_parent_manifest", lambda: parent_manifest)
    monkeypatch.setattr(cand_mod, "_load_parent_baseline_rows", lambda: {})
    monkeypatch.setattr(cand_mod, "_git_sha", lambda: "deadbeef")
    monkeypatch.setattr(cand_mod, "_input_fingerprints", lambda *a: {"pit": {}})
    monkeypatch.setattr(cand_mod, "_preflight_quotes", lambda *a: None)

    preloader = MagicMock()
    monkeypatch.setattr(cand_mod, "PITPreloader", lambda *a, **kw: preloader)
    monkeypatch.setattr(
        cand_mod, "Connection",
        lambda: MagicMock(__enter__=lambda s: None, __exit__=lambda *a: None),
    )
    quotes = {date(2021, 6, 30): pd.DataFrame({"close": [1.0]}),
              date(2021, 12, 31): pd.DataFrame({"close": [1.0]})}
    monkeypatch.setattr(cand_mod, "batch_query_quote", lambda *a: quotes)

    calls = []
    monkeypatch.setattr(
        cand_mod, "run_composite_backtest",
        lambda **kw: (calls.append(kw), _fake_result(kw["preset_name"][-1]))[1],
    )
    monkeypatch.setattr(sys, "argv", [
        "prog", "--run-id", "test_run",
        "--output", str(tmp_path / "build" / "test_run"),
        "--evidence-root", str(tmp_path / "evidence"),
    ])
    cand_mod.main()

    assert len(calls) == 6
    for kw in calls:
        assert kw["preloader"] is preloader           # 同一预加载对象复用
        assert kw["quote_by_date"] is quotes
        assert kw["rebalance_dates"] == [date(2021, 6, 30), date(2021, 12, 31)]
        assert kw["compounding_rebalance"] is True
        assert kw["rebalance_months"] == 6
        assert kw["fee_rate"] == 0.0
    # 权重与候选表逐项相等
    by_label = {kw["preset_name"]: kw["allocation_override"] for kw in calls}
    assert by_label["us_candidate_A"] == {"quality": 0.5, "growth": 0.5}
    assert by_label["us_candidate_B"] == {
        "quality": 0.5, "growth": 0.35, "momentum": 0.15
    }

    out = tmp_path / "build" / "test_run"
    for name in ("manifest.json", "summary.csv", "summary.md",
                 "rebalance_records.csv", "sub_strategy_records.csv",
                 "overlap_by_rebalance.csv"):
        assert (out / name).exists(), name
        assert (tmp_path / "evidence" / "test_run" / name).exists(), name
    assert (tmp_path / "evidence" / "test_run" / "SHA256SUMS").exists()
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["parameters"]["compounding_rebalance"] is True
    assert manifest["parameters"]["parent_baseline"]["run_id"] == cand_mod.PARENT_RUN_ID


def test_input_drift_requires_contemporaneous_fcf_reference():
    """漂移后不得继续引用父归档数值；三档当次基线必须齐全。"""
    parent = {("fcf_roe_value", bps): {"annualized_return": 0.1}
              for bps in cand_mod.BPS_TIERS}
    current = {("fcf_roe_value", bps): {"annualized_return": 0.2}
               for bps in cand_mod.BPS_TIERS}

    selected, source = cand_mod._reference_rows_for_run(
        parent, {"pit.watermark": {"parent": "old", "ours": "new"}}, current
    )
    assert selected is current
    assert source == "contemporaneous_fcf_roe_value"

    with pytest.raises(ValueError, match="必须提供"):
        cand_mod._reference_rows_for_run(parent, {"pit.watermark": {}}, None)
    with pytest.raises(ValueError, match="缺少成本档"):
        cand_mod._reference_rows_for_run(
            parent, {"pit.watermark": {}}, {("fcf_roe_value", 0.0): {}}
        )


# ── 5.1.4 全成本档复利语义与守恒 ─────────────────────────────

def test_zero_cost_compounding_targets_current_nav(monkeypatch):
    """0 bps 也走共享资金池：第二期目标 = 当期总 NAV × 权重（非初始切片）。"""
    _patch_engine_data(monkeypatch)
    monkeypatch.setattr(
        composite_mod, "_normalize_sub_portfolio",
        lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("复利模式不得走固定初始切片缩放")
        ),
    )
    result = _run_candidate(slippage_bps=0.0)

    # 第一期后 A 翻倍：quality 100 万 + growth 50 万 = 150 万
    second = result.composite_details.records[1]
    assert second.sub_navs["quality"] == pytest.approx(750_000.0, rel=1e-9)
    assert second.sub_navs["growth"] == pytest.approx(750_000.0, rel=1e-9)

    # 守恒：零成本下各期资金和 = 当期市值，无凭空增减
    assert result.total_costs == 0.0
    total = sum(second.sub_navs.values())
    assert total == pytest.approx(1_500_000.0, rel=1e-9)


def test_migrate_capital_pool_zero_rate_exact_conservation(monkeypatch):
    """资金池迁移在 0 费率下精确守恒，目标 = 当期总净值 × 权重。"""
    a = Portfolio(500.0)
    b = Portfolio(500.0)
    a.rebalance(D1, ["A"], {"A": 10.0}, {})
    b.rebalance(D1, ["B"], {"B": 20.0}, {})
    px = {"A": 10.0, "B": 20.0}
    monkeypatch.setattr(
        composite_mod, "get_sell_prices_mixed",
        lambda d, codes, bench, mkt: {c: px[c] for c in codes},
    )
    cfg = {
        "description": "t", "type": "composite", "rebalance": "monthly",
        "benchmark": None,
        "sub_strategies": [{"name": "a", "residual": False},
                           {"name": "b", "residual": False}],
    }
    total_before = a.nav(px) + b.nav(px)
    targets = _migrate_capital_pool(
        cfg, {"a": a, "b": b}, {"a": 0.25, "b": 0.75}, D2, None, "US"
    )
    assert targets["a"] == pytest.approx(total_before * 0.25)
    assert targets["b"] == pytest.approx(total_before * 0.75)
    assert a.total_costs == 0.0 and b.total_costs == 0.0
    assert a.nav(px) + b.nav(px) == pytest.approx(total_before, abs=1e-9)


def test_positive_costs_nonnegative_and_grow_with_rate(monkeypatch):
    """10/20 bps 总成本非负、随费率单调，且来自真实买卖记录。"""
    _patch_engine_data(monkeypatch)
    r10 = _run_candidate(slippage_bps=10.0)
    _patch_engine_data(monkeypatch)
    r20 = _run_candidate(slippage_bps=20.0)

    assert r10.total_costs > 0
    assert r20.total_costs > r10.total_costs
    # 成本必须能由真实交易解释：有成交才有成本
    assert r10.metrics.total_trades > 0
    # 绝不按全清再建收费的上界（rate × 2 × 每次全额换手）
    assert r10.total_costs < 0.002 * 2 * 1_500_000.0


# ── 5.1.5 重叠持仓：子策略保留 + 账户级合并 ──────────────────

def test_overlapping_stock_kept_per_sub_and_merged(monkeypatch):
    _patch_engine_data(
        monkeypatch, targets_by_sub={"quality": ["A"], "growth": ["A"]}
    )
    result = _run_candidate()

    rec = result.composite_details.records[0]
    assert rec.sub_holdings["quality"] == ["A"]   # 子策略明细各自保留
    assert rec.sub_holdings["growth"] == ["A"]
    assert result.final_holdings == ["A"]          # 账户级合并后不重复

    rows = cand_mod._overlap_records({("A", 0.0): result})
    assert rows[0]["pair"] == "growth/quality"
    assert rows[0]["overlap_ratio"] == pytest.approx(1.0)
    assert rows[0]["account_unique_count"] == 1

    sub_rows = cand_mod._sub_strategy_records({("A", 0.0): result})
    assert {r["sub_strategy"] for r in sub_rows} == {"quality", "growth"}
    for r in sub_rows:
        assert r["holdings_json"] == '["A"]'
        assert r["nav_weight"] == pytest.approx(0.5)


# ── 5.1.6 rebalance_months 原样写入 ──────────────────────────

def test_rebalance_months_recorded_as_six(monkeypatch):
    _patch_engine_data(monkeypatch)
    result = _run_candidate()
    assert result.rebalance_months == 6


# ── 5.1.7 跨档表述纪律 ─────────────────────────────────────

def test_summary_md_marks_cross_tier_as_total_implementation_impact(tmp_path):
    rows = [{
        "candidate": "A", "single_side_cost_bps": 0.0,
        "start_date": "2021-06-30", "end_date": "2026-07-16",
        "rebalance_months": 6, "annualized_return": 0.1,
        "max_drawdown": -0.2, "sharpe_ratio": 1.0, "volatility": 0.15,
        "annualized_alpha": 0.01, "information_ratio": 0.3,
        "total_costs": 0.0, "total_trades": 4,
        "final_sub_contributions": json.dumps({"quality": 0.5, "growth": 0.5}),
    }]
    out = tmp_path / "summary.md"
    cand_mod._write_summary_md(out, "rid", rows, {}, {})
    text = out.read_text(encoding="utf-8")
    assert "成本的总实施影响" in text
    assert "纯费率归因" in text  # 以否定句形式出现，禁止误称


# ── 5.1.8 缺基准/缺行情显式失败 ──────────────────────────────

def test_preflight_fails_on_missing_rebalance_quotes(monkeypatch):
    monkeypatch.setattr(
        cand_mod, "load_benchmark_prices", lambda *a: {D1: 100.0}
    )
    with pytest.raises(SystemExit, match="调仓日行情缺失"):
        cand_mod._preflight_quotes(
            {D1: pd.DataFrame({"close": [1.0]}), D2: pd.DataFrame()},
            DATES, "SPY", D1, D2,
        )


def test_preflight_fails_on_missing_benchmark(monkeypatch):
    monkeypatch.setattr(cand_mod, "load_benchmark_prices", lambda *a: {})
    with pytest.raises(SystemExit, match="无数据"):
        cand_mod._preflight_quotes(
            {d: pd.DataFrame({"close": [1.0]}) for d in DATES},
            DATES, "SPY", D1, D2,
        )


def test_engine_fails_on_missing_benchmark_prices(monkeypatch):
    """引擎层兜底：基准行情缺失时显式 ValueError，不产出缺基准报告。"""
    _patch_engine_data(monkeypatch)
    result_benchless = None
    try:
        result_benchless = run_composite_backtest(
            preset_name="us_candidate_A",
            start=D1, end=D2, market="US", initial_capital=1_000_000,
            benchmark="SPY",
            rebalance_dates=list(DATES), preloader=MagicMock(), quote_by_date={},
            rebalance_months=6,
            allocation_override={"quality": 0.5, "growth": 0.5},
            config=_candidate_cfg(), compounding_rebalance=True,
        )
    except ValueError as exc:
        assert "SPY" in str(exc)
    assert result_benchless is None
