import type { Market } from "./common";

export interface FixedRule {
  rule: string;
  description: string;
}

export interface AppliedFilters {
  market: string;
  market_cap_min: number;
  fcf_yield_min: number;
  roe_min: number;
  roe_consecutive_years: number;
  top_n: number;
}

export interface StrategyStock {
  score: number;
  score_rank: number;
  stock_code: string;
  stock_name: string;
  market: Market;
  industry: string;
  market_cap: number;
  pe_ttm: number | null;
  pb: number | null;
  fcf_yield: number | null;
  roe: number | null;
  roe_1y_ago: number | null;
  roe_2y_ago: number | null;
  gross_margin: number | null;
  net_margin: number | null;
  debt_ratio: number | null;
  ttm_report_date: string | null;
  ttm_notice_date: string | null;
  stale_warning: boolean;
  currency: string;
  factor_ranks: Record<string, number>;
}

export interface FcfRoeParams {
  market: Market;
  market_cap_min?: number;
  fcf_yield_min?: number;
  roe_min?: number;
  top_n?: number;
}

export interface FcfRoeResult {
  total_before_filter: number;
  total_after_filter: number;
  total: number;
  results: StrategyStock[];
  fixed_rules: FixedRule[];
  applied_filters: AppliedFilters;
  weights: Record<string, number>;
  currency: string;
}
