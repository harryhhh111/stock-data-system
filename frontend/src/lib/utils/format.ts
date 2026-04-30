import { format, formatDistanceToNow } from "date-fns";
import { zhCN } from "date-fns/locale";

const CN_NUM = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 });
const CN_PCT = new Intl.NumberFormat("zh-CN", { style: "percent", maximumFractionDigits: 1 });

/** 数字格式化（千分位，最多2位小数） */
export function fmtNum(n: number | null | undefined): string {
  if (n == null) return "-";
  return CN_NUM.format(n);
}

/** 金额格式化（亿） */
export function fmtYi(n: number | null | undefined): string {
  if (n == null) return "-";
  const yi = n / 1e8;
  if (yi >= 1) return `${yi.toFixed(2)} 亿`;
  const wan = n / 1e4;
  return `${wan.toFixed(0)} 万`;
}

/** 市值格式化 */
export function fmtMcap(n: number | null | undefined): string {
  if (n == null) return "-";
  if (n >= 1e12) return `${(n / 1e12).toFixed(2)} 万亿`;
  if (n >= 1e8) return `${(n / 1e8).toFixed(2)} 亿`;
  return `${(n / 1e4).toFixed(0)} 万`;
}

/** 百分比格式化 */
export function fmtPct(n: number | null | undefined): string {
  if (n == null) return "-";
  return CN_PCT.format(n);
}

/** 相对时间（如 "3 小时前"） */
export function fmtRelative(date: string | null | undefined): string {
  if (!date) return "-";
  return formatDistanceToNow(new Date(date), { addSuffix: true, locale: zhCN });
}

/** 绝对时间（如 "2026-04-30 15:30"） */
export function fmtDatetime(date: string | null | undefined): string {
  if (!date) return "-";
  return format(new Date(date), "yyyy-MM-dd HH:mm");
}
