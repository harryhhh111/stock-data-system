import { useState } from 'react';
import { Link } from 'react-router-dom';
import { useMutation, useQueries, useQuery, useQueryClient } from '@tanstack/react-query';
import { Star, RefreshCw, Pencil, Trash2, ExternalLink, LockKeyhole } from 'lucide-react';
import { ApiError, watchlistApi } from '@/lib/api/client';
import { StockSearch } from '@/components/analyzer/stock-search';
import { PageHeader } from '@/components/layout/page-header';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import type { Market } from '@/lib/types/common';
import type { WatchItem, WatchQuote } from '@/lib/types/watchlist';

const markets: Market[] = ['US', 'CN_HK', 'CN_A'];
const currency = { US: 'USD', CN_HK: 'HKD', CN_A: 'CNY' };
const errorText = (error: unknown) => error instanceof ApiError ? error.detail || error.message : error instanceof Error ? error.message : '请求失败';

function Sparkline({ quote }: { quote: WatchQuote | undefined }) {
  if (!quote || quote.trend.length < 2) return <span className="text-muted-foreground">历史不足</span>;
  const values = quote.trend.map(p => p.close);
  const valid = values.filter((p): p is number => p != null);
  if (valid.length < 2) return <span className="text-muted-foreground">历史不足</span>;
  const min = Math.min(...valid), max = Math.max(...valid);
  const segments: string[][] = [[]];
  values.forEach((v, i) => { if (v == null) segments.push([]); else segments[segments.length - 1].push(`${i * 110 / (values.length - 1)},${30 - (v - min) / (max - min || 1) * 26}`); });
  return <svg viewBox="0 0 112 34" className="w-28 h-9 text-primary" role="img" aria-label={`${quote.trend.length}个交易日原始收盘走势`}>
    {segments.map((s, i) => <polyline key={i} points={s.join(' ')} fill="none" stroke="currentColor" strokeWidth="1.8" />)}
  </svg>;
}

export function WatchlistPage() {
  const qc = useQueryClient();
  const [market, setMarket] = useState<Market | 'all'>('all');
  const [group, setGroup] = useState('');
  const [sort, setSort] = useState('default');
  const [token, setToken] = useState('');
  const [editor, setEditor] = useState<Partial<WatchItem> | null>(null);
  const [actionError, setActionError] = useState('');
  const itemsQuery = useQuery({ queryKey: ['watchlist'], queryFn: watchlistApi.list });
  const items = itemsQuery.data ?? [];
  const quoteQueries = useQueries({ queries: markets.map(m => {
    const codes = items.filter(i => i.market === m).map(i => i.stock_code).sort();
    return { queryKey: ['watchlist-quotes', m, codes], queryFn: () => watchlistApi.quotes(m, codes), enabled: codes.length > 0, staleTime: 60_000, refetchInterval: 300_000, retry: 1 };
  }) });
  const quotes = new Map<string, WatchQuote>();
  quoteQueries.forEach((q, i) => q.data?.quotes.forEach(v => quotes.set(`${markets[i]}:${v.stock_code}`, v)));
  const groups = [...new Set(items.map(i => i.group_name).filter(Boolean))].sort();
  const visible = items.filter(i => (market === 'all' || i.market === market) && (!group || i.group_name === group));
  if (sort !== 'default') visible.sort((a, b) => {
    const av = quotes.get(`${a.market}:${a.stock_code}`)?.change_pct, bv = quotes.get(`${b.market}:${b.stock_code}`)?.change_pct;
    if (av == null) return bv == null ? 0 : 1;
    if (bv == null) return -1;
    return sort === 'up' ? bv - av : av - bv;
  });
  const save = useMutation({ mutationFn: (item: Partial<WatchItem>) => watchlistApi.save(item, token, item.id), onSuccess: () => { setEditor(null); setActionError(''); qc.invalidateQueries({ queryKey: ['watchlist'] }); }, onError: e => setActionError(errorText(e)) });
  const remove = useMutation({ mutationFn: (id: number) => watchlistApi.remove(id, token), onSuccess: () => { setActionError(''); qc.invalidateQueries({ queryKey: ['watchlist'] }); }, onError: e => setActionError(errorText(e)) });
  const selectStyle = 'h-9 rounded-md border bg-background px-3 text-sm';
  return <div className="space-y-6">
    <PageHeader icon={Star} title="关注股" description="你的关注公司、研究理由与价格变化，放在同一个看板。"><Button variant="outline" onClick={() => { qc.invalidateQueries({ queryKey: ['watchlist'] }); qc.invalidateQueries({ queryKey: ['watchlist-quotes'] }); }}><RefreshCw className="w-4 h-4 mr-2" />刷新</Button></PageHeader>
    <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">{[['关注股票', items.length], ['研究分组', groups.length], ['行情口径', '日线收盘']].map(([label, value]) => <Card key={label}><CardContent className="pt-5"><p className="text-sm text-muted-foreground">{label}</p><p className="text-2xl font-semibold mt-1">{value}</p></CardContent></Card>)}</div>
    <div className="flex flex-wrap gap-3 items-center">
      <select aria-label="筛选市场" className={selectStyle} value={market} onChange={e => setMarket(e.target.value as Market | 'all')}><option value="all">全部市场</option>{markets.map(m => <option key={m}>{m}</option>)}</select>
      <select aria-label="筛选分组" className={selectStyle} value={group} onChange={e => setGroup(e.target.value)}><option value="">全部分组</option>{groups.map(g => <option key={g}>{g}</option>)}</select>
      <select aria-label="排序" className={selectStyle} value={sort} onChange={e => setSort(e.target.value)}><option value="default">添加顺序</option><option value="up">涨幅优先</option><option value="down">跌幅优先</option></select>
      <Button onClick={() => { setActionError(''); setEditor({ group_name: '', note: '', research_url: '' }); }}>添加股票</Button>
      <details className="text-sm ml-auto"><summary className="cursor-pointer text-muted-foreground"><LockKeyhole className="inline w-4 h-4 mr-1" />编辑凭据</summary><Input className="mt-2 w-64" type="password" autoComplete="off" aria-label="管理员编辑凭据" value={token} onChange={e => setToken(e.target.value)} placeholder="仅在本页保留" /></details>
    </div>
    <p className="text-xs text-muted-foreground">最新已入库收盘行情，每5分钟刷新读取。原始日线未校验拆股/除权；超过4个自然日未更新会提示滞后。</p>
    {itemsQuery.isPending && <p>正在读取关注列表…</p>}
    {itemsQuery.error && <div role="alert" className="text-destructive">{errorText(itemsQuery.error)}</div>}
    {actionError && !editor && <div role="alert" className="text-destructive">{actionError}</div>}
    {itemsQuery.isSuccess && !items.length && <Card><CardContent className="py-16 text-center"><Star className="w-9 h-9 mx-auto mb-4 text-muted-foreground" /><h3 className="text-lg font-medium">从第一只关注股开始</h3><p className="text-muted-foreground mt-2">添加你正在研究的公司，记录为什么关注它。</p></CardContent></Card>}
    {items.length > 0 && <div className="border rounded-lg bg-card overflow-x-auto"><Table><TableHeader><TableRow>{['股票', '分组', '最近收盘', '涨跌幅', '近期走势', '行情日期', '关注理由', '操作'].map(h => <TableHead key={h}>{h}</TableHead>)}</TableRow></TableHeader><TableBody>{visible.map(item => {
      const quote = quotes.get(`${item.market}:${item.stock_code}`), response = quoteQueries[markets.indexOf(item.market)];
      return <TableRow key={item.id}>
        <TableCell><Link className="font-medium hover:text-primary" to={`/analyzer?${new URLSearchParams({ code: item.stock_code, market: item.market })}`}>{item.stock_name}</Link><div className="text-xs text-muted-foreground">{item.stock_code} · {item.market}</div></TableCell>
        <TableCell>{item.group_name || '未分组'}</TableCell>
        <TableCell className="font-mono whitespace-nowrap">{quote?.close?.toLocaleString('en-US', { maximumFractionDigits: 4 }) ?? '—'} <span className="text-xs text-muted-foreground">{currency[item.market]}</span></TableCell>
        <TableCell className={quote?.change_pct != null && quote.change_pct >= 0 ? 'text-emerald-500' : 'text-rose-500'}>{quote?.change_pct == null ? '—' : `${quote.change_pct >= 0 ? '+' : ''}${quote.change_pct.toFixed(2)}%`}</TableCell>
        <TableCell><Sparkline quote={quote} /></TableCell>
        <TableCell className="min-w-40"><div>{quote?.quote_date || '—'}</div><div className="text-xs text-muted-foreground max-w-52">{response?.error ? errorText(response.error) : response?.isPending ? '读取行情中…' : quote?.status !== 'ok' ? quote?.warning : ''}{quote?.stale && ' · 行情滞后'}</div></TableCell>
        <TableCell className="min-w-48 max-w-72 whitespace-pre-wrap">{item.note || <span className="text-muted-foreground">补充关注理由</span>}</TableCell>
        <TableCell><div className="flex gap-1">{item.research_url && <Button asChild variant="ghost" size="icon"><a href={item.research_url} target="_blank" rel="noopener noreferrer" aria-label="打开研究"><ExternalLink className="w-4 h-4" /></a></Button>}<Button variant="ghost" size="icon" aria-label={`编辑${item.stock_code}`} onClick={() => { setActionError(''); setEditor(item); }}><Pencil className="w-4 h-4" /></Button><Button variant="ghost" size="icon" disabled={remove.isPending || !token} aria-label={`移除${item.stock_code}`} onClick={() => { if (window.confirm(`从关注列表移除 ${item.stock_name}？`)) remove.mutate(item.id); }}><Trash2 className="w-4 h-4" /></Button></div></TableCell>
      </TableRow>;
    })}</TableBody></Table>{!visible.length && <p className="p-8 text-center text-muted-foreground">该筛选条件下没有关注股</p>}</div>}
    <Dialog open={editor != null} onOpenChange={open => { if (!open && !save.isPending) setEditor(null); }}><DialogContent className="max-h-[90vh] overflow-y-auto"><DialogHeader><DialogTitle>{editor?.id ? '编辑关注股' : '添加关注股'}</DialogTitle></DialogHeader>{editor && <form className="space-y-4" onSubmit={e => { e.preventDefault(); if (editor.stock_code && token) save.mutate(editor); }}>
      {!editor.id && <><select aria-label="搜索市场" className={selectStyle} value={editor.market || 'US'} onChange={e => setEditor({ ...editor, market: e.target.value as Market, stock_code: undefined, stock_name: undefined })}>{markets.map(m => <option key={m}>{m}</option>)}</select><StockSearch inline market={editor.market || 'US'} onSelect={stock => setEditor({ ...editor, ...stock })} /></>}
      {!editor.id && <details className="text-sm"><summary className="cursor-pointer text-muted-foreground">搜索不到？填写代码和名称</summary><div className="space-y-3 mt-3"><label className="block">股票代码<Input maxLength={20} value={editor.stock_code || ''} onChange={e => setEditor({ ...editor, market: editor.market || 'US', stock_code: e.target.value.toUpperCase() })} placeholder="例如 NBIS" /></label><label className="block">公司名称<Input maxLength={200} value={editor.stock_name || ''} onChange={e => setEditor({ ...editor, market: editor.market || 'US', stock_name: e.target.value })} placeholder="填写你研究的公司名称" /></label><p className="text-xs text-muted-foreground">代码须按所选市场填写，系统不校验证券身份；没有入库行情时显示缺失，不自动同步。</p></div></details>}
      {editor.stock_code && <p className="font-medium">{editor.stock_name} · {editor.stock_code} · {editor.market}</p>}
      <label className="block text-sm">分组<Input className="mt-1" maxLength={80} value={editor.group_name || ''} onChange={e => setEditor({ ...editor, group_name: e.target.value })} placeholder="例如 AI 云、存储、消费" /></label>
      <label className="block text-sm">关注理由<textarea className="mt-1 w-full rounded-md border bg-background p-3 min-h-24" maxLength={2000} value={editor.note || ''} onChange={e => setEditor({ ...editor, note: e.target.value })} placeholder="为什么关注？目前最想验证什么？" /></label>
      <label className="block text-sm">研究链接（可选）<Input className="mt-1" type="url" maxLength={2000} value={editor.research_url || ''} onChange={e => setEditor({ ...editor, research_url: e.target.value })} placeholder="https://github.com/…" /></label>
      <label className="block text-sm">管理员编辑凭据<Input className="mt-1" type="password" autoComplete="off" value={token} onChange={e => setToken(e.target.value)} placeholder="仅保存在当前页面内存" /></label>
      {actionError && <p role="alert" className="text-destructive text-sm">{actionError}</p>}
      <Button type="submit" disabled={!editor.stock_code || !editor.stock_name?.trim() || !token || save.isPending}>{save.isPending ? '保存中…' : '保存关注股'}</Button>
    </form>}</DialogContent></Dialog>
  </div>;
}
