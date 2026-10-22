// 每日资讯（公司新闻层）类型，合同 docs/WATCHLIST_DAILY_NEWS_MVP_PLAN.md §4
import type { AnnouncementSourceState } from './watchlist';

export interface NewsEntry {
  title: string;
  url: string;
  source_kind: 'sec' | 'ir' | 'media';
  platform: string | null;
  media_source: string | null;
  form_type: string;
  content_nature: string | null;   // 媒体 v1 一律 'unclassified'
  stock_code: string;
  relation_reason: string | null;
  excerpt: string | null;
  published_at: string | null;
  published_precision: string;
  published_raw: string;
  source_timezone: string | null;
  time_unknown: boolean;
  date_only: boolean;
  first_observed_at: string | null;
  is_backfill: boolean;
  bj_day: string | null;           // 北京时间日期（分组键）
  fold_count: number;              // 折叠组条目数（1 = 未折叠）
  fold_sources: { name: string; url: string; published_at: string | null; platform: string | null }[];
}

export interface NewsFeed {
  initialized: boolean;
  message?: string;
  announcements_enabled: boolean;
  days: string[];
  kind: string;
  stock: string;
  timezone: string;
  entries: NewsEntry[];
  sources: (AnnouncementSourceState & { kind: string })[];
}

export interface NewsPinned {
  initialized: boolean;
  message?: string;
  announcements_enabled: boolean;
  as_of: string;
  timezone: string;
  official: NewsEntry[];
  media_groups: NewsEntry[];
}
