import type { Market } from "./common";

export interface SyncStatusByMarket {
  market: Market;
  total_stocks: number;
  success: number;
  failed: number;
  in_progress: number;
  partial: number;
  last_sync_time: string | null;
  last_report_date: string | null;
  report_date_latest?: string | null;
  report_coverage_pct?: number;
}

export interface SyncProgressEntry {
  stock_code: string;
  stock_name: string;
  market: Market;
  status: "success" | "failed" | "partial" | "in_progress";
  tables_synced: string[];
  last_sync_time: string | null;
  last_report_date: string | null;
  error_detail: string | null;
}

export interface SyncLogEntry {
  id: number;
  data_type: string;
  market: string;
  status: string;
  started_at: string;
  finished_at: string | null;
  success_count: number;
  fail_count: number;
  elapsed_seconds: number | null;
  error_detail: string | null;
}