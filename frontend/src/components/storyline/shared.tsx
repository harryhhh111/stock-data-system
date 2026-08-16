import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import { Badge } from "@/components/ui/badge";
import { fmtYi, fmtPct } from "@/lib/utils/format";
import { cn } from "@/lib/utils/cn";
import type { StorylineReport, StorylineEvent } from "@/lib/types/storyline";

// ── 日期 / 期别工具 ──

export function yearOf(dateStr: string): number {
  return new Date(dateStr).getFullYear();
}

/** 报告期 → 季度桶 1-4 */
export function quarterOf(dateStr: string): number {
  return Math.ceil((new Date(dateStr).getMonth() + 1) / 3);
}

export function periodLabel(r: StorylineReport): string {
  const yy = String(yearOf(r.report_date)).slice(2);
  if (r.report_type === "annual") return `${yy}年报`;
  if (r.report_type === "semi") return `${yy}中报`;
  return `${yy}Q${quarterOf(r.report_date)}`;
}

// ── 事件类型 ──

export const EVENT_TYPE_LABEL: Record<string, string> = {
  ipo: "上市",
  product: "产品",
  management: "管理层",
  capital: "资本运作",
  milestone: "里程碑",
  "m&a": "并购",
  litigation: "诉讼",
  general: "其他",
};

export function eventTypeLabel(t: string): string {
  return EVENT_TYPE_LABEL[t] ?? t;
}

// ── 同比（红涨绿跌） ──

export function Yoy({ value, className }: { value: number | null; className?: string }) {
  if (value == null) return <span className={cn("text-muted-foreground", className)}>—</span>;
  const pct = (value * 100).toFixed(1);
  return (
    <span
      className={cn(
        "tabular-nums",
        value > 0 && "text-red-600",
        value < 0 && "text-green-600",
        className,
      )}
    >
      {value > 0 ? "↑ +" : value < 0 ? "↓ " : ""}
      {pct}%
    </span>
  );
}

// ── 年报大卡片（年度鱼骨 / 纵向故事共用） ──

export function AnnualCard({
  report,
  selected,
  onClick,
}: {
  report: StorylineReport;
  selected?: boolean;
  onClick?: () => void;
}) {
  return (
    <button
      onClick={onClick}
      className={cn(
        "w-44 shrink-0 rounded-xl border bg-card text-left shadow-sm transition-all",
        "hover:shadow-md hover:-translate-y-0.5",
        selected ? "border-primary ring-1 ring-primary" : "border-border",
        onClick ? "cursor-pointer" : "cursor-default",
      )}
    >
      <div className="flex items-baseline justify-between px-3 pt-2.5 pb-1">
        <span className="text-sm font-bold">{periodLabel(report)}</span>
        {report.notice_date && (
          <span className="text-[10px] text-muted-foreground">
            公告 {report.notice_date.slice(5, 10)}
          </span>
        )}
      </div>
      <div className="px-3 pb-1.5">
        <div className="text-[10px] text-muted-foreground">营业总收入</div>
        <div className="flex items-baseline gap-1.5">
          <span className="text-base font-semibold tabular-nums">{fmtYi(report.revenue)}</span>
          <Yoy value={report.revenue_yoy} className="text-xs" />
        </div>
        <div className="text-[10px] text-muted-foreground mt-1">归母净利润</div>
        <div className="flex items-baseline gap-1.5">
          <span className="text-base font-semibold tabular-nums">{fmtYi(report.net_profit)}</span>
          <Yoy value={report.net_profit_yoy} className="text-xs" />
        </div>
      </div>
      <div className="border-t mx-3" />
      <div className="grid grid-cols-2 gap-x-2 px-3 py-1.5 text-[10px] text-muted-foreground">
        <span>毛利率 {fmtPct(report.gross_margin)}</span>
        <span>负债率 {fmtPct(report.debt_ratio)}</span>
        <span className="col-span-2">经营现金流 {fmtYi(report.cfo_net)}</span>
      </div>
    </button>
  );
}

// ── 季度小卡片（展开季报 / 季度全景共用） ──

const TYPE_STYLE: Record<StorylineReport["report_type"], string> = {
  annual: "border-l-amber-500",
  semi: "border-l-violet-500",
  quarterly: "border-l-sky-500",
};

export function QuarterCard({ report }: { report: StorylineReport }) {
  return (
    <div
      className={cn(
        "w-36 shrink-0 rounded-lg border border-l-4 bg-card px-2.5 py-2 shadow-sm",
        TYPE_STYLE[report.report_type],
      )}
    >
      <div className="flex items-baseline justify-between">
        <span className="text-xs font-bold">{periodLabel(report)}</span>
        {report.notice_date && (
          <span className="text-[10px] text-muted-foreground">{report.notice_date.slice(5, 10)}</span>
        )}
      </div>
      <div className="mt-1 space-y-0.5 text-xs">
        <div className="flex items-baseline justify-between gap-1">
          <span className="text-muted-foreground">营收</span>
          <span className="font-medium tabular-nums">{fmtYi(report.revenue)}</span>
        </div>
        <div className="flex justify-end">
          <Yoy value={report.revenue_yoy} className="text-[10px]" />
        </div>
        <div className="flex items-baseline justify-between gap-1">
          <span className="text-muted-foreground">净利</span>
          <span className="font-medium tabular-nums">{fmtYi(report.net_profit)}</span>
        </div>
        <div className="flex justify-end">
          <Yoy value={report.net_profit_yoy} className="text-[10px]" />
        </div>
      </div>
      <div className="mt-1 border-t pt-1 text-[10px] text-muted-foreground">
        毛利率 {fmtPct(report.gross_margin)} · 负债率 {fmtPct(report.debt_ratio)}
      </div>
    </div>
  );
}

// ── 事件节点与详情弹层 ──

export function EventDialog({
  event,
  onClose,
}: {
  event: StorylineEvent | null;
  onClose: () => void;
}) {
  return (
    <Dialog open={event != null} onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{event?.title}</DialogTitle>
          <DialogDescription className="flex items-center gap-2">
            <span>{event?.event_date}</span>
            {event && <Badge variant="outline">{eventTypeLabel(event.event_type)}</Badge>}
          </DialogDescription>
        </DialogHeader>
        {event?.summary && (
          <p className="text-sm text-foreground whitespace-pre-wrap">{event.summary}</p>
        )}
        {event?.source_url && (
          <a
            href={event.source_url}
            target="_blank"
            rel="noreferrer"
            className="text-sm text-primary underline underline-offset-4"
          >
            查看来源
          </a>
        )}
      </DialogContent>
    </Dialog>
  );
}

/** 鱼骨事件节点：标题 chip + 引线 + 圆点，高低错落由 offset 控制 */
export function EventNode({
  event,
  offset,
  onSelect,
}: {
  event: StorylineEvent;
  offset: number; // 0 = 贴轴, 1 = 抬高
  onSelect: (e: StorylineEvent) => void;
}) {
  return (
    <button
      onClick={() => onSelect(event)}
      className="absolute left-1/2 -translate-x-1/2 flex flex-col items-center group w-max max-w-full"
      style={{ bottom: `${offset * 2.25}rem` }}
    >
      <span className="text-[11px] px-2 py-0.5 rounded-full bg-primary/10 text-primary max-w-28 truncate group-hover:bg-primary group-hover:text-primary-foreground transition-colors">
        {event.title}
      </span>
      <span className="w-px h-3 bg-primary/40" />
      <span className="h-1.5 w-1.5 rounded-full bg-primary" />
    </button>
  );
}

// ── 范围过滤 ──

export type RangeYears = 5 | 10 | 0; // 0 = 全部

export function filterByRange<T>(items: T[], getDate: (t: T) => string, range: RangeYears): T[] {
  if (range === 0 || items.length === 0) return items;
  const latest = Math.max(...items.map((t) => yearOf(getDate(t))));
  const cutoff = latest - range + 1;
  return items.filter((t) => yearOf(getDate(t)) >= cutoff);
}
