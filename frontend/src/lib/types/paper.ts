import type { Market } from "./common";

export interface PaperAccount {
  account_id: string;
  account_name: string;
  strategy_name: string;
  preset_type: "normal" | "composite";
  market: Market;
  benchmark: string | null;
  initial_capital: number;
  cash: number;
  total_value: number;
  nav: number;
  fee_rate: number;
  slippage_bps: number;
  status: "active" | "paused" | "archived";
  last_valued_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface PaperPosition {
  stock_code: string;
  stock_name?: string;
  market: string;
  sub_strategy: string | null;
  shares: number;
  avg_cost: number;
  last_price: number | null;
  market_value: number;
  weight: number;
}

export interface PaperTrade {
  trade_id: number;
  trade_date: string;
  stock_code: string;
  market: string;
  sub_strategy: string | null;
  side: "buy" | "sell";
  shares: number;
  price: number;
  amount: number;
  fee: number;
  slippage: number;
  reason: string | null;
  signal_snapshot: Record<string, unknown>;
}

export interface PaperNavSnapshot {
  value_date: string;
  cash: number;
  market_value: number;
  total_value: number;
  nav: number;
  benchmark_nav: number | null;
  daily_return: number | null;
  drawdown: number | null;
  position_count: number;
}

export interface PaperStrategyRun {
  run_id: number;
  run_date: string;
  run_type: "valuation" | "rebalance" | "daily_run";
  status: "success" | "failed" | "skipped";
  signals: Record<string, string>;
  allocation: Record<string, number>;
  target_positions: Record<string, unknown>;
  trade_plan: Record<string, unknown>;
  error_message: string | null;
  started_at: string;
  finished_at: string | null;
}

export interface PaperRunResult {
  run_type: "valuation" | "rebalance" | "daily_run";
  run_date: string;
  status: string;
  signals: Record<string, string>;
  allocation: Record<string, number>;
  trades: PaperTrade[];
  nav_before: PaperNavSnapshot | null;
  nav_after: PaperNavSnapshot | null;
}

export interface PaperAccountDetail {
  account: PaperAccount;
  current_holdings: PaperPosition[];
  recent_trades: PaperTrade[];
  nav_history: PaperNavSnapshot[];
  recent_runs: PaperStrategyRun[];
}

export interface CreatePaperAccountParams {
  account_name: string;
  strategy_name: string;
  preset_type?: string;
  market: Market;
  benchmark?: string | null;
  initial_capital?: number;
  fee_rate?: number;
  slippage_bps?: number;
  config?: Record<string, unknown>;
}
