import { useMemo, useRef, useState, useEffect } from "react";
import { cn } from "@/lib/utils/cn";
import type { StorylineReport, StorylineEvent } from "@/lib/types/storyline";
import {
  QuarterCard,
  EventNode,
  EventDialog,
  yearOf,
  quarterOf,
  filterByRange,
  type RangeYears,
} from "./shared";

interface Props {
  reports: StorylineReport[];
  events: StorylineEvent[];
  range: RangeYears;
}

interface Col {
  year: number;
  q: number;
}

/** 季度全景：每年拆成 Q1-Q4 四列铺开，有财报的列渲染季度卡片 */
export function FishboneQuarterly({ reports, events, range }: Props) {
  const [selectedEvent, setSelectedEvent] = useState<StorylineEvent | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  const { columns, reportMap, eventMap, yearSpans } = useMemo(() => {
    const rs = filterByRange(reports, (r) => r.report_date, range);
    const es = filterByRange(events, (e) => e.event_date, range);

    const reportMap = new Map<string, StorylineReport>();
    for (const r of rs) {
      reportMap.set(`${yearOf(r.report_date)}-${quarterOf(r.report_date)}`, r);
    }
    const eventMap = new Map<string, StorylineEvent[]>();
    for (const e of es) {
      const k = `${yearOf(e.event_date)}-${quarterOf(e.event_date)}`;
      eventMap.set(k, [...(eventMap.get(k) ?? []), e]);
    }

    if (rs.length === 0 && es.length === 0) {
      return { columns: [] as Col[], reportMap, eventMap, yearSpans: [] as { year: number; span: number }[] };
    }
    const years = [
      ...rs.map((r) => yearOf(r.report_date)),
      ...es.map((e) => yearOf(e.event_date)),
    ];
    const minY = Math.min(...years);
    const maxY = Math.max(...years);

    const columns: Col[] = [];
    const yearSpans: { year: number; span: number }[] = [];
    for (let y = minY; y <= maxY; y++) {
      for (let q = 1; q <= 4; q++) columns.push({ year: y, q });
      yearSpans.push({ year: y, span: 4 });
    }
    return { columns, reportMap, eventMap, yearSpans };
  }, [reports, events, range]);

  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollLeft = el.scrollWidth;
  }, [columns.length]);

  if (columns.length === 0) {
    return <div className="py-16 text-center text-sm text-muted-foreground">该范围内暂无数据</div>;
  }

  const hasEvents = eventMap.size > 0;
  const key = (c: Col) => `${c.year}-${c.q}`;

  return (
    <div className="space-y-3">
      <div ref={scrollRef} className="overflow-x-auto rounded-xl border bg-card/50">
        <div className="inline-block min-w-full px-4 pt-3 pb-4">
          {/* 大事件鱼骨 lane */}
          {hasEvents && (
            <div className="flex">
              {columns.map((c) => (
                <div key={key(c)} className="relative h-28 w-36 shrink-0 mx-1">
                  {(eventMap.get(key(c)) ?? []).map((e, i) => (
                    <EventNode key={e.id} event={e} offset={i % 2} onSelect={setSelectedEvent} />
                  ))}
                </div>
              ))}
            </div>
          )}

          {/* 年份行（每年跨 4 列） */}
          <div className="flex">
            {yearSpans.map((ys) => (
              <div
                key={ys.year}
                className="shrink-0 text-center text-sm font-bold text-foreground pb-1"
                style={{ width: `${ys.span * 10}rem` }}
              >
                {ys.year}
              </div>
            ))}
          </div>

          {/* 时间主轴（季度刻度） */}
          <div className="flex">
            {columns.map((c) => (
              <div key={key(c)} className="w-36 shrink-0 mx-1 flex flex-col items-center">
                <div className="w-full border-t-2 border-primary/30 relative">
                  <span className="absolute left-1/2 -translate-x-1/2 -top-[4px] h-1.5 w-1.5 rounded-full bg-primary/60" />
                </div>
                <span
                  className={cn(
                    "mt-1 text-xs",
                    reportMap.has(key(c)) ? "text-foreground font-medium" : "text-muted-foreground",
                  )}
                >
                  Q{c.q}
                </span>
              </div>
            ))}
          </div>

          {/* 季度卡片行 */}
          <div className="flex mt-2">
            {columns.map((c) => {
              const r = reportMap.get(key(c));
              return (
                <div key={key(c)} className="w-36 shrink-0 mx-1">
                  {r && <QuarterCard report={r} />}
                </div>
              );
            })}
          </div>
        </div>
      </div>

      <div className="flex items-center gap-4 text-[11px] text-muted-foreground px-1">
        <span className="flex items-center gap-1"><span className="w-2 h-2 rounded-sm bg-sky-500" /> 季报</span>
        <span className="flex items-center gap-1"><span className="w-2 h-2 rounded-sm bg-violet-500" /> 中报</span>
        <span className="flex items-center gap-1"><span className="w-2 h-2 rounded-sm bg-amber-500" /> 年报</span>
      </div>

      <EventDialog event={selectedEvent} onClose={() => setSelectedEvent(null)} />
    </div>
  );
}
