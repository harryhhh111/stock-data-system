import type { Market } from './common';
export interface ResearchDiagnostic {
  status: 'matched' | 'no_entry'; commit_sha: string; entity_id: string | null;
  missing_sections: string[];
  sections: { kind: string; path: string; line: number; heading_path: string[]; truncated: boolean; total_chars: number }[];
}
export interface WatchSuggestion {
  group: string; note: string; source: 'research_doc' | 'generic'; docs: string[];
  research?: ResearchDiagnostic;
}
export interface WatchItem {
  id: number; market: Market; stock_code: string; stock_name: string;
  group_name: string; note: string; research_url: string; sort_order: number;
  created_at: string; updated_at: string;
}
export interface WatchQuote {
  stock_code: string; close: number | null; change_pct: number | null;
  quote_date: string | null; status: string; warning: string; stale: boolean;
  previous_quote_date?: string | null;
  trend: { date: string; close: number | null }[];
}
export interface QuoteBatch { market: Market; currency: string; fetched_at: string; quotes: WatchQuote[] }
