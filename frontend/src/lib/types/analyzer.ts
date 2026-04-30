import type { Market } from "./common";

export interface StockSearchResult {
  stock_code: string;
  stock_name: string;
  market: Market;
  industry: string | null;
}

export interface StockInfo {
  stock_code: string;
  stock_name: string;
  market: Market;
  industry: string | null;
  list_date: string | null;
  close: number | null;
  market_cap: number | null;
  pe_ttm: number | null;
  pb: number | null;
  fcf_yield: number | null;
  revenue_ttm: number | null;
  net_profit_ttm: number | null;
  cfo_ttm: number | null;
}

export interface AnalysisSection<T = Record<string, unknown>> {
  rating: number | null;
  star: string;
  verdict: string;
  details: T;
}

export interface ProfitabilityDetailsItem {
  year: number;
  revenue: number | null;
  net_profit: number | null;
  gross_margin: number | null;
  net_margin: number | null;
  roe: number | null;
  revenue_yoy: number | null;
  net_profit_yoy: number | null;
}

export interface DebtTrendItem {
  year: number;
  debt_ratio: number | null;
}

export interface HealthDetails {
  debt_ratio: number | null;
  current_ratio: number | null;
  quick_ratio: number | null;
  debt_trend: DebtTrendItem[];
  total_assets: number | null;
  total_liab: number | null;
  total_equity: number | null;
}

export interface FCFYearItem {
  year: number;
  fcf: number | null;
  cfo: number | null;
  net_profit: number | null;
}

export interface CashflowDetails {
  source: string;
  cfo: number | null;
  capex: number | null;
  fcf: number | null;
  revenue: number | null;
  net_profit: number | null;
  cfo_quality: number | null;
  capex_intensity: number | null;
  fcf_years: FCFYearItem[];
  ttm_report_date: string | null;
  stale_warning: string | null;
}

export interface ValuationDetails {
  pe: number | null;
  pb: number | null;
  fcf_yield: number | null;
  market_cap: number | null;
  close: number | null;
  peer_count: number;
  median_pe: number | null;
  median_pb: number | null;
  median_fcf_yield: number | null;
  pe_vs: string | null;
  pb_vs: string | null;
  fcf_yield_vs: string | null;
}

export interface OverallAssessment {
  rating: number | null;
  star: string;
  verdict: string;
  risks: string[];
}

export interface AnalysisReport {
  stock: StockInfo;
  sections: {
    profitability: AnalysisSection<ProfitabilityDetailsItem[]>;
    health: AnalysisSection<HealthDetails>;
    cashflow: AnalysisSection<CashflowDetails>;
    valuation: AnalysisSection<ValuationDetails>;
  };
  overall: OverallAssessment;
}