import type { Market, Severity } from "./common";

export interface MarketSeverityCount {
  market: Market;
  error: number;
  warning: number;
  info: number;
}

export interface QualitySummary {
  by_severity: { severity: Severity; count: number }[];
  by_market: MarketSeverityCount[];
  by_check: { check_name: string; label: string; severity: Severity; count: number }[];
  last_check_at: string | null;
}

export interface QualityIssue {
  id: number;
  batch_id: string;
  stock_code: string;
  stock_name: string;
  market: Market;
  report_date: string;
  check_name: string;
  severity: Severity;
  field_name: string | null;
  actual_value: string | null;
  expected_value: string | null;
  message: string;
  suggestion: string | null;
  created_at: string;
}