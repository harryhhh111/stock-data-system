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

/** 创建回测任务时使用的参数，区分普通策略与复合策略 */
export type BacktestRunParams =
  | {
      preset_name: string;
      preset_type: "normal";
      market: Market;
      start: string;
      end?: string;
      months: number;
      top_n?: number | null;
      initial_capital: number;
      benchmark?: string | null;
      timing?: boolean;
    }
  | {
      preset_name: string;
      preset_type: "composite";
      market: "CN_A";
      start: string;
      end?: string;
      initial_capital: number;
      benchmark?: string | null;
    };

export type BacktestRunStatus = "CREATED" | "RUNNING" | "DONE" | "FAILED" | "CANCELLED";

export interface PerformanceMetrics {
  total_return: number;
  annualized_return: number;
  max_drawdown: number;
  sharpe_ratio: number;
  volatility: number;
  num_rebalances: number;
  avg_holding_count: number;
  total_trades: number;
  excess_return?: number | null;
  annualized_alpha?: number | null;
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

export interface CompositeRebalanceRecord {
  date: string;
  signals: Record<string, string>;
  allocation: Record<string, number>;
  sub_holdings: Record<string, string[]>;
  sub_navs: Record<string, number>;
}

export interface CompositeDetails {
  records: CompositeRebalanceRecord[];
  final_sub_contributions: Record<string, number>;
  final_sub_allocation: Record<string, number>;
}

export interface BacktestResult {
  preset_name: string;
  preset_type?: "normal" | "composite";
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
  composite_details?: CompositeDetails | null;
}

export interface BacktestTask {
  task_id: string;
  status: BacktestRunStatus;
  progress_pct: number;
  progress_label: string;
  result?: BacktestResult;
  error?: string;
}

export interface BacktestPreset {
  name: string;
  description: string;
  type: "normal" | "composite";
  rebalance?: string | null;
  benchmark?: string | null;
  sub_strategies?: CompositeSubStrategy[];
}

export interface CompositeSubStrategy {
  name: string;
  strategy: string;
  commodity: string;
  weight_bull: number;
  weight_bear: number;
  weight_neutral: number;
  top_n_override?: number | null;
  market_scope: string;
  residual: boolean;
}

/** 历史回测摘要 */
export interface BacktestRunSummary {
  task_id: string;
  run_id: string;
  status: BacktestRunStatus;
  created_at: string;
  started_at?: string | null;
  completed_at?: string | null;
  elapsed_ms?: number;
  params: BacktestRunParams;
  preset_name: string;
  preset_type: "normal" | "composite";
  market: Market;
  start_month: string;
  end_month?: string | null;
  rebalance_months?: number | null;
  top_n?: number | null;
  initial_capital: number;
  benchmark?: string | null;
  timing?: boolean;
  progress_pct: number;
  progress_label?: string;
  error?: string;
  metrics?: PerformanceMetrics | null;
}

/** 历史回测详情（包含完整 result） */
export interface BacktestRunDetail extends BacktestRunSummary {
  result?: BacktestResult | null;
}

/** 回测历史列表响应 */
export interface BacktestRunsResponse {
  items: BacktestRunSummary[];
  total: number;
  limit: number;
  offset: number;
}

/** 换手详情：每期调仓的买入/卖出/持有股票列表 */
export interface TurnoverDetail {
  sold: string[];
  bought: string[];
  held: string[];
}
