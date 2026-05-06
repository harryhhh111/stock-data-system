import { cn } from "@/lib/utils/cn";
import { TrendingUp, Gem, Coins, Zap, ShieldCheck } from "lucide-react";
import type { Preset } from "@/lib/types/screener";

const PRESET_ICONS: Record<string, typeof TrendingUp> = {
  classic_value: Gem,
  fcf_roe_value: ShieldCheck,
  quality: TrendingUp,
  growth_value: Zap,
  dividend_value: Coins,
};

const PRESET_COLORS: Record<string, string> = {
  classic_value: "border-amber-500/30 bg-amber-500/5 hover:bg-amber-500/10",
  fcf_roe_value: "border-violet-500/30 bg-violet-500/5 hover:bg-violet-500/10",
  quality: "border-emerald-500/30 bg-emerald-500/5 hover:bg-emerald-500/10",
  growth_value: "border-blue-500/30 bg-blue-500/5 hover:bg-blue-500/10",
  dividend_value: "border-rose-500/30 bg-rose-500/5 hover:bg-rose-500/10",
};

const PRESET_ACTIVE: Record<string, string> = {
  classic_value: "border-amber-500 bg-amber-500/15 ring-1 ring-amber-500/30",
  fcf_roe_value: "border-violet-500 bg-violet-500/15 ring-1 ring-violet-500/30",
  quality: "border-emerald-500 bg-emerald-500/15 ring-1 ring-emerald-500/30",
  growth_value: "border-blue-500 bg-blue-500/15 ring-1 ring-blue-500/30",
  dividend_value: "border-rose-500 bg-rose-500/15 ring-1 ring-rose-500/30",
};

interface Props {
  presets: Preset[];
  selected: string;
  onSelect: (name: string) => void;
}

export function PresetCards({ presets, selected, onSelect }: Props) {
  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5 gap-3">
      {presets.map((preset) => {
        const Icon = PRESET_ICONS[preset.name] ?? Gem;
        const isActive = selected === preset.name;
        return (
          <button
            key={preset.name}
            onClick={() => onSelect(preset.name)}
            className={cn(
              "relative flex flex-col items-start gap-2 rounded-lg border p-4 text-left transition-all duration-150",
              isActive
                ? PRESET_ACTIVE[preset.name] ?? "border-primary bg-primary/10 ring-1 ring-primary/30"
                : PRESET_COLORS[preset.name] ?? "border-border bg-card hover:bg-accent",
            )}
          >
            <div className="flex items-center gap-2">
              <Icon className={cn("h-4 w-4", isActive ? "text-foreground" : "text-muted-foreground")} />
              <span className={cn("text-sm font-medium", isActive && "text-foreground")}>
                {preset.name.replace(/_/g, " ")}
              </span>
            </div>
            <p className="text-xs text-muted-foreground line-clamp-2">{preset.description}</p>
            <div className="flex gap-2 mt-auto pt-1">
              <span className="text-[10px] tabular-nums text-muted-foreground bg-muted rounded px-1.5 py-0.5">
                Top {preset.top_n}
              </span>
              <span className="text-[10px] tabular-nums text-muted-foreground bg-muted rounded px-1.5 py-0.5">
                {Object.keys(preset.weights).length} 因子
              </span>
            </div>
          </button>
        );
      })}
    </div>
  );
}
