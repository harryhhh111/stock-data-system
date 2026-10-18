import type { HlPositionState } from '@/lib/types/hyperliquid';

export const dash = (value: string | null | undefined) =>
  value == null || value === '' ? '—' : value;

export const signed = (value: string | null | undefined) =>
  value == null || value === '' ? '—' : `${Number(value) >= 0 ? '+' : ''}${value}`;

/** 小数字符串转百分数（如 "0.00001" → "0.00100%"）。 */
export const pctFromFraction = (value: string | null | undefined, dp = 5) =>
  value == null || value === '' ? '—' : `${(Number(value) * 100).toFixed(dp)}%`;

/** 已是百分数数值的字符串（如 "1.50"、"10.77"）。 */
export const pctFromNumber = (value: string | null | undefined, withSign = false) =>
  value == null || value === '' ? '—' : `${withSign ? signed(value) : value}%`;

export function formatTime(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString('zh-CN', { hour12: false });
}

export const POSITION_STATE_LABEL: Record<HlPositionState, string> = {
  flat: '空仓',
  single_leg: '单腿',
  opposite: '两腿反向',
  same_direction: '同向持仓',
  unknown: '未知',
};

export const accountShort = (address: string) =>
  `${address.slice(0, 8)}…${address.slice(-6)}`;

export const NETWORK_LABEL: Record<string, string> = { mainnet: '主网', testnet: '测试网' };

export const dexLabel = (dex: string) => (dex === '' ? '原生' : dex);
