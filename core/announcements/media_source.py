"""core/announcements/media_source.py — 媒体来源适配（每日资讯 MVP 公司新闻层）。

合同：docs/WATCHLIST_DAILY_NEWS_MVP_PLAN.md（2026-10-22 审核通过）。
依据：docs/WATCHLIST_DAILY_NEWS_SOURCE_VALIDATION.md（线 A 终审归档）。

每股两个媒体来源：
- google_news_rss：按公司查询词（线 A 验证形态：公司全名 when:14d），
  固定入口 news.google.com/rss/search；硬约束：单查询 100 条上限、
  无分页、回溯约 7~10 天 → 每日按固定间隔落库，事后补不回来。
- finnhub：/company-news symbol 直连；from 取当日−1 天（线 A 指出 from
  日期不覆盖北京时间窗口起点，采集侧留冗余）；稳定 id 去重。

约束：
- 不占 SEC gate，不复用 sec_client；复用 ir_source 的 HTTP 层
  （单次请求/轮/源，无重试循环，超时与体积上限）。
- 去重身份：RSS guid（回退规范化 link）；Finnhub id。均落 source_record_id。
- 过滤（§2.1）：标题+摘要不命中该公司名称/别名/ticker 任一 → 丢弃，
  每轮丢弃计数落运行记录（dropped_count，可见、不静默）。
- 媒体条目 content_nature 一律 'unclassified'（UI 显示「内容性质未分类」），
  relation_reason 写机器可核对理由。
- API key 只从 config 读取，不打印、不落库。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode

import config
from core.announcements import ir_source

logger = logging.getLogger(__name__)

GOOGLE_NEWS_RSS_ENTRY = "https://news.google.com/rss/search"
FINNHUB_COMPANY_NEWS = "https://finnhub.io/api/v1/company-news"
MEDIA_PARSER_VERSION = 1

# 首接标记回补的口径：首次成功抓取时，发布时间早于北京时间今日的条目
# 属历史快照批量落库，标 is_backfill，不冒充新发生
UTC = timezone.utc


@dataclass(frozen=True)
class MediaItem:
    source_record_id: str      # RSS guid / 规范化 link / Finnhub id
    title: str
    url: str
    summary: str               # Finnhub summary；RSS 无摘要为空串
    source_name: str           # 媒体名（Reuters/Yahoo/…）
    published_at: datetime | None
    published_raw: str
    precision: str             # 'second' | 'unknown'
    content_hash: str


def _content_hash(fields: dict) -> str:
    canonical = json.dumps(fields, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ── Google News RSS ────────────────────────────────────────

def google_rss_url(query: str) -> str:
    """线 A 验证过的查询形态：公司全名 + when:14d，英文区。"""
    qs = urlencode({"q": f"{query} when:14d",
                    "hl": "en-US", "gl": "US", "ceid": "US:en"})
    return f"{GOOGLE_NEWS_RSS_ENTRY}?{qs}"


def parse_google_rss(body: bytes) -> list[MediaItem]:
    """解析 RSS。XML 损坏是解析失败（抛错），空 channel 是真空。"""
    try:
        root = ET.fromstring(body.decode("utf-8", errors="replace"))
    except ET.ParseError as exc:
        raise ir_source.IrFetchError(f"RSS XML 解析失败: {exc}",
                                     error_class="parse_error") from exc
    items: list[MediaItem] = []
    for node in root.iter("item"):
        title = (node.findtext("title") or "").strip()
        guid = (node.findtext("guid") or "").strip()
        link = (node.findtext("link") or "").strip()
        pub_raw = (node.findtext("pubDate") or "").strip()
        source_name = (node.findtext("source") or "").strip()
        if not title or not (guid or link):
            raise ir_source.IrFetchError("RSS 条目缺 title/guid/link",
                                         error_class="parse_error")
        published = None
        precision = "unknown"
        if pub_raw:
            try:
                published = parsedate_to_datetime(pub_raw)
                if published.tzinfo is None:
                    published = published.replace(tzinfo=UTC)
                precision = "second"
            except (ValueError, TypeError):
                raise ir_source.IrFetchError(
                    f"RSS pubDate 非法: {pub_raw!r}", error_class="parse_error")
        record_id = guid or ir_source.normalize_url(link)
        items.append(MediaItem(
            source_record_id=record_id,
            title=title,
            url=link,
            summary="",
            source_name=source_name,
            published_at=published,
            published_raw=pub_raw,
            precision=precision,
            content_hash=_content_hash({
                "id": record_id, "title": title, "pubDate": pub_raw,
                "source": source_name}),
        ))
    return items


# ── Finnhub company-news ───────────────────────────────────

def finnhub_url(symbol: str, to_date: date) -> str:
    """from 取当日−1 天（线 A：from 不覆盖北京时间窗口起点，留冗余）。"""
    from_date = to_date - timedelta(days=1)
    qs = urlencode({"symbol": symbol, "from": from_date.isoformat(),
                    "to": to_date.isoformat()})
    return f"{FINNHUB_COMPANY_NEWS}?{qs}"


def parse_finnhub(body: bytes, symbol: str) -> list[MediaItem]:
    try:
        rows = json.loads(body.decode("utf-8", errors="replace"))
    except ValueError as exc:
        raise ir_source.IrFetchError("Finnhub JSON 非法",
                                     error_class="parse_error") from exc
    if not isinstance(rows, list):
        # 401 等错误响应 {"error": ...}：明确失败，不当空列表
        raise ir_source.IrFetchError(
            f"Finnhub 响应非列表: {str(rows)[:120]}", error_class="parse_error")
    items: list[MediaItem] = []
    for row in rows:
        item_id = row.get("id")
        headline = (row.get("headline") or "").strip()
        if not item_id or not headline:
            raise ir_source.IrFetchError("Finnhub 条目缺 id/headline",
                                         error_class="parse_error")
        ts = row.get("datetime")
        published = None
        precision = "unknown"
        raw = str(ts or "")
        if ts:
            try:
                published = datetime.fromtimestamp(int(ts), tz=UTC)
                precision = "second"
            except (ValueError, OverflowError, OSError) as exc:
                raise ir_source.IrFetchError(
                    f"Finnhub datetime 非法: {ts!r}", error_class="parse_error") from exc
        items.append(MediaItem(
            source_record_id=str(item_id),
            title=headline,
            url=row.get("url") or "",
            summary=(row.get("summary") or "").strip(),
            source_name=(row.get("source") or "").strip(),
            published_at=published,
            published_raw=raw,
            precision=precision,
            content_hash=_content_hash({
                "id": str(item_id), "headline": headline,
                "datetime": raw, "source": row.get("source")}),
        ))
    return items


def finnhub_headers() -> dict:
    """API key 只从 config 读取；调用方不得打印。"""
    key = (config.finnhub.api_key or "").strip()
    if not key:
        raise ir_source.IrFetchError(
            "STOCK_FINNHUB_API_KEY 未配置，Finnhub 来源不可用",
            error_class="missing_api_key")
    return {"X-Finnhub-Token": key}


def match_terms_for(source: dict) -> list[str]:
    """注册行 query_config 中的命中词表（公司名/别名/ticker）。"""
    return list(json.loads(source.get("query_config") or "{}")
                .get("match_terms") or [])


def relation_reason_for(source: dict) -> str:
    """机器可核对的关联理由（写入每条公告的 relation_reason）。"""
    cfg = json.loads(source.get("query_config") or "{}")
    if source["platform"] == "google_news_rss":
        return f'ticker 直连：Google 查询词 "{cfg.get("google_query", "")}"'
    if source["platform"] == "finnhub":
        return f"Finnhub symbol {cfg.get('finnhub_symbol', '')}"
    return f"平台 {source['platform']}"


# ── 过滤（§2.1）────────────────────────────────────────────

def matches_company(item_title: str, item_summary: str,
                    match_terms: list[str]) -> bool:
    """标题+摘要命中公司名/别名/ticker 任一（大小写不敏感、词边界）。"""
    text = f"{item_title} {item_summary}".lower()
    for term in match_terms:
        term = term.strip().lower()
        if not term:
            continue
        if re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", text):
            return True
    return False


# ── 首批公司媒体源种子（新增公司 = 加一行注册配置）──────────
# match_terms：公司名/别名/ticker；google_query：线 A 验证的公司查询词。
# 媒体注册行默认 enabled=False（真实启用属部署片）。

MEDIA_SEEDS: dict[str, dict] = {
    "ADBE": {
        "google_query": "Adobe",
        "match_terms": ["Adobe", "ADBE"],
        "finnhub_symbol": "ADBE",
    },
    "INTC": {
        "google_query": "Intel",
        "match_terms": ["Intel", "INTC"],
        "finnhub_symbol": "INTC",
    },
    "PLTR": {
        "google_query": "Palantir",
        "match_terms": ["Palantir", "PLTR"],
        "finnhub_symbol": "PLTR",
    },
    "MU": {
        "google_query": "Micron Technology",
        "match_terms": ["Micron Technology", "Micron", "MU"],
        "finnhub_symbol": "MU",
    },
    "WDC": {
        "google_query": "Western Digital",
        "match_terms": ["Western Digital", "WDC"],
        "finnhub_symbol": "WDC",
    },
    "NBIS": {
        "google_query": "Nebius",
        "match_terms": ["Nebius", "NBIS"],
        "finnhub_symbol": "NBIS",
    },
}

MEDIA_PLATFORMS = ("google_news_rss", "finnhub")

# 各平台覆盖视野（天）：停采超过该视野后，快照无法证明中断区间覆盖，
# 按 gap 语义记缺口并标 partial（不推进完整检查点）
COVERAGE_HORIZON_DAYS = {
    "google_news_rss": 10,   # when:14d 查询，实测回溯约 7~10 天
    "finnhub": 2,            # from 取当日−1 天
}


def media_source_specs(stock_code: str) -> list[dict]:
    """一家公司按种子展开为媒体来源注册规格（每股两个来源）。"""
    seed = MEDIA_SEEDS.get(stock_code)
    if not seed:
        return []
    return [
        {
            "platform": "google_news_rss",
            "allowed_domain": "news.google.com",
            "entry_url": google_rss_url(seed["google_query"]),
            "source_timezone": "UTC",  # RSS pubDate 带 GMT 偏移
            "query_config": json.dumps({
                "google_query": seed["google_query"],
                "match_terms": seed["match_terms"]}, ensure_ascii=False),
            "relation_reason": f'ticker 直连：Google 查询词 "{seed["google_query"]}"',
        },
        {
            "platform": "finnhub",
            "allowed_domain": "finnhub.io",
            "entry_url": FINNHUB_COMPANY_NEWS,
            "source_timezone": "UTC",  # datetime 为 epoch 秒
            "query_config": json.dumps({
                "finnhub_symbol": seed["finnhub_symbol"],
                "match_terms": seed["match_terms"]}, ensure_ascii=False),
            "relation_reason": f"Finnhub symbol {seed['finnhub_symbol']}",
        },
    ]


def fetch_and_parse(source: dict, *, today: date) -> list[MediaItem]:
    """按平台获取并解析一个媒体来源（单请求，无重试循环）。"""
    platform = source["platform"]
    if platform == "google_news_rss":
        body = ir_source.http_get(
            source["entry_url"], allowed_domain=source["allowed_domain"])
        return parse_google_rss(body)
    if platform == "finnhub":
        symbol = json.loads(source["query_config"])["finnhub_symbol"]
        url = finnhub_url(symbol, today)
        body = ir_source.http_get(
            url, allowed_domain=source["allowed_domain"],
            extra_headers=finnhub_headers())
        return parse_finnhub(body, symbol)
    raise ir_source.IrFetchError(f"未知媒体平台: {platform!r}",
                                 error_class="unknown_platform")
