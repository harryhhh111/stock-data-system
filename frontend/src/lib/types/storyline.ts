import type { Market } from "./common";

export interface StorylineStock {
  stock_code: string;
  stock_name: string;
  market: Market;
  industry: string | null;
  list_date: string | null;
  currency: string | null;
}

export interface StorylineReport {
  report_date: string;
  report_type: "quarterly" | "semi" | "annual";
  notice_date: string | null;
  revenue: number | null;
  revenue_yoy: number | null;
  net_profit: number | null;
  net_profit_yoy: number | null;
  gross_margin: number | null;
  eps_basic: number | null;
  net_profit_excl: number | null;
  roe: number | null;
  total_assets: number | null;
  total_liab: number | null;
  debt_ratio: number | null;
  cfo_net: number | null;
}

export interface StorylineEvent {
  id: number;
  event_date: string;
  event_type: string;
  title: string;
  summary: string | null;
  source_url: string | null;
}

export interface StorylineTimeline {
  stock: StorylineStock;
  reports: StorylineReport[];
  events: StorylineEvent[];
  /** 每股分红：{年份: 当年合计每股分红} */
  dividends: Record<string, number>;
  /** 分业务收入构成（最近若干期，倒序） */
  segments: StorylinePeriodSegments[];
}

export interface StorylineSegment {
  item_name: string;
  revenue: number | null;
  revenue_ratio: number | null;
  gross_margin: number | null;
}

export interface StorylinePeriodSegments {
  report_date: string;
  source: string; // 'em_f10' | 'llm:*'
  dimensions: Partial<Record<"product" | "industry" | "region", StorylineSegment[]>>;
}

export interface KlinePoint {
  date: string;
  open: number | null;
  close: number | null;
  low: number | null;
  high: number | null;
  volume: number | null;
}
