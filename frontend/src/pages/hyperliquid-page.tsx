import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { RefreshCw } from 'lucide-react';

import { PageHeader } from '@/components/layout/page-header';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { hyperliquidApi, ApiError } from '@/lib/api/client';
import type {
  HlBlock, HlFundingBlock, HlPositionsBlock, HlRatioBlock,
  HlSnapshot, HlUnlocksBlock, HlAnnouncementsBlock,
} from '@/lib/types/hyperliquid';

const STATUS_LABEL: Record<string, { text: string; className: string }> = {
  ok: { text: '正常', className: 'bg-emerald-500/10 text-emerald-600' },
  partial: { text: '部分可用', className: 'bg-amber-500/10 text-amber-600' },
  unavailable: { text: '不可用', className: 'bg-rose-500/10 text-rose-600' },
};

const errorText = (error: unknown) => error instanceof ApiError ? error.detail || error.message : error instanceof Error ? error.message : '请求失败';

const dash = (value: string | null | undefined) => (value == null || value === '' ? '—' : value);
const signed = (value: string | null | undefined) =>
  value == null || value === '' ? '—' : `${Number(value) >= 0 ? '+' : ''}${value}`;

/** 小数字符串转百分数显示（如 "0.00001" → "0.00100%"）。 */
const pctFromFraction = (value: string | null | undefined, dp = 5) =>
  value == null || value === '' ? '—' : `${(Number(value) * 100).toFixed(dp)}%`;

/** 已是百分数数值的字符串（如 drift "1.50"、annualized "10.77"）。 */
const pctFromNumber = (value: string | null | undefined, signedFlag = false) =>
  value == null || value === '' ? '—' : `${signedFlag ? signed(value) : value}%`;

function BlockHeader({ title, block, hint }: { title: string; block: HlBlock; hint?: string }) {
  const status = STATUS_LABEL[block.status] ?? STATUS_LABEL.unavailable;
  return (
    <CardHeader className="pb-3">
      <div className="flex items-center justify-between gap-2">
        <CardTitle className="text-base">{title}</CardTitle>
        <Badge variant="secondary" className={status.className}>{status.text}</Badge>
      </div>
      {hint && <CardDescription>{hint}</CardDescription>}
    </CardHeader>
  );
}

function Diagnostics({ block }: { block: HlBlock }) {
  if (!block.diagnostics.length) return null;
  return (
    <ul className="mt-2 space-y-1 text-xs text-muted-foreground">
      {block.diagnostics.map((d, i) => <li key={i}>· {d}</li>)}
    </ul>
  );
}

function PositionsCard({ block }: { block: HlPositionsBlock }) {
  return (
    <Card>
      <BlockHeader title="仓位" block={block}
        hint="测试网账户持仓与 pair 状态；缺价格时不显示零名义。" />
      <CardContent className="space-y-3 text-sm">
        <div className="flex flex-wrap gap-x-8 gap-y-1">
          <span>权益 <span className="font-mono">{dash(block.equity)}</span> USDC</span>
          <span>名义比 <span className="font-mono">{dash(block.notional_ratio)}</span></span>
          <span>漂移 <span className="font-mono">{pctFromNumber(block.ratio_drift)}</span></span>
          <span>动作状态 <span className="font-mono">{dash(block.action_state)}</span></span>
          <span>挂单数 <span className="font-mono">{block.open_orders ?? '—'}</span></span>
        </div>
        {block.positions.length > 0 && (
          <table className="w-full text-sm">
            <thead><tr className="text-left text-xs text-muted-foreground">
              <th className="py-1">币种</th><th>方向</th><th>数量</th><th>标记价</th>
              <th>当前名义</th><th>未实现盈亏</th>
            </tr></thead>
            <tbody>{block.positions.map((p, i) => (
              <tr key={i} className="border-t">
                <td className="py-1.5 font-medium">{p.coin}</td>
                <td>{p.side === 'long' ? '多' : '空'}</td>
                <td className="font-mono">{p.size}</td>
                <td className="font-mono">{dash(p.mark_px)}</td>
                <td className="font-mono">{p.notional == null ? '—（缺价格）' : `${p.notional} USD`}</td>
                <td className={`font-mono ${Number(p.unrealized_pnl) >= 0 ? 'text-emerald-600' : 'text-rose-600'}`}>
                  {signed(p.unrealized_pnl)}
                </td>
              </tr>
            ))}</tbody>
          </table>
        )}
        <Diagnostics block={block} />
      </CardContent>
    </Card>
  );
}

function FundingCard({ block }: { block: HlFundingBlock }) {
  const coins = Object.keys(block.hourly_rates);
  return (
    <Card>
      <BlockHeader title="资金费" block={block} hint="市场费率、日净收付估算与实际收付分别标注。" />
      <CardContent className="space-y-3 text-sm">
        <div className="space-y-1">
          {coins.map((coin) => (
            <div key={coin} className="flex gap-6">
              <span className="w-12 font-medium">{coin}</span>
              <span className="font-mono">{pctFromFraction(block.hourly_rates[coin])}/h</span>
              <span className="text-muted-foreground">年化 <span className="font-mono">{pctFromNumber(block.annualized?.[coin])}</span></span>
            </div>
          ))}
        </div>
        <p>
          日净收付估算（费率不变）：
          {block.daily_estimate
            ? <span className="font-mono"> {block.daily_estimate.direction === 'receive' ? '收' : '付'} {block.daily_estimate.amount} USDC</span>
            : ' —'}
        </p>
        <div>
          <p>过去 48 小时实际净收付：<span className="font-mono">{signed(block.actual_48h_net)} USDC</span>
            {block.window_complete === false && <span className="ml-2 text-xs text-amber-600">窗口不完整</span>}</p>
          {(block.window_start && block.window_end) && (
            <p className="text-xs text-muted-foreground">窗口 {block.window_start} → {block.window_end}</p>
          )}
          {block.recent_payments.length > 0 && (
            <ul className="mt-1 space-y-0.5 text-xs text-muted-foreground">
              {block.recent_payments.map((p, i) => (
                <li key={i} className="font-mono">
                  {p.time} {p.coin} {signed(p.usdc)} USDC（费率 {pctFromFraction(p.rate)}/h）
                </li>
              ))}
            </ul>
          )}
        </div>
        <Diagnostics block={block} />
      </CardContent>
    </Card>
  );
}

function RatioCard({ block }: { block: HlRatioBlock }) {
  const above = block.vs_ma30_pct != null && Number(block.vs_ma30_pct) >= 0;
  return (
    <Card>
      <BlockHeader title="HYPE/BTC" block={block} hint="UTC 已收盘日线对齐；相对 30MA 位置。" />
      <CardContent className="space-y-2 text-sm">
        <div className="flex gap-6">
          <span>当前 <span className="font-mono">{dash(block.current)}</span></span>
          <span>30MA <span className="font-mono">{dash(block.ma30)}</span></span>
          <span className={above ? 'text-emerald-600' : 'text-rose-600'}>
            {block.vs_ma30_pct == null ? '—' : `${above ? '上方' : '下方'} ${pctFromNumber(block.vs_ma30_pct)}`}
          </span>
        </div>
        <p className="text-xs text-muted-foreground">截至 {dash(block.as_of_date)} UTC 收盘</p>
        <Diagnostics block={block} />
      </CardContent>
    </Card>
  );
}

function UnlocksCard({ block }: { block: HlUnlocksBlock }) {
  return (
    <Card>
      <BlockHeader title="解锁锚点" block={block} hint="人工配置并核实的未来 7 天锚点。" />
      <CardContent className="space-y-2 text-sm">
        {block.anchors.length > 0 ? (
          <ul className="space-y-1.5">
            {block.anchors.map((a, i) => (
              <li key={i}>
                <span className="font-mono">{a.date}</span>（{a.days_ahead} 天后）{a.label}{' '}
                <a className="text-primary underline" href={a.source_url} target="_blank" rel="noopener noreferrer">来源</a>
                <span className="text-xs text-muted-foreground">（{a.verified_utc} 人工核实）</span>
              </li>
            ))}
          </ul>
        ) : <p className="text-muted-foreground">未来 7 天没有已核实的锚点</p>}
        {block.note && <p className="text-xs text-muted-foreground">{block.note}</p>}
        <Diagnostics block={block} />
      </CardContent>
    </Card>
  );
}

function AnnouncementsCard({ block }: { block: HlAnnouncementsBlock }) {
  return (
    <Card>
      <BlockHeader title="官方动态" block={block} hint="Telegram 公开预览，仅最近可见消息，非完整历史。" />
      <CardContent className="space-y-2 text-sm">
        {block.items.length > 0 ? (
          <ul className="space-y-2">
            {block.items.map((a, i) => (
              <li key={i}>
                <Badge variant="outline" className="mr-2">{a.kind === 'api' ? 'API' : '公告'}</Badge>
                <a className="text-primary underline" href={a.url} target="_blank" rel="noopener noreferrer">{a.text}</a>
                <span className="ml-2 text-xs text-muted-foreground">{a.published_at ?? '时间未知'}</span>
              </li>
            ))}
          </ul>
        ) : <p className="text-muted-foreground">暂无可见公告</p>}
        <p className="text-xs text-muted-foreground">查询时间 {dash(block.queried_at)}</p>
        <Diagnostics block={block} />
      </CardContent>
    </Card>
  );
}

function formatTime(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString('zh-CN', { hour12: false });
}

export function HyperliquidPage() {
  const query = useQuery({
    queryKey: ['hyperliquid-dashboard'],
    queryFn: hyperliquidApi.dashboard,
    staleTime: 30_000,
    refetchInterval: false,
    retry: false,
    placeholderData: keepPreviousData,
  });
  const snapshot: HlSnapshot | undefined = query.data;

  return (
    <div className="space-y-4">
      <PageHeader icon={RefreshCw} title="Hyperliquid"
        description="测试网只读快照：仓位、资金费、HYPE/BTC 比价、解锁锚点与官方动态。后端 30 秒缓存，页面不自动轮询。">
        <Button variant="outline" onClick={() => query.refetch()} disabled={query.isFetching}>
          <RefreshCw className={`w-4 h-4 mr-2 ${query.isFetching ? 'animate-spin' : ''}`} />
          {query.isFetching ? '刷新中…' : '刷新'}
        </Button>
      </PageHeader>

      {query.isError && !snapshot && (
        <Card><CardContent className="py-10 text-center text-rose-600" role="alert">
          Hyperliquid 看板加载失败：{errorText(query.error)}
        </CardContent></Card>
      )}

      {snapshot && (
        <>
          {query.isError && (
            <div role="alert" className="text-sm text-amber-600">
              刷新失败（{errorText(query.error)}），以下为上次的读数（{formatTime(snapshot.queried_at)}）。
            </div>
          )}
          <div className="flex flex-wrap items-center gap-x-6 gap-y-1 text-sm text-muted-foreground">
            <Badge variant="secondary" className="bg-sky-500/10 text-sky-600">测试网</Badge>
            <span>账户 <span className="font-mono">{snapshot.account_address.slice(0, 10)}…{snapshot.account_address.slice(-6)}</span></span>
            <span>数据查询时间 <span className="font-mono">{formatTime(snapshot.queried_at)}</span></span>
            {query.isFetching && <span>刷新中，保留当前读数…</span>}
          </div>

          <div className="grid gap-4 lg:grid-cols-2">
            <div className="lg:col-span-2"><PositionsCard block={snapshot.blocks.positions} /></div>
            <FundingCard block={snapshot.blocks.funding} />
            <RatioCard block={snapshot.blocks.ratio} />
            <UnlocksCard block={snapshot.blocks.unlocks} />
            <AnnouncementsCard block={snapshot.blocks.announcements} />
          </div>
        </>
      )}
    </div>
  );
}
