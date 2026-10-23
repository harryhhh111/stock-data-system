import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Newspaper, ExternalLink, RefreshCw } from 'lucide-react';
import { ApiError, newsApi, watchlistApi } from '@/lib/api/client';
import { PageHeader } from '@/components/layout/page-header';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import type { NewsEntry } from '@/lib/types/news';

const errorText = (error: unknown) => error instanceof ApiError ? error.detail || error.message : error instanceof Error ? error.message : '请求失败';

// 时间一律北京时间展示并显式标注；仅日期不补午夜；SEC 标「SEC 接收时间」。
function bjTime(iso: string | null) {
  if (!iso) return null;
  return new Date(iso).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false });
}

function entryTimeText(e: NewsEntry) {
  if (e.time_unknown) return `时间未知，按发现时间 ${bjTime(e.first_observed_at) ?? '—'}（北京时间）`;
  if (e.date_only) return `${e.published_raw.slice(0, 10)}（仅日期，来源时区 ${e.source_timezone || '未知'}）`;
  const label = e.source_kind === 'sec' ? 'SEC 接收时间' : '发布时间';
  return `${bjTime(e.published_at)}（北京时间，${label}）`;
}

const KIND_BADGE: Record<string, string> = { sec: '官方披露', ir: '官方发布', media: '媒体' };

function safeHttpUrl(url: string) {
  return /^https?:\/\//.test(url) ? url : null;
}

function NewsCard({ entry }: { entry: NewsEntry }) {
  const [showSources, setShowSources] = useState(false);
  const link = safeHttpUrl(entry.url);
  // v1.1：中文标题/摘要为主，带「AI 译」标签；无中文回退原文标题；
  // 原标题始终可对照（展开）
  return <Card>
    <CardContent className="py-4">
      <div className="text-sm font-medium leading-snug">
        {entry.importance === 'high' && <span className="inline-flex items-center rounded-sm bg-amber-100 text-amber-800 px-1.5 py-0.5 text-xs mr-1.5 align-middle">重要</span>}
        {link ? <a href={link} target="_blank" rel="noopener noreferrer" className="hover:text-primary">{entry.title_zh || entry.title}<ExternalLink className="inline w-3 h-3 ml-1" /></a> : (entry.title_zh || entry.title)}
        {entry.title_zh && <span className="inline-flex items-center rounded-sm border px-1 py-0.5 text-xs text-muted-foreground ml-1.5 align-middle">AI 译</span>}
      </div>
      {entry.summary_zh && <p className="text-xs mt-1">{entry.summary_zh}<span className="text-muted-foreground">（AI 译）</span></p>}
      {!entry.summary_zh && entry.excerpt && <p className="text-xs text-muted-foreground mt-1 line-clamp-2">{entry.excerpt}</p>}
      {entry.title_zh && <details className="text-xs text-muted-foreground mt-1"><summary className="cursor-pointer">原标题对照</summary><p className="mt-1">{entry.title}</p></details>}
      <div className="text-xs text-muted-foreground mt-2 flex flex-wrap gap-x-2 gap-y-1 items-center">
        <span className="inline-flex items-center rounded-sm border px-1.5 py-0.5">{KIND_BADGE[entry.source_kind] ?? entry.source_kind}</span>
        {entry.media_source && <span>{entry.media_source}</span>}
        <span className="font-medium text-foreground">{entry.stock_code}</span>
        {entry.content_nature === 'unclassified' && <span>内容性质未分类</span>}
        {entry.is_backfill && <span>回补</span>}
        {entry.fold_count > 1 && <button type="button" className="underline underline-offset-2" onClick={() => setShowSources(!showSources)}>
          {entry.fold_count} 个来源{showSources ? ' ▲' : ' ▼'}
        </button>}
      </div>
      <div className="text-xs text-muted-foreground mt-1">{entryTimeText(entry)}</div>
      {entry.relation_reason && <div className="text-xs text-muted-foreground">关联：{entry.relation_reason}</div>}
      {showSources && entry.fold_sources.length > 0 && <ul className="mt-1 space-y-0.5">
        {entry.fold_sources.map((s, i) => <li key={i} className="text-xs">
          {safeHttpUrl(s.url)
            ? <a href={s.url} target="_blank" rel="noopener noreferrer" className="text-primary hover:underline">{s.name || s.url}</a>
            : <span>{s.name}</span>}
          <span className="text-muted-foreground">{s.published_at ? ` · ${bjTime(s.published_at)}（北京时间）` : ''}</span>
        </li>)}
      </ul>}
    </CardContent>
  </Card>;
}

export function NewsPage() {
  const [stock, setStock] = useState('');
  const [kind, setKind] = useState<'all' | 'official' | 'media'>('all');
  const [dayOffset, setDayOffset] = useState(0);  // 0=今天（北京时间）
  const pinnedQuery = useQuery({ queryKey: ['news-pinned'], queryFn: newsApi.pinned, staleTime: 60_000, refetchInterval: 300_000, retry: 1 });
  // 默认（dayOffset=null 语义）拉最近 7 日时间流；选中某天则单日
  const feedQuery = useQuery({
    queryKey: ['news-feed', dayOffset, stock, kind],
    queryFn: () => {
      if (dayOffset < 0) return newsApi.feed({ stock, kind });  // 最近 7 日
      const d = new Date(Date.now() - dayOffset * 86400_000);
      const bj = new Date(d.toLocaleString('en-US', { timeZone: 'Asia/Shanghai' }));
      const date = `${bj.getFullYear()}-${String(bj.getMonth() + 1).padStart(2, '0')}-${String(bj.getDate()).padStart(2, '0')}`;
      return newsApi.feed({ date, stock, kind });
    },
    staleTime: 60_000, refetchInterval: 300_000, retry: 1,
  });
  const feed = feedQuery.data;
  // 公司筛选选项用稳定的全量关注列表（US），不随筛选结果收窄
  const watchlistQuery = useQuery({ queryKey: ['watchlist'], queryFn: watchlistApi.list });
  const stocks = useMemo(() => [...new Set(
    (watchlistQuery.data ?? []).filter(i => i.market === 'US').map(i => i.stock_code),
  )].sort(), [watchlistQuery.data]);
  const byDay = useMemo(() => {
    const groups = new Map<string, NewsEntry[]>();
    (feed?.entries ?? []).forEach(e => {
      const day = e.bj_day ?? '日期未知';
      groups.set(day, [...(groups.get(day) ?? []), e]);
    });
    return [...groups.entries()].sort((a, b) => b[0].localeCompare(a[0]));
  }, [feed]);
  const pinned = pinnedQuery.data;
  const selectStyle = 'h-9 rounded-md border bg-background px-3 text-sm';
  return <div className="space-y-6">
    <PageHeader icon={Newspaper} title="每日资讯" description="关注公司的官方披露与媒体报道，按北京时间日期分组；原始标题不翻译、不做 AI 评分。">
      <Button variant="outline" onClick={() => { pinnedQuery.refetch(); feedQuery.refetch(); }}><RefreshCw className="w-4 h-4 mr-2" />刷新</Button>
    </PageHeader>

    {pinnedQuery.isSuccess && pinned?.initialized && pinned.entries.length > 0 && <section className="space-y-2">
      <h2 className="text-sm font-semibold">置顶（近 48 小时 · 重要性/多来源，封顶 10 条）{!pinned.ai_available && <span className="text-xs text-muted-foreground font-normal">（AI 暂不可用，按机械规则入选）</span>}</h2>
      <div className="grid gap-2 md:grid-cols-2">
        {pinned.entries.map((e, i) => <NewsCard key={i} entry={e} />)}
      </div>
    </section>}

    <div className="flex flex-wrap gap-3 items-center text-sm">
      <select aria-label="筛选公司" className={selectStyle} value={stock} onChange={e => setStock(e.target.value)}>
        <option value="">全部公司</option>
        {stocks.map(s => <option key={s}>{s}</option>)}
      </select>
      <select aria-label="筛选类型" className={selectStyle} value={kind} onChange={e => setKind(e.target.value as typeof kind)}>
        <option value="all">全部类型</option>
        <option value="official">官方披露/发布</option>
        <option value="media">媒体</option>
      </select>
      <select aria-label="选择日期" className={selectStyle} value={dayOffset} onChange={e => setDayOffset(Number(e.target.value))}>
        <option value={-1}>最近 7 日</option>
        {[0, 1, 2, 3, 4, 5, 6].map(d => <option key={d} value={d}>{d === 0 ? '今天' : `${d} 天前`}（北京时间）</option>)}
      </select>
    </div>

    {feedQuery.isPending && <p className="text-sm text-muted-foreground">读取资讯中…</p>}
    {feedQuery.error && <div role="alert" className="text-destructive">{errorText(feedQuery.error)}</div>}
    {feed && !feed.initialized && <p className="text-sm text-muted-foreground">{feed.message}</p>}
    {feed?.initialized && <>
      {!feed.announcements_enabled && <p className="text-xs text-muted-foreground">公告采集未启用（STOCK_ANNOUNCEMENTS_ENABLED=false）：以下为既有库存数据。</p>}
      {byDay.length === 0 && <Card><CardContent className="py-16 text-center text-muted-foreground">
        {feed.sources.some(s => s.state === 'ok') ? '已接入来源暂无资讯' : '来源未接入或不可用——见下方来源状态'}
      </CardContent></Card>}
      {byDay.map(([day, entries]) => <section key={day} className="space-y-2">
        <h2 className="text-sm font-semibold">{day} · {entries.length} 条（北京时间）</h2>
        <div className="grid gap-2 md:grid-cols-2">{entries.map((e, i) => <NewsCard key={i} entry={e} />)}</div>
      </section>)}
      {feed.sources.length > 0 && <section className="space-y-1 pt-4 border-t">
        <h2 className="text-sm font-semibold">来源状态</h2>
        {feed.sources.map((s, i) => <div key={i} className="text-xs text-muted-foreground">
          <span className="font-medium text-foreground">{s.platform ?? s.kind}</span> {s.message}
          {s.last_success_at && ` · 上次完整成功 ${bjTime(s.last_success_at)}（北京时间）`}
          {s.last_attempt_at && ` · 最近尝试 ${bjTime(s.last_attempt_at)}（北京时间）${s.last_attempt_status ? `：${s.last_attempt_status}` : ''}`}
          {s.next_due_at && ` · 下次检查 ${bjTime(s.next_due_at)}（北京时间）`}
        </div>)}
      </section>}
    </>}
  </div>;
}
