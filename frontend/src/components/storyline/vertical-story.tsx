import { useMemo, useState } from "react";
import { cn } from "@/lib/utils/cn";
import type { StorylineReport, StorylineEvent } from "@/lib/types/storyline";
import {
  AnnualCard,
  QuarterCard,
  EventDialog,
  eventTypeLabel,
  yearOf,
  filterByRange,
  type RangeYears,
} from "./shared";
import { ChevronDown } from "lucide-react";

interface Props {
  reports: StorylineReport[];
  events: StorylineEvent[];
  range: RangeYears;
}

/** 纵向故事流：最新一年在最上，向下滚动进入过去 */
export function VerticalStory({ reports, events, range }: Props) {
  const [selectedEvent, setSelectedEvent] = useState<StorylineEvent | null>(null);
  const [openYears, setOpenYears] = useState<Set<number>>(new Set());

  const years = useMemo(() => {
    const rs = filterByRange(reports, (r) => r.report_date, range);
    const es = filterByRange(events, (e) => e.event_date, range);
    const byYear = new Map<number, { reports: StorylineReport[]; events: StorylineEvent[] }>();
    for (const r of rs) {
      const y = yearOf(r.report_date);
      const entry = byYear.get(y) ?? { reports: [], events: [] };
      entry.reports.push(r);
      byYear.set(y, entry);
    }
    for (const e of es) {
      const y = yearOf(e.event_date);
      const entry = byYear.get(y) ?? { reports: [], events: [] };
      entry.events.push(e);
      byYear.set(y, entry);
    }
    return [...byYear.entries()]
      .map(([year, v]) => ({
        year,
        reports: v.reports.sort((a, b) => a.report_date.localeCompare(b.report_date)),
        events: v.events.sort((a, b) => a.event_date.localeCompare(b.event_date)),
        annual: v.reports.find((r) => r.report_type === "annual") ?? v.reports[v.reports.length - 1],
      }))
      .sort((a, b) => b.year - a.year);
  }, [reports, events, range]);

  if (years.length === 0) {
    return <div className="py-16 text-center text-sm text-muted-foreground">该范围内暂无数据</div>;
  }

  const toggleYear = (y: number) => {
    setOpenYears((prev) => {
      const next = new Set(prev);
      if (next.has(y)) next.delete(y);
      else next.add(y);
      return next;
    });
  };

  return (
    <div className="max-w-3xl">
      {years.map((y, idx) => {
        const quarterlies = y.reports.filter((r) => r.report_type !== "annual");
        const open = openYears.has(y.year);
        return (
          <div key={y.year} className="relative pl-16 pb-8">
            {/* 左侧年份轨 */}
            <div className="absolute left-0 top-0 bottom-0 flex flex-col items-center w-12">
              <span className="text-lg font-bold tabular-nums bg-background relative z-10">{y.year}</span>
              {idx < years.length - 1 && <div className="flex-1 w-px bg-border mt-1" />}
            </div>

            {/* 事件 chips */}
            {y.events.length > 0 && (
              <div className="flex flex-wrap gap-2 mb-3">
                {y.events.map((e) => (
                  <button
                    key={e.id}
                    onClick={() => setSelectedEvent(e)}
                    className="inline-flex items-center gap-1.5 rounded-full border bg-primary/5 px-3 py-1 text-xs hover:bg-primary/15 transition-colors"
                  >
                    <span className="h-1.5 w-1.5 rounded-full bg-primary" />
                    <span className="text-muted-foreground">{e.event_date.slice(5)}</span>
                    <span className="font-medium">{e.title}</span>
                    <span className="text-muted-foreground">· {eventTypeLabel(e.event_type)}</span>
                  </button>
                ))}
              </div>
            )}

            {/* 年报卡片 */}
            {y.annual && (
              <div>
                <AnnualCard report={y.annual} />
                {quarterlies.length > 0 && (
                  <button
                    onClick={() => toggleYear(y.year)}
                    className="mt-2 inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground transition-colors"
                  >
                    <ChevronDown className={cn("h-3.5 w-3.5 transition-transform", open && "rotate-180")} />
                    {open ? "收起季报" : `${y.year} 季报明细（${quarterlies.length} 期）`}
                  </button>
                )}
                {open && (
                  <div className="mt-3 flex gap-3 overflow-x-auto pb-1 animate-in fade-in duration-200">
                    {quarterlies.map((r) => (
                      <QuarterCard key={`${r.report_date}-${r.report_type}`} report={r} />
                    ))}
                  </div>
                )}
              </div>
            )}
          </div>
        );
      })}

      <EventDialog event={selectedEvent} onClose={() => setSelectedEvent(null)} />
    </div>
  );
}
