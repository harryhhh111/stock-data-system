import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { AlertTriangle, ExternalLink } from "lucide-react";
import type { OneOffEvent } from "@/lib/types/analyzer";
import { fmtPct, fmtYi } from "@/lib/utils/format";

interface Props {
  events: OneOffEvent[];
}

function MetricRow({
  label,
  original,
  normalized,
  format,
}: {
  label: string;
  original: number | null;
  normalized: number | null;
  format: "pe" | "pct" | "yi";
}) {
  const fmt = (v: number | null) => {
    if (v == null) return "-";
    if (format === "pe") return v.toFixed(1);
    if (format === "pct") return fmtPct(v);
    return fmtYi(v);
  };

  const diff =
    original != null && normalized != null ? normalized - original : null;

  return (
    <div className="grid grid-cols-4 items-center gap-2 py-2 text-sm border-b last:border-b-0">
      <span className="text-muted-foreground">{label}</span>
      <span className="text-right">{fmt(original)}</span>
      <span className="text-right font-medium">{fmt(normalized)}</span>
      <span
        className={`text-right text-xs ${
          diff == null
            ? "text-muted-foreground"
            : diff > 0
            ? "text-green-600"
            : diff < 0
            ? "text-red-600"
            : "text-muted-foreground"
        }`}
      >
        {diff == null ? "-" : `${diff > 0 ? "+" : ""}${fmt(diff)}`}
      </span>
    </div>
  );
}

function NormalizedOnlyRow({
  label,
  value,
  format,
}: {
  label: string;
  value: number | null;
  format: "pe" | "pct" | "yi";
}) {
  const fmt = (v: number | null) => {
    if (v == null) return "-";
    if (format === "pe") return v.toFixed(1);
    if (format === "pct") return fmtPct(v);
    return fmtYi(v);
  };

  return (
    <div className="grid grid-cols-2 items-center gap-2 py-2 text-sm border-b last:border-b-0">
      <span className="text-muted-foreground">{label}</span>
      <span className="text-right font-medium">{fmt(value)}</span>
    </div>
  );
}

export function OneOffEvents({ events }: Props) {
  if (!events || events.length === 0) return null;

  return (
    <Card className="border-l-4 border-l-amber-500">
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 text-base">
          <AlertTriangle className="h-4 w-4 text-amber-500" />
          <span>一次性事项提示</span>
          <Badge variant="secondary" className="text-xs font-normal">
            {events.length} 条
          </Badge>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-5">
        {events.map((event) => {
          const hasOriginal = event.original != null;
          const original = event.original ?? event.normalized;

          return (
            <div
              key={event.event_id}
              className="rounded-lg border bg-card p-4 space-y-3"
            >
              <div className="flex items-start justify-between gap-3">
                <div>
                  <h4 className="font-medium text-sm">{event.title}</h4>
                  <p className="text-xs text-muted-foreground mt-1">
                    报告期 {event.report_date} · 有效期至 {event.active_through}
                  </p>
                </div>
                <Badge variant="outline" className="text-amber-600 border-amber-200 bg-amber-50 shrink-0">
                  影响 TTM
                </Badge>
              </div>

              <p className="text-sm text-muted-foreground leading-relaxed">
                {event.description}
              </p>

              {event.adjustments.length > 0 && (
                <div className="flex flex-wrap gap-2">
                  {event.adjustments.map((adj, idx) => (
                    <Badge
                      key={idx}
                      variant="secondary"
                      className="text-xs font-normal"
                    >
                      {adj.label}: {fmtYi(adj.amount)}
                    </Badge>
                  ))}
                </div>
              )}

              <div className="rounded-md border bg-muted/30 px-3">
                {hasOriginal ? (
                  <>
                    <div className="grid grid-cols-4 items-center gap-2 py-2 text-xs text-muted-foreground border-b">
                      <span>指标</span>
                      <span className="text-right">原始值</span>
                      <span className="text-right">正常化后</span>
                      <span className="text-right">变动</span>
                    </div>
                    <MetricRow
                      label="净利润 TTM"
                      original={original.net_profit_ttm}
                      normalized={event.normalized.net_profit_ttm}
                      format="yi"
                    />
                    <MetricRow
                      label="PE TTM"
                      original={original.pe_ttm}
                      normalized={event.normalized.pe_ttm}
                      format="pe"
                    />
                    <MetricRow
                      label="FCF TTM"
                      original={original.fcf_ttm}
                      normalized={event.normalized.fcf_ttm}
                      format="yi"
                    />
                    <MetricRow
                      label="FCF Yield"
                      original={original.fcf_yield}
                      normalized={event.normalized.fcf_yield}
                      format="pct"
                    />
                  </>
                ) : (
                  <>
                    <div className="grid grid-cols-2 items-center gap-2 py-2 text-xs text-muted-foreground border-b">
                      <span>指标</span>
                      <span className="text-right">正常化后</span>
                    </div>
                    <NormalizedOnlyRow
                      label="净利润 TTM"
                      value={event.normalized.net_profit_ttm}
                      format="yi"
                    />
                    <NormalizedOnlyRow
                      label="PE TTM"
                      value={event.normalized.pe_ttm}
                      format="pe"
                    />
                    <NormalizedOnlyRow
                      label="FCF TTM"
                      value={event.normalized.fcf_ttm}
                      format="yi"
                    />
                    <NormalizedOnlyRow
                      label="FCF Yield"
                      value={event.normalized.fcf_yield}
                      format="pct"
                    />
                  </>
                )}
              </div>

              <a
                href={event.source_url}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-1 text-xs text-primary hover:underline underline-offset-2"
              >
                查看公司正式披露
                <ExternalLink className="h-3 w-3" />
              </a>
            </div>
          );
        })}
      </CardContent>
    </Card>
  );
}
