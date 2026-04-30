import { Card, CardContent } from "@/components/ui/card";
import { cn } from "@/lib/utils/cn";

interface RatingItem {
  label: string;
  rating: number | null;
  star: string;
  verdict: string;
}

interface Props {
  items: RatingItem[];
}

function ratingColor(rating: number | null): string {
  if (rating == null) return "text-muted-foreground";
  if (rating >= 4) return "text-emerald-500";
  if (rating >= 3) return "text-blue-500";
  if (rating >= 2) return "text-amber-500";
  return "text-red-500";
}

function ratingBg(rating: number | null): string {
  if (rating == null) return "border-l-muted-foreground/30";
  if (rating >= 4) return "border-l-emerald-500";
  if (rating >= 3) return "border-l-blue-500";
  if (rating >= 2) return "border-l-amber-500";
  return "border-l-red-500";
}

export function RatingCardGrid({ items }: Props) {
  return (
    <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
      {items.map((item) => (
        <Card key={item.label} className={cn("border-l-4", ratingBg(item.rating))}>
          <CardContent className="p-3 space-y-1">
            <div className="flex items-center justify-between">
              <span className="text-xs text-muted-foreground font-medium uppercase tracking-wide">{item.label}</span>
              <span className={cn("text-lg font-bold tabular-nums", ratingColor(item.rating))}>
                {item.rating?.toFixed(1) ?? "-"}
              </span>
            </div>
            <div className="text-xs">{item.star}</div>
            <p className="text-xs text-muted-foreground line-clamp-2">{item.verdict}</p>
          </CardContent>
        </Card>
      ))}
    </div>
  );
}
