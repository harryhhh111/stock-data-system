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
import type { WatchItem, WatchQuote, WatchSuggestion } from '@/lib/types/watchlist';

const markets: Market[] = ['US', 'CN_HK', 'CN_A'];
const currency = { US: 'USD', CN_HK: 'HKD', CN_A: 'CNY' };
const errorText = (error: unknown) => error instanceof ApiError ? error.detail || error.message : error instanceof Error ? error.message : '请求失败';

function ResearchHint({ suggestion }: { suggestion: WatchSuggestion }) {
  const research = suggestion.research;
  const names: Record<string, string> = { thesis: '核心判断', risks: '风险', metrics: '跟踪指标', open_questions: '待验证问题' };
  return <span className="text-xs text-muted-foreground">
    {suggestion.source === 'research_doc' ? `基于研究：${suggestion.docs.join('、')}（草稿可修改）` : '该证券未配置研究入口，已生成通用草稿（可修改）'}
    {research && <><span className="block">来源版本 {research.commit_sha.slice(0, 8)}；不代表研究数据已更新</span>
      {research.missing_sections.length > 0 && <span className="block">未配置：{research.missing_sections.map(key => names[key] || key).join('、')}</span>}
      {research.sections.some(section => section.truncated) && <span className="block">部分章节已截断，仅使用摘录</span>}</>}
  </span>;
}

function Sparkline({ quote }: { quote: WatchQuote | undefined }) {
  if (quote?.status === 'review_price_basis') return <span className="text-muted-foreground">价格口径待核对</span>;
  if (!quote || quote.trend.length === 0) return <span className="text-muted-foreground">暂无走势数据</span>;
  if (quote.trend.length < 2) return <span className="text-muted-foreground">仅有 {quote.trend.length} 日记录，历史不足</span>;
  const values = quote.trend.map(p => p.close);
  const valid = values.filter((p): p is number => p != null);
  if (valid.length < 2) return <span className="text-muted-foreground">共 {values.length} 条日线，有效收盘不足 2 条</span>;
  const min = Math.min(...valid), max = Math.max(...valid);
  const segments: string[][] = [[]];
  values.forEach((v, i) => { if (v == null) segments.push([]); else segments[segments.length - 1].push(`${i * 110 / (values.length - 1)},${30 - (v - min) / (max - min || 1) * 26}`); });
  const window = `${quote.trend[0].date} 至 ${quote.trend[quote.trend.length - 1].date}`;
  return <div><svg viewBox="0 0 112 34" className="w-28 h-9 text-primary" role="img" aria-label={`${quote.trend.length}条交易日线，${window}，未复权收盘价走势`}>
    <title>{window}；{quote.trend.length} 条入库日线，非实时行情</title>
    {segments.map((s, i) => <polyline key={i} points={s.join(' ')} fill="none" stroke="currentColor" strokeWidth="1.8" />)}
  </svg><div className="text-xs text-muted-foreground whitespace-nowrap">{quote.trend.length}/20 日记录{valid.length < values.length && ` · 有效 ${valid.length} 日`}</div><div className="text-xs text-muted-foreground whitespace-nowrap">{quote.trend[0].date}<br />至 {quote.trend[quote.trend.length - 1].date}</div></div>;
}

export function WatchlistPage() {
  const qc = useQueryClient();
  const [market, setMarket] = useState<Market | 'all'>('all');
  const [group, setGroup] = useState('');
  const [sort, setSort] = useState('default');
  const [editor, setEditor] = useState<Partial<WatchItem> | null>(null);
  const [actionError, setActionError] = useState('');
  const [expandedNotes, setExpandedNotes] = useState<Set<number>>(new Set());
  const toggleNote = (id: number) => setExpandedNotes(prev => {
    const next = new Set(prev);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  });
  const itemsQuery = useQuery({ queryKey: ['watchlist'], queryFn: watchlistApi.list });
  const items = itemsQuery.data ?? [];
  const sessionQuery = useQuery({ queryKey: ['watchlist-session'], queryFn: watchlistApi.session, staleTime: 60_000, retry: false });
  const canEdit = sessionQuery.data?.authenticated === true;
  const [loginToken, setLoginToken] = useState('');
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
  const save = useMutation({ mutationFn: (item: Partial<WatchItem>) => watchlistApi.save(item, item.id), onSuccess: () => { setEditor(null); setActionError(''); qc.invalidateQueries({ queryKey: ['watchlist'] }); }, onError: e => setActionError(errorText(e)) });
  const remove = useMutation({ mutationFn: (id: number) => watchlistApi.remove(id), onSuccess: () => { setActionError(''); qc.invalidateQueries({ queryKey: ['watchlist'] }); }, onError: e => setActionError(errorText(e)) });
  const suggest = useMutation({ mutationFn: (v: { market: Market; stock_code: string; stock_name: string }) => watchlistApi.suggest(v), onSuccess: (r, v) => { setActionError(''); setEditor(prev => prev && prev.stock_code === v.stock_code && (prev.market || 'US') === v.market ? { ...prev, group_name: r.group || prev.group_name, note: r.note || prev.note } : prev); }, onError: e => setActionError(errorText(e)) });
  const activeSuggestion = suggest.variables?.stock_code === editor?.stock_code && suggest.variables?.market === (editor?.market || 'US') ? suggest.data : undefined;
  const login = useMutation({ mutationFn: (t: string) => watchlistApi.login(t), onSuccess: () => { setLoginToken(''); setActionError(''); qc.invalidateQueries({ queryKey: ['watchlist-session'] }); }, onError: e => setActionError(errorText(e)) });
  const logout = useMutation({ mutationFn: () => watchlistApi.logout(), onSuccess: () => { qc.invalidateQueries({ queryKey: ['watchlist-session'] }); }, onError: e => setActionError(errorText(e)) });
  const selectStyle = 'h-9 rounded-md border bg-background px-3 text-sm';
  return <div className="space-y-6">
    <PageHeader icon={Star} title="关注股" description="你的关注公司、研究理由与价格变化，放在同一个看板。"><Button variant="outline" onClick={() => { qc.invalidateQueries({ queryKey: ['watchlist'] }); qc.invalidateQueries({ queryKey: ['watchlist-quotes'] }); }}><RefreshCw className="w-4 h-4 mr-2" />刷新</Button></PageHeader>
    <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">{[['关注股票', items.length], ['研究分组', groups.length], ['行情口径', '日线收盘']].map(([label, value]) => <Card key={label}><CardContent className="pt-5"><p className="text-sm text-muted-foreground">{label}</p><p className="text-2xl font-semibold mt-1">{value}</p></CardContent></Card>)}</div>
    <div className="flex flex-wrap gap-3 items-center">
      <select aria-label="筛选市场" className={selectStyle} value={market} onChange={e => setMarket(e.target.value as Market | 'all')}><option value="all">全部市场</option>{markets.map(m => <option key={m}>{m}</option>)}</select>
      <select aria-label="筛选分组" className={selectStyle} value={group} onChange={e => setGroup(e.target.value)}><option value="">全部分组</option>{groups.map(g => <option key={g}>{g}</option>)}</select>
      <select aria-label="排序" className={selectStyle} value={sort} onChange={e => setSort(e.target.value)}><option value="default">添加顺序</option><option value="up">涨幅优先</option><option value="down">跌幅优先</option></select>
      <Button onClick={() => { setActionError(''); setEditor({ group_name: '', note: '', research_url: '' }); }}>添加股票</Button>
      <details className="text-sm ml-auto"><summary className="cursor-pointer text-muted-foreground"><LockKeyhole className="inline w-4 h-4 mr-1" />{canEdit ? '管理员已登录' : '管理员登录'}</summary>{canEdit
        ? <div className="mt-2 flex items-center gap-2"><span className="text-xs text-muted-foreground">30 天内免输凭据</span><Button variant="outline" size="sm" disabled={logout.isPending} onClick={() => logout.mutate()}>退出登录</Button></div>
        : <div className="mt-2 flex items-center gap-2"><Input className="w-56" type="password" autoComplete="off" aria-label="管理员凭据" value={loginToken} onChange={e => setLoginToken(e.target.value)} placeholder="输入编辑凭据登录" /><Button variant="outline" size="sm" disabled={!loginToken || login.isPending} onClick={() => login.mutate(loginToken)}>{login.isPending ? '登录中…' : '登录'}</Button>{login.error && <span className="text-xs text-destructive">{errorText(login.error)}</span>}</div>}
      </details>
    </div>
    <div className="space-y-1 text-xs text-muted-foreground"><p>行情口径：未复权日线收盘价，非盘中实时行情；每 5 分钟刷新读取数据库，不触发同步。</p><p>涨跌幅 =（最新收盘价 ÷ 前一条入库日线收盘价 − 1）× 100%，比较日期见每行；不是 20 日累计涨跌幅。走势最多展示近 20 条交易日线，缺失日期不补齐。</p><p>原始价格未校验拆股/除权；超过 4 个自然日未更新会提示滞后。</p></div>
    {itemsQuery.isPending && <p>正在读取关注列表…</p>}
    {itemsQuery.error && <div role="alert" className="text-destructive">{errorText(itemsQuery.error)}</div>}
    {actionError && !editor && <div role="alert" className="text-destructive">{actionError}</div>}
    {itemsQuery.isSuccess && !items.length && <Card><CardContent className="py-16 text-center"><Star className="w-9 h-9 mx-auto mb-4 text-muted-foreground" /><h3 className="text-lg font-medium">从第一只关注股开始</h3><p className="text-muted-foreground mt-2">添加你正在研究的公司，记录为什么关注它。</p></CardContent></Card>}
    {items.length > 0 && <div className="border rounded-lg bg-card overflow-x-auto"><Table><TableHeader><TableRow>{['股票', '分组', '最新收盘价', '涨跌幅（较前次收盘）', '近 20 个交易日走势', '最新行情日期', '关注理由', '操作'].map(h => <TableHead key={h}>{h}</TableHead>)}</TableRow></TableHeader><TableBody>{visible.map(item => {
      const quote = quotes.get(`${item.market}:${item.stock_code}`), response = quoteQueries[markets.indexOf(item.market)];
      return <TableRow key={item.id}>
        <TableCell><Link className="font-medium hover:text-primary" to={`/analyzer?${new URLSearchParams({ code: item.stock_code, market: item.market })}`}>{item.stock_name}</Link><div className="text-xs text-muted-foreground">{item.stock_code} · {item.market}</div></TableCell>
        <TableCell>{item.group_name || '未分组'}</TableCell>
        <TableCell className="font-mono whitespace-nowrap">{quote?.close?.toLocaleString('en-US', { maximumFractionDigits: 4 }) ?? '—'} <span className="text-xs text-muted-foreground">{currency[item.market]}</span></TableCell>
        <TableCell className="min-w-44"><div className={quote?.change_pct == null ? 'text-muted-foreground' : quote.change_pct >= 0 ? 'text-emerald-500' : 'text-rose-500'}>{quote?.change_pct == null ? '—' : `${quote.change_pct >= 0 ? '+' : ''}${quote.change_pct.toFixed(2)}%`}</div><div className="text-xs text-muted-foreground whitespace-nowrap">{quote?.status === 'review_price_basis' ? '价格口径待核对' : quote?.change_pct == null ? '暂无可比收盘价' : quote.previous_quote_date && quote.quote_date ? `${quote.previous_quote_date} → ${quote.quote_date}` : '比较日期未提供'}</div></TableCell>
        <TableCell><Sparkline quote={quote} /></TableCell>
        <TableCell className="min-w-40"><div>{quote?.quote_date || '—'}</div><div className="text-xs text-muted-foreground max-w-52">{response?.error ? errorText(response.error) : response?.isPending ? '读取行情中…' : quote?.status !== 'ok' ? quote?.warning : ''}{quote?.stale && ' · 行情滞后'}</div></TableCell>
        <TableCell className="min-w-48 max-w-72">{item.note
          ? <button type="button" onClick={() => toggleNote(item.id)} className="text-left w-full max-w-72 group" aria-expanded={expandedNotes.has(item.id)} aria-label={`展开或收起${item.stock_code}的关注理由`}>
              <span className={expandedNotes.has(item.id) ? 'block whitespace-pre-wrap' : 'line-clamp-3'}>{item.note}</span>
              <span className="text-xs text-muted-foreground group-hover:text-foreground">{expandedNotes.has(item.id) ? '收起 ▲' : '展开 ▼'}</span>
            </button>
          : <span className="text-muted-foreground">补充关注理由</span>}</TableCell>
        <TableCell><div className="flex gap-1">{item.research_url && <Button asChild variant="ghost" size="icon"><a href={item.research_url} target="_blank" rel="noopener noreferrer" aria-label="打开研究"><ExternalLink className="w-4 h-4" /></a></Button>}<Button variant="ghost" size="icon" aria-label={`编辑${item.stock_code}`} onClick={() => { setActionError(''); setEditor(item); }}><Pencil className="w-4 h-4" /></Button><Button variant="ghost" size="icon" disabled={remove.isPending || !canEdit} aria-label={`移除${item.stock_code}`} onClick={() => { if (window.confirm(`从关注列表移除 ${item.stock_name}？`)) remove.mutate(item.id); }}><Trash2 className="w-4 h-4" /></Button></div></TableCell>
      </TableRow>;
    })}</TableBody></Table>{!visible.length && <p className="p-8 text-center text-muted-foreground">该筛选条件下没有关注股</p>}</div>}
    <Dialog open={editor != null} onOpenChange={open => { if (!open && !save.isPending) setEditor(null); }}><DialogContent className="max-h-[90vh] overflow-y-auto" onPointerDownOutside={e => e.preventDefault()} onEscapeKeyDown={e => e.preventDefault()}><DialogHeader><DialogTitle>{editor?.id ? '编辑关注股' : '添加关注股'}</DialogTitle></DialogHeader>{editor && <form className="space-y-4" onSubmit={e => { e.preventDefault(); if (editor.stock_code && canEdit) save.mutate(editor); }}>
      {!editor.id && <><select aria-label="搜索市场" className={selectStyle} value={editor.market || 'US'} onChange={e => setEditor({ ...editor, market: e.target.value as Market, stock_code: undefined, stock_name: undefined })}>{markets.map(m => <option key={m}>{m}</option>)}</select><StockSearch inline market={editor.market || 'US'} onSelect={stock => setEditor({ ...editor, ...stock })} /></>}
      {!editor.id && <details className="text-sm"><summary className="cursor-pointer text-muted-foreground">搜索不到？填写代码和名称</summary><div className="space-y-3 mt-3"><label className="block">股票代码<Input maxLength={20} value={editor.stock_code || ''} onChange={e => setEditor({ ...editor, market: editor.market || 'US', stock_code: e.target.value.toUpperCase() })} placeholder="例如 NBIS" /></label><label className="block">公司名称<Input maxLength={200} value={editor.stock_name || ''} onChange={e => setEditor({ ...editor, market: editor.market || 'US', stock_name: e.target.value })} placeholder="填写你研究的公司名称" /></label><p className="text-xs text-muted-foreground">代码须按所选市场填写，系统不校验证券身份；没有入库行情时显示缺失，不自动同步。</p></div></details>}
      {editor.stock_code && <p className="font-medium">{editor.stock_name} · {editor.stock_code} · {editor.market}</p>}
      {editor.stock_code && editor.stock_name?.trim() && <div className="flex items-center gap-3">
        <Button type="button" variant="outline" size="sm" disabled={!canEdit || suggest.isPending} onClick={() => { setActionError(''); suggest.mutate({ market: editor.market || 'US', stock_code: editor.stock_code!, stock_name: editor.stock_name!.trim() }); }}>{suggest.isPending ? '生成中…' : 'AI 生成建议'}</Button>
        {!canEdit && <span className="text-xs text-muted-foreground">需要先点页面右上角「管理员登录」</span>}
        {activeSuggestion && <ResearchHint suggestion={activeSuggestion} />}
        {suggest.error && <span className="text-xs text-destructive">{errorText(suggest.error)}</span>}
      </div>}
      <label className="block text-sm">分组<Input className="mt-1" maxLength={80} value={editor.group_name || ''} onChange={e => setEditor({ ...editor, group_name: e.target.value })} placeholder="例如 AI 云、存储、消费" /></label>
      <label className="block text-sm">关注理由<textarea className="mt-1 w-full rounded-md border bg-background p-3 min-h-24" maxLength={2000} value={editor.note || ''} onChange={e => setEditor({ ...editor, note: e.target.value })} placeholder="为什么关注？目前最想验证什么？" /></label>
      <label className="block text-sm">研究链接（可选）<Input className="mt-1" type="url" maxLength={2000} value={editor.research_url || ''} onChange={e => setEditor({ ...editor, research_url: e.target.value })} placeholder="https://github.com/…" /></label>
      {!canEdit && <p className="text-xs text-muted-foreground">保存需要管理员登录（页面右上角）。</p>}
      {actionError && <p role="alert" className="text-destructive text-sm">{actionError}</p>}
      <Button type="submit" disabled={!editor.stock_code || !editor.stock_name?.trim() || !canEdit || save.isPending}>{save.isPending ? '保存中…' : '保存关注股'}</Button>
    </form>}</DialogContent></Dialog>
  </div>;
}
