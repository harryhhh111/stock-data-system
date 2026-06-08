import type { Market } from "./common";

export interface BacktestParams {
  preset_name: string;
  market: Market;
  start: string;
  end?: string;
  months: number;
  top_n?: number;
  initial_capital: number;
  benchmark?: string | null;
  timing?: boolean;
}

export interface PerformanceMetrics {
  total_return: number;
  annualized_return: number;
  max_drawdown: number;
  sharpe_ratio: number;
  volatility: number;
  num_rebalances: number;
  avg_holding_count: number;
  total_trades: number;
}

export interface BacktestSnapshot {
  date: string;
  total_value: number;
  positions: string[];
  turnover: number;
}

export interface BenchmarkComparison {
  benchmark_ticker: string;
  benchmark_description: string;
  benchmark_total_return: number;
  benchmark_annualized: number;
  benchmark_max_drawdown: number;
  excess_return: number;
  annualized_alpha: number;
  information_ratio: number;
  tracking_error: number;
  beta: number;
  correlation: number;
}

export interface BacktestResult {
  preset_name: string;
  start_date: string;
  end_date: string;
  rebalance_months: number;
  initial_capital: number;
  final_value: number;
  metrics: PerformanceMetrics;
  rebalance_history: BacktestSnapshot[];
  final_holdings: string[];
  benchmark_comparison?: BenchmarkComparison | null;
  strategy_daily_nav?: Record<string, number> | null;
  benchmark_daily_nav?: Record<string, number> | null;
  stock_names?: Record<string, string>;
}

export type BacktestTaskStatus = "CREATED" | "RUNNING" | "DONE" | "FAILED";

export interface BacktestTask {
  task_id: string;
  status: BacktestTaskStatus;
  progress_pct: number;
  progress_label: string;
  result?: BacktestResult;
  error?: string;
}

export interface BacktestPreset {
  name: string;
  description: string;
}

/** 换手详情：每期调仓的买入/卖出/持有股票列表 */
export interface TurnoverDetail {
  sold: string[];
  bought: string[];
  held: string[];
}
