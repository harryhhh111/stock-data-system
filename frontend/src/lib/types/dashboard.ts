import type { Market, Severity } from "./common";

export interface SyncStatus {
  success: number;
  failed: number;
  in_progress: number;
  partial: number;
}

export interface SyncTrend {
  date: string;
  success: number;
  failed: number;
}

export interface Freshness {
  market: Market;
  financial_date: string | null;
  quote_date: string | null;
  financial_stale: boolean;
  quote_stale: boolean;
}

export interface RecentIssue {
  id: number;
  stock_code: string;
  stock_name: string;
  market: Market;
  severity: Severity;
  check_name: string;
  message: string;
  created_at: string;
}

export interface DashboardStats {
  total_stocks: Record<Market, number>;
  sync_status: Record<Market, SyncStatus>;
  sync_trend: Record<Market, SyncTrend[]>;
  validation_issues: {
    errors_24h: number;
    warnings_7d: number;
    total_open: number;
  };
  anomalies_today: number;
  freshness: Freshness[];
  recent_issues: RecentIssue[];
}
