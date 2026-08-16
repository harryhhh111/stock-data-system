import { useMemo, useRef, useState, useEffect } from "react";
import { cn } from "@/lib/utils/cn";
import { fmtYi, fmtPct } from "@/lib/utils/format";
import type { StorylineReport, StorylineEvent } from "@/lib/types/storyline";
import {
  QuarterCard,
  EventNode,
  EventDialog,
  Yoy,
  yearOf,
  periodLabel,
  filterByRange,
  type RangeYears,
} from "./shared";

interface Props {
  reports: StorylineReport[];
  events: StorylineEvent[];
  dividends: Record<string, number>;
  range: RangeYears;
}

interface YearCol {
  year: number;
  report?: StorylineReport;
  hasQuarterly: boolean;
  events: StorylineEvent[];
}

const LABEL_COL = "7.5rem";
const YEAR_COL = "11rem";

/** 年度鱼骨（表格卡片融合）：label 冻结在左，年份列无缝连成一条数据带 */
export function FishboneAnnual({ reports, events, dividends, range }: Props) {
  const [selectedEvent, setSelectedEvent] = useState<StorylineEvent | null>(null);
  const [expandedYear, setExpandedYear] = useState<number | null>(null);
  const [hoverYear, setHoverYear] = useState<number | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  const years = useMemo<YearCol[]>(() => {
    const rs = filterByRange(reports, (r) => r.report_date, range);
    const es = filterByRange(events, (e) => e.event_date, range);
    const annualByYear = new Map<number, StorylineReport>();
    const quarterCountByYear = new Map<number, number>();
    for (const r of rs) {
      const y = yearOf(r.report_date);
      if (r.report_type === "annual") annualByYear.set(y, r);
      else quarterCountByYear.set(y, (quarterCountByYear.get(y) ?? 0) + 1);
    }
    // 只有季报的年份用最新一期代表
    for (const r of rs) {
      const y = yearOf(r.report_date);
      if (!annualByYear.has(y)) annualByYear.set(y, r);
    }
    const eventByYear = new Map<number, StorylineEvent[]>();
    for (const e of es) {
      const y = yearOf(e.event_date);
      eventByYear.set(y, [...(eventByYear.get(y) ?? []), e]);
    }
    const ys = [...new Set([...annualByYear.keys(), ...eventByYear.keys()])].sort((a, b) => a - b);
    return ys.map((y) => ({
      year: y,
      report: annualByYear.get(y),
      hasQuarterly: (quarterCountByYear.get(y) ?? 0) > 0,
      events: eventByYear.get(y) ?? [],
    }));
  }, [reports, events, range]);

  // 锚定最新：初始滚动到最右
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollLeft = el.scrollWidth;
  }, [years.length]);

  const expandedReports = useMemo(() => {
    if (expandedYear == null) return [];
    return reports
      .filter((r) => yearOf(r.report_date) === expandedYear)
      .sort((a, b) => a.report_date.localeCompare(b.report_date));
  }, [reports, expandedYear]);

  if (years.length === 0) {
    return <div className="py-16 text-center text-sm text-muted-foreground">该范围内暂无数据</div>;
  }

  const hasEvents = years.some((y) => y.events.length > 0);
  const gridTemplate = `${LABEL_COL} repeat(${years.length}, ${YEAR_COL})`;

  const toggleYear = (y: YearCol) => {
    if (!y.hasQuarterly) return;
    setExpandedYear(expandedYear === y.year ? null : y.year);
  };

  // 左侧冻结 label 单元格
  const labelCell = (text: string, extra?: string) => (
    <div
      className={cn(
        "sticky left-0 z-10 bg-card px-3 flex items-center text-xs text-muted-foreground border-r",
        extra,
      )}
    >
      {text}
    </div>
  );

  // 数据带单元格：整列悬停高亮 + 点击展开季报
  const dataCell = (y: YearCol, children: React.ReactNode, extra?: string) => (
    <div
      onMouseEnter={() => setHoverYear(y.year)}
      onMouseLeave={() => setHoverYear(null)}
      onClick={() => toggleYear(y)}
      className={cn(
        "px-3 py-2 text-center transition-colors border-r border-border/50 last:border-r-0",
        hoverYear === y.year && "bg-accent/50",
        expandedYear === y.year && "bg-primary/5",
        y.hasQuarterly && "cursor-pointer",
        extra,
      )}
    >
      {children}
    </div>
  );

  return (
    <div className="space-y-3">
      <div ref={scrollRef} className="overflow-x-auto rounded-xl border bg-card">
        <div className="inline-block min-w-full">
          {/* 大事件鱼骨 lane */}
          {hasEvents && (
            <div className="grid" style={{ gridTemplateColumns: gridTemplate }}>
              {labelCell("大事件", "h-28")}
              {years.map((y) => (
                <div key={y.year} className="relative h-28">
                  {y.events.map((e, i) => (
                    <EventNode key={e.id} event={e} offset={i % 2} onSelect={setSelectedEvent} />
                  ))}
                </div>
              ))}
            </div>
          )}

          {/* 时间主轴 */}
          <div className="grid" style={{ gridTemplateColumns: gridTemplate }}>
            {labelCell("年份", "border-t-2 border-primary/30 py-1.5")}
            {years.map((y) => (
              <div
                key={y.year}
                className="border-t-2 border-primary/30 relative flex justify-center py-1.5"
              >
                <span className="absolute left-1/2 -translate-x-1/2 -top-[5px] h-2 w-2 rounded-full bg-primary/60" />
                <span
                  className={cn(
                    "text-sm font-bold",
                    y.report ? "text-foreground" : "text-muted-foreground",
                  )}
                >
                  {y.year}
                </span>
              </div>
            ))}
          </div>

          {/* 期别行（公告日 + 展开提示） */}
          <div className="grid border-t" style={{ gridTemplateColumns: gridTemplate }}>
            {labelCell("期别")}
            {years.map((y) =>
              dataCell(
                y,
                y.report && (
                  <>
                    <div className="text-sm font-semibold">
                      {periodLabel(y.report)}
                      {y.hasQuarterly && (
                        <span className="ml-1 text-[10px] font-normal text-muted-foreground">
                          {expandedYear === y.year ? "▴" : "▾"}
                        </span>
                      )}
                    </div>
                    {y.report.notice_date && (
                      <div className="text-[10px] text-muted-foreground">
                        公告 {y.report.notice_date.slice(5, 10)}
                      </div>
                    )}
                  </>
                ),
              ),
            )}
          </div>

          {/* 营业总收入 */}
          <div className="grid border-t" style={{ gridTemplateColumns: gridTemplate }}>
            {labelCell("营业总收入")}
            {years.map((y) =>
              dataCell(
                y,
                y.report && (
                  <>
                    <div className="text-base font-semibold tabular-nums">{fmtYi(y.report.revenue)}</div>
                    <Yoy value={y.report.revenue_yoy} className="text-xs" />
                  </>
                ),
              ),
            )}
          </div>

          {/* 归母净利润 */}
          <div className="grid border-t" style={{ gridTemplateColumns: gridTemplate }}>
            {labelCell("归母净利润")}
            {years.map((y) =>
              dataCell(
                y,
                y.report && (
                  <>
                    <div className="text-base font-semibold tabular-nums">{fmtYi(y.report.net_profit)}</div>
                    <Yoy value={y.report.net_profit_yoy} className="text-xs" />
                  </>
                ),
              ),
            )}
          </div>

          {/* 扣非净利润 */}
          <div className="grid border-t" style={{ gridTemplateColumns: gridTemplate }}>
            {labelCell("扣非净利润")}
            {years.map((y) =>
              dataCell(
                y,
                y.report && <span className="text-sm tabular-nums">{fmtYi(y.report.net_profit_excl)}</span>,
              ),
            )}
          </div>

          {/* ROE */}
          <div className="grid border-t" style={{ gridTemplateColumns: gridTemplate }}>
            {labelCell("ROE")}
            {years.map((y) =>
              dataCell(
                y,
                y.report && <span className="text-sm tabular-nums">{fmtPct(y.report.roe)}</span>,
              ),
            )}
          </div>

          {/* EPS */}
          <div className="grid border-t" style={{ gridTemplateColumns: gridTemplate }}>
            {labelCell("EPS")}
            {years.map((y) =>
              dataCell(
                y,
                y.report && (
                  <span className="text-sm tabular-nums">
                    {y.report.eps_basic != null ? y.report.eps_basic.toFixed(2) : "-"}
                  </span>
                ),
              ),
            )}
          </div>

          {/* 毛利率 / 负债率 */}
          <div className="grid border-t" style={{ gridTemplateColumns: gridTemplate }}>
            {labelCell("毛利率")}
            {years.map((y) =>
              dataCell(
                y,
                y.report && <span className="text-sm tabular-nums">{fmtPct(y.report.gross_margin)}</span>,
              ),
            )}
          </div>
          <div className="grid border-t" style={{ gridTemplateColumns: gridTemplate }}>
            {labelCell("资产负债率")}
            {years.map((y) =>
              dataCell(
                y,
                y.report && <span className="text-sm tabular-nums">{fmtPct(y.report.debt_ratio)}</span>,
              ),
            )}
          </div>

          {/* 经营现金流 */}
          <div className="grid border-t" style={{ gridTemplateColumns: gridTemplate }}>
            {labelCell("经营现金流")}
            {years.map((y) =>
              dataCell(
                y,
                y.report && <span className="text-sm tabular-nums">{fmtYi(y.report.cfo_net)}</span>,
              ),
            )}
          </div>

          {/* 每股分红 */}
          <div className="grid border-t" style={{ gridTemplateColumns: gridTemplate }}>
            {labelCell("每股分红")}
            {years.map((y) =>
              dataCell(
                y,
                <span className="text-sm tabular-nums">
                  {dividends[String(y.year)] != null ? dividends[String(y.year)].toFixed(2) : "-"}
                </span>,
              ),
            )}
          </div>
        </div>
      </div>

      {years.some((y) => y.hasQuarterly) && (
        <p className="text-[11px] text-muted-foreground px-1">带 ▾ 的年份可点击展开逐期明细</p>
      )}

      {/* 展开的季度明细 */}
      {expandedYear != null && expandedReports.length > 0 && (
        <div className="rounded-xl border bg-card p-4 animate-in fade-in slide-in-from-top-2 duration-200">
          <div className="text-sm font-semibold mb-3">{expandedYear} 年逐期明细（累计口径）</div>
          <div className="flex gap-3 overflow-x-auto pb-1">
            {expandedReports.map((r) => (
              <QuarterCard key={`${r.report_date}-${r.report_type}`} report={r} />
            ))}
          </div>
        </div>
      )}

      <EventDialog event={selectedEvent} onClose={() => setSelectedEvent(null)} />
    </div>
  );
}
