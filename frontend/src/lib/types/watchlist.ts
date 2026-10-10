import type { Market } from './common';
export interface WatchItem {
  id: number; market: Market; stock_code: string; stock_name: string;
  group_name: string; note: string; research_url: string; sort_order: number;
  created_at: string; updated_at: string;
}
export interface WatchQuote {
  stock_code: string; close: number | null; change_pct: number | null;
  quote_date: string | null; status: string; warning: string; stale: boolean;
  trend: { date: string; close: number | null }[];
}
export interface QuoteBatch { market: Market; currency: string; fetched_at: string; quotes: WatchQuote[] }
