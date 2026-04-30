import type { Market } from "./common";

export interface FactorWeight {
  weight: number;
  ascending: boolean;
}

export interface FilterConfig {
  market_cap_min?: number | null;
  exclude_st?: boolean;
  exclude_industries?: string[];
  pe_ttm_positive?: boolean;
  pe_ttm_max?: number | null;
  pb_max?: number | null;
  min_days_since_list?: number | null;
  fcf_yield_min?: number | null;
  debt_ratio_max?: number | null;
  gross_margin_min?: number | null;
  net_margin_min?: number | null;
  dividend_yield_min?: number | null;
}

export interface Preset {
  name: string;
  description: string;
  filters: FilterConfig;
  weights: Record<string, FactorWeight>;
  top_n: number;
}

export interface ScreenerStock {
  score: number;
  score_rank: number;
  stock_code: string;
  stock_name: string;
  market: Market;
  industry: string;
  market_cap: number;
  pe_ttm: number | null;
  pb: number | null;
  dividend_yield: number | null;
  fcf_yield: number | null;
  roe: number | null;
  gross_margin: number | null;
  net_margin: number | null;
  debt_ratio: number | null;
  factor_ranks: Record<string, number>;
}

export interface ScreenerResult {
  total_before_filter: number;
  total_after_filter: number;
  total: number;
  results: ScreenerStock[];
  preset: string;
  market: string;
}