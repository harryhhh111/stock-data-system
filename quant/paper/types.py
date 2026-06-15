"""模拟盘类型定义 — Pydantic 模型，供 API 返回用。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class CreateAccountParams(BaseModel):
    account_name: str
    strategy_name: str
    preset_type: str = "composite"
    market: str = "CN_A"
    benchmark: str | None = None
    initial_capital: float = 1_000_000
    fee_rate: float = 0.0
    slippage_bps: float = 0.0
    config: dict[str, Any] = Field(default_factory=dict)


class PaperAccount(BaseModel):
    account_id: str
    account_name: str
    strategy_name: str
    preset_type: str
    market: str
    benchmark: str | None
    initial_capital: float
    cash: float
    total_value: float
    nav: float
    fee_rate: float
    slippage_bps: float
    status: str
    last_valued_at: str | None
    created_at: str
    updated_at: str


class PaperPosition(BaseModel):
    stock_code: str
    market: str
    sub_strategy: str | None
    shares: float
    avg_cost: float
    last_price: float | None
    market_value: float
    weight: float


class PaperTrade(BaseModel):
    trade_id: int
    trade_date: str
    stock_code: str
    market: str
    sub_strategy: str | None
    side: str
    shares: float
    price: float
    amount: float
    fee: float
    slippage: float
    reason: str | None
    signal_snapshot: dict[str, Any]


class PaperNavSnapshot(BaseModel):
    value_date: str
    cash: float
    market_value: float
    total_value: float
    nav: float
    benchmark_nav: float | None
    daily_return: float | None
    drawdown: float | None
    position_count: int


class PaperStrategyRun(BaseModel):
    run_id: int
    run_date: str
    run_type: str
    status: str
    signals: dict[str, Any]
    allocation: dict[str, Any]
    target_positions: dict[str, Any]
    trade_plan: dict[str, Any]
    error_message: str | None
    started_at: str
    finished_at: str | None


class PaperRunResult(BaseModel):
    run_type: str
    run_date: str
    status: str
    signals: dict[str, str]
    allocation: dict[str, float]
    trades: list[PaperTrade]
    nav_before: PaperNavSnapshot | None
    nav_after: PaperNavSnapshot


class PaperAccountDetail(BaseModel):
    account: PaperAccount
    current_holdings: list[PaperPosition]
    recent_trades: list[PaperTrade]
    nav_history: list[PaperNavSnapshot]
    recent_runs: list[PaperStrategyRun]
