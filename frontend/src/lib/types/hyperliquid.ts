export type HlBlockStatus = 'ok' | 'partial' | 'unavailable';

export interface HlBlock {
  status: HlBlockStatus;
  queried_at: string;
  diagnostics: string[];
}

export interface HlPosition {
  coin: string;
  side: 'long' | 'short';
  size: string;
  mark_px: string | null;
  unrealized_pnl: string | null;
  notional: string | null;
}

export interface HlPositionsBlock extends HlBlock {
  equity: string | null;
  positions: HlPosition[];
  notional_ratio: string | null;
  ratio_drift: string | null;
  action_state: string | null;
  open_orders: number | null;
}

export interface HlFundingPayment { time: string; coin: string; usdc: string; rate: string }

export interface HlFundingBlock extends HlBlock {
  hourly_rates: Record<string, string | null>;
  annualized: Record<string, string | null>;
  daily_estimate: { direction: 'receive' | 'pay'; amount: string } | null;
  window_start: string | null;
  window_end: string | null;
  window_complete: boolean | null;
  actual_48h_net: string | null;
  recent_payments: HlFundingPayment[];
}

export interface HlRatioBlock extends HlBlock {
  current: string | null;
  ma30: string | null;
  vs_ma30_pct: string | null;
  as_of_date: string | null;
}

export interface HlUnlockAnchor {
  date: string;
  days_ahead: number;
  label: string;
  source_url: string;
  verified_utc: string;
}

export interface HlUnlocksBlock extends HlBlock {
  anchors: HlUnlockAnchor[];
  note: string;
}

export interface HlAnnouncement {
  kind: 'general' | 'api';
  text: string;
  published_at: string | null;
  url: string;
  source: string;
}

export interface HlAnnouncementsBlock extends HlBlock {
  items: HlAnnouncement[];
}

export interface HlSnapshot {
  schema_version: number;
  network: string;
  account_address: string;
  queried_at: string;
  blocks: {
    positions: HlPositionsBlock;
    funding: HlFundingBlock;
    ratio: HlRatioBlock;
    unlocks: HlUnlocksBlock;
    announcements: HlAnnouncementsBlock;
  };
}
