import { cn } from "@/lib/utils/cn";
import { Card, CardContent } from "@/components/ui/card";
import type { ReactNode } from "react";

interface Props {
  title: string;
  value: string | number;
  subtitle?: string;
  icon?: ReactNode;
  variant?: "default" | "success" | "warning" | "danger";
}

const borderMap: Record<string, string> = {
  default: "border-l-blue-500",
  success: "border-l-green-500",
  warning: "border-l-yellow-500",
  danger: "border-l-red-500",
};

export function StatCard({ title, value, subtitle, icon, variant = "default" }: Props) {
  return (
    <Card className={cn("border-l-4", borderMap[variant])}>
      <CardContent className="p-4 flex items-center justify-between">
        <div>
          <p className="text-sm text-muted-foreground">{title}</p>
          <p className={cn(
            "text-2xl font-bold",
            variant === "danger" && "text-red-500"
          )}>
            {value}
          </p>
          {subtitle && <p className="text-xs text-muted-foreground">{subtitle}</p>}
        </div>
        {icon && <div className="text-muted-foreground/40">{icon}</div>}
      </CardContent>
    </Card>
  );
}
