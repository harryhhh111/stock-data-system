import { AlertTriangle } from "lucide-react";

interface Props {
  errors: unknown[];
}

export function ErrorBanner({ errors }: Props) {
  if (errors.length === 0) return null;
  return (
    <div className="border border-destructive/50 bg-destructive/10 text-destructive rounded-lg px-4 py-3 text-sm flex items-start gap-2">
      <AlertTriangle className="h-4 w-4 mt-0.5 shrink-0" />
      <span>部分市场数据加载失败: {errors.map((e: any) => e?.message ?? String(e)).join("; ")}</span>
    </div>
  );
}
