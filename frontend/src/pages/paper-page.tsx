import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import type { BacktestPreset } from "@/lib/types/backtest";
import { useNavigate } from "react-router-dom";
import { paperApi, backtestApi } from "@/lib/api/client";
import { usePaperFilterStore } from "@/lib/store/paper-store";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Input } from "@/components/ui/input";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from "@/components/ui/dialog";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { PageHeader } from "@/components/layout/page-header";
import { Wallet, Plus, TrendingUp, TrendingDown } from "lucide-react";
import type { Market } from "@/lib/types/common";
import type { PaperAccount } from "@/lib/types/paper";

const MARKETS: { value: Market; label: string }[] = [
  { value: "CN_A", label: "A 股" },
  { value: "CN_HK", label: "港股" },
  { value: "US", label: "美股" },
];

function AccountCard({ account }: { account: PaperAccount }) {
  const navigate = useNavigate();
  const nav = account.nav;
  const isPositive = nav >= 1.0;

  return (
    <Card
      className="cursor-pointer hover:shadow-md transition-shadow"
      onClick={() => navigate(`/paper/${account.account_id}`)}
    >
      <CardHeader className="pb-2">
        <div className="flex items-center justify-between">
          <CardTitle className="text-sm truncate">{account.account_name}</CardTitle>
          <Badge variant={account.status === "active" ? "default" : "secondary"}>
            {account.status}
          </Badge>
        </div>
      </CardHeader>
      <CardContent className="space-y-2">
        <div className="flex items-center justify-between">
          <span className="text-xs text-muted-foreground">{account.strategy_display_name}</span>
          <span className="text-xs text-muted-foreground">{account.market}</span>
        </div>
        <div className="flex items-center gap-2">
          {isPositive ? (
            <TrendingUp className="h-4 w-4 text-green-500" />
          ) : (
            <TrendingDown className="h-4 w-4 text-red-500" />
          )}
          <span className={`text-xl font-bold tabular-nums ${isPositive ? "text-green-600" : "text-red-600"}`}>
            {nav.toFixed(4)}
          </span>
        </div>
        <div className="flex justify-between text-xs text-muted-foreground">
          <span>市值 {account.total_value.toLocaleString("zh-CN", { maximumFractionDigits: 0 })}</span>
          <span>{account.last_valued_at ? account.last_valued_at.slice(0, 10) : "未运行"}</span>
        </div>
      </CardContent>
    </Card>
  );
}

function CreateAccountDialog({
  open, onOpenChange, onCreated,
}: { open: boolean; onOpenChange: (v: boolean) => void; onCreated: () => void }) {
  const [name, setName] = useState("");
  const [strategy, setStrategy] = useState("commodity_rotation");
  const [market, setMarket] = useState<string>("CN_A");
  const [capital, setCapital] = useState(1_000_000);
  const [submitting, setSubmitting] = useState(false);

  const { data: presetsData } = useQuery({
    queryKey: ["paper", "presets"],
    queryFn: () => backtestApi.presets(),
  });
  const presets = presetsData?.presets ?? [];

  const handleSubmit = async () => {
    setSubmitting(true);
    try {
      await paperApi.create({
        account_name: name,
        strategy_name: strategy,
        market: market as Market,
        initial_capital: capital,
      });
      onCreated();
      setName("");
      setStrategy("commodity_rotation");
      setMarket("CN_A");
      setCapital(1_000_000);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader><DialogTitle>新建模拟盘账户</DialogTitle></DialogHeader>
        <div className="space-y-4 py-4">
          <div>
            <label className="text-xs text-muted-foreground mb-1 block">账户名称</label>
            <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="如：商品周期实盘" />
          </div>
          <div>
            <label className="text-xs text-muted-foreground mb-1 block">策略</label>
            <Select value={strategy} onValueChange={setStrategy}>
              <SelectTrigger><SelectValue /></SelectTrigger>
              <SelectContent>
                {presets.map((p: BacktestPreset) => (
                  <SelectItem key={p.name} value={p.name}>
                    {p.description}{p.type === "composite" ? " · 复合" : ""}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div>
            <label className="text-xs text-muted-foreground mb-1 block">市场</label>
            <Select value={market} onValueChange={setMarket}>
              <SelectTrigger><SelectValue /></SelectTrigger>
              <SelectContent>
                {MARKETS.map((m) => <SelectItem key={m.value} value={m.value}>{m.label}</SelectItem>)}
              </SelectContent>
            </Select>
          </div>
          <div>
            <label className="text-xs text-muted-foreground mb-1 block">初始资金</label>
            <Input type="number" value={capital} onChange={(e) => setCapital(Number(e.target.value))} />
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>取消</Button>
          <Button onClick={handleSubmit} disabled={submitting || !name}>
            {submitting ? "创建中..." : "创建"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export function PaperPage() {
  const filter = usePaperFilterStore();
  const [createOpen, setCreateOpen] = useState(false);

  const { data: accounts, isLoading, refetch } = useQuery({
    queryKey: ["paper", "accounts", filter.statusFilter],
    queryFn: () => paperApi.list({ status: filter.statusFilter }),
    refetchInterval: 60_000,
  });

  return (
    <div className="space-y-6 animate-in fade-in duration-300">
      <PageHeader icon={Wallet} title="模拟盘" description="纸面组合跟踪" />

      <div className="flex items-center justify-between">
        <Select value={filter.statusFilter} onValueChange={filter.setStatusFilter}>
          <SelectTrigger className="w-32">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="active">活跃</SelectItem>
            <SelectItem value="paused">暂停</SelectItem>
            <SelectItem value="archived">归档</SelectItem>
          </SelectContent>
        </Select>
        <Button onClick={() => setCreateOpen(true)}>
          <Plus className="h-4 w-4 mr-2" />新建账户
        </Button>
      </div>

      {isLoading ? (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {[1, 2, 3].map((i) => <Skeleton key={i} className="h-40" />)}
        </div>
      ) : accounts && accounts.length > 0 ? (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {accounts.map((a) => <AccountCard key={a.account_id} account={a} />)}
        </div>
      ) : (
        <div className="text-center text-muted-foreground py-12">
          <Wallet className="h-12 w-12 mx-auto mb-4 opacity-30" />
          <p>暂无模拟盘账户</p>
          <p className="text-xs mt-1">点击"新建账户"开始</p>
        </div>
      )}

      <CreateAccountDialog
        open={createOpen}
        onOpenChange={setCreateOpen}
        onCreated={() => { setCreateOpen(false); refetch(); }}
      />
    </div>
  );
}
