"""core/announcements/ir_source.py — 公司 IR/新闻室来源适配框架（平台优先）。

合同：docs/WATCHLIST_ANNOUNCEMENTS_MVP_PLAN.md §2/§3/§4。

设计要点：
- 适配器按平台/站点结构实现，不按公司写死；平台识别显式存储在注册表
  platform 列，不靠域名猜测。新增公司只需新增注册行（换 entry_url），
  新平台才写新适配器。
- IR 使用自己的 HTTP 层（单次尝试、超时、体积上限、重定向校验），
  不经过 core/sec_client.py，不占 SEC 2 req/s 额度；不绕过验证码/403。
  受阻是一等状态：注册行 source_status='blocked' + status_reason，
  不抓取、不计入成功率分母、不阻塞其他来源。
- 去重：来源 + 稳定发布 ID 或规范化 URL，不按标题强行合并。
- 覆盖能力诚实化：列表历史不足回补窗口时记实际覆盖范围并标 partial，
  缺口写 watchlist_announcement_gap，不标完整。

平台适配器：
- nebius_newsroom：Next.js __NEXT_DATA__ Apollo 归一化缓存，取
  press_releases（排除 blog_posts）；结构按 2026-10-21 实测建模，
  启用前仍需真实采集验证。
- q4_press_release：Q4 Inc IR 平台新闻列表接口结构（MU/WDC 共用）。
  线 A 实测两家均受阻（403/动态加载），本适配器按 Q4 公开结构编写，
  未真实验证；注册初始状态为 blocked。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

logger = logging.getLogger(__name__)

# 列表响应体积上限与单页超时（方案 §3：每次任务设响应体上限与超时）
MAX_LISTING_BYTES = 8 * 1024 * 1024
LISTING_TIMEOUT = 30
IR_USER_AGENT = "StockDataSync/1.0 (watchlist announcements)"


class IrFetchError(RuntimeError):
    """IR 列表获取/解析失败；必须落 failed 状态，不得伪装成无公告。"""

    def __init__(self, message: str, *, error_class: str):
        super().__init__(message)
        self.error_class = error_class


# ── HTTP 层（独立于 SEC gate）──────────────────────────────

def _resolve_ips(host: str):
    """解析域名地址；独立成函数便于离线测试 monkeypatch（不做真实 DNS）。"""
    import socket
    return {info[4][0] for info in socket.getaddrinfo(host, 443)}


def _is_public_ip(ip_str: str) -> bool:
    import ipaddress
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return ip.is_global  # 内网/回环/保留/链路本地/组播均非 global


# 测试通过 monkeypatch 替换为允许本地回环的版本。
def _validate_url(url: str, allowed_domain: str) -> None:
    """只允许 https、目标域名（含子域）内的地址；拒绝 IP 字面量
    （allowed_domain 注册为 127.0.0.1 之类不能绕过）与内网/保留地址。
    域名解析后的地址检查在 http_get 请求前进行（_resolve_ips 可离线替换）。
    """
    import ipaddress

    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        valid = (
            parsed.scheme == "https"
            and (host == allowed_domain or host.endswith("." + allowed_domain))
            and parsed.port in (None, 443)
            and not parsed.username and not parsed.password
        )
        if valid:
            try:
                ipaddress.ip_address(host)
                valid = False  # IP 字面量一律拒绝（注册表只放域名）
            except ValueError:
                pass
    except ValueError:
        valid = False
    if not valid:
        raise IrFetchError(
            f"IR URL 校验失败（仅允许 https://{allowed_domain} 及其子域，"
            f"拒绝 IP 字面量）: {url}",
            error_class="url_rejected")


def http_get(url: str, *, allowed_domain: str, timeout: float = LISTING_TIMEOUT,
             max_bytes: int = MAX_LISTING_BYTES, max_redirects: int = 3) -> bytes:
    """单次尝试 GET：超时、体积上限、重定向逐跳校验；不做自动重试
    （退避由 next_due 推进承担，避免失败时紧密重跑）。"""
    import requests

    current = url
    for _hop in range(max_redirects + 1):
        _validate_url(current, allowed_domain)
        # 解析后地址检查：域名白名单之外，解析到内网/保留地址同样拒绝
        host = urlsplit(current).hostname or ""
        try:
            resolved = _resolve_ips(host)
        except OSError as exc:
            raise IrFetchError(f"IR 域名解析失败: {host}",
                               error_class="transport") from exc
        if not resolved or not all(_is_public_ip(ip) for ip in resolved):
            raise IrFetchError(
                f"IR 目标解析到非公开地址，拒绝抓取: {host}",
                error_class="url_rejected")
        try:
            with requests.get(
                current,
                headers={"User-Agent": IR_USER_AGENT, "Accept": "*/*"},
                timeout=timeout, allow_redirects=False, stream=True,
            ) as resp:
                if resp.status_code in (301, 302, 303, 307, 308):
                    location = resp.headers.get("Location")
                    if not location:
                        raise IrFetchError("IR 重定向缺少 Location",
                                           error_class="redirect_invalid")
                    current = requests.compat.urljoin(current, location)
                    continue
                if resp.status_code in (401, 403):
                    raise IrFetchError(f"IR HTTP {resp.status_code}（受阻）",
                                       error_class=f"http_{resp.status_code}")
                if resp.status_code == 429:
                    raise IrFetchError("IR HTTP 429（限流）",
                                       error_class="http_429")
                if not 200 <= resp.status_code < 300:
                    raise IrFetchError(f"IR HTTP {resp.status_code}",
                                       error_class=f"http_{resp.status_code}")
                chunks, size = [], 0
                for chunk in resp.iter_content(65536):
                    size += len(chunk)
                    if size > max_bytes:
                        raise IrFetchError("IR 列表响应超过体积上限",
                                           error_class="response_too_large")
                    chunks.append(chunk)
                return b"".join(chunks)
        except requests.RequestException as exc:
            raise IrFetchError(f"IR 传输失败: {type(exc).__name__}: {exc}",
                               error_class="transport") from exc
    raise IrFetchError("IR 重定向次数超限", error_class="redirect_invalid")


# ── 适配器基类与数据结构 ───────────────────────────────────

@dataclass(frozen=True)
class IrItem:
    """IR 列表条目（已按来源时区/精度解析）。"""

    source_record_id: str      # 稳定发布 ID 或规范化 URL
    title: str
    url: str
    published_raw: str
    published_at: datetime | None
    precision: str             # 'second' | 'day' | 'unknown'
    content_hash: str


def _content_hash(fields: dict) -> str:
    canonical = json.dumps(fields, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def normalize_url(url: str) -> str:
    """规范化 URL：小写 scheme/host、去 fragment、去跟踪参数、去尾斜杠。"""
    parsed = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(parsed.query)
             if not k.lower().startswith("utm_")]
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(),
                       parsed.path.rstrip("/") or "/", urlencode(query), ""))


class IrAdapter:
    """平台适配器基类。子类按平台/站点结构实现 parse_listing。"""

    platform: str = ""
    parser_version: int = 1
    supports_pagination: bool = False

    def page_url(self, entry_url: str, page: int) -> str | None:
        """第 page 页（0 起）的列表 URL；不支持分页时 page>0 返回 None。"""
        return entry_url if page == 0 else None

    def parse_listing(self, body: bytes, entry_url: str) -> list[IrItem]:
        raise NotImplementedError


# ── nebius.com/newsroom（Next.js __NEXT_DATA__）─────────────

class NebiusNewsroomAdapter(IrAdapter):
    """Nebius 新闻室：__NEXT_DATA__ 内 Apollo 归一化缓存的 press_releases。

    区分新闻发布与博客：只取 __typename == 'press_releases'，
    blog_posts 不属于官方新闻发布。结构按 2026-10-21 实测建模。
    条目字段：name（slug，稳定 ID）、title、date（ISO 日期）、
    publication_datetime（UTC 秒级，可空）。
    """

    platform = "nebius_newsroom"
    parser_version = 1
    supports_pagination = False  # 实测列表完整内嵌于首屏 __NEXT_DATA__

    _NEXT_DATA_RE = re.compile(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        re.S)

    def parse_listing(self, body: bytes, entry_url: str) -> list[IrItem]:
        m = self._NEXT_DATA_RE.search(body.decode("utf-8", errors="replace"))
        if not m:
            raise IrFetchError("nebius 页面缺少 __NEXT_DATA__（结构变更？）",
                               error_class="parse_error")
        try:
            data = json.loads(m.group(1))
        except ValueError as exc:
            raise IrFetchError("nebius __NEXT_DATA__ JSON 非法",
                               error_class="parse_error") from exc
        items: list[IrItem] = []
        for key, value in self._iter_cache(data):
            if not key.startswith("press_releases:"):
                continue
            if not isinstance(value, dict):
                continue
            slug = value.get("name")
            title = value.get("title")
            day = value.get("date")
            if not slug or not title or not day:
                raise IrFetchError(
                    f"nebius press_release 条目缺必需字段: {key}",
                    error_class="parse_error")
            pub_raw = str(value.get("publication_datetime") or "")
            published = None
            precision = "day"
            raw = str(day)
            if pub_raw:
                try:
                    published = datetime.fromisoformat(
                        pub_raw.replace("Z", "+00:00"))
                    if published.tzinfo is None:
                        published = published.replace(tzinfo=timezone.utc)
                    precision = "second"
                    raw = pub_raw
                except ValueError:
                    raise IrFetchError(
                        f"nebius publication_datetime 非法: {pub_raw!r}",
                        error_class="parse_error")
            items.append(IrItem(
                source_record_id=str(slug),
                title=str(title),
                url=f"https://nebius.com/newsroom/{slug}",
                published_raw=raw,
                published_at=published,
                precision=precision,
                content_hash=_content_hash({
                    "slug": str(slug), "title": str(title),
                    "date": str(day), "publication_datetime": pub_raw}),
            ))
        items.sort(key=lambda it: it.source_record_id)
        return items

    def _iter_cache(self, data):
        """定位 Apollo 归一化缓存层并遍历 (key, value)。"""
        def find(obj):
            if isinstance(obj, dict):
                if any(re.match(r"^\w+:\{", k) for k in obj):
                    return obj
                for v in obj.values():
                    found = find(v)
                    if found is not None:
                        return found
            elif isinstance(obj, list):
                for v in obj:
                    found = find(v)
                    if found is not None:
                        return found
            return None
        cache = find(data)
        if cache is None:
            # __NEXT_DATA__ 存在且是合法 Next.js 负载但无归一化缓存：
            # 列表为空（无 press_releases 条目）；结构损坏则 parse_error
            if isinstance(data, dict) and "props" in data:
                return {}
            raise IrFetchError("nebius __NEXT_DATA__ 缺少归一化缓存",
                               error_class="parse_error")
        return cache.items()


# ── Q4 Inc IR 平台（MU/WDC 共用；未真实验证）────────────────

class Q4PressReleaseAdapter(IrAdapter):
    """Q4 Inc 平台新闻列表 JSON 接口结构（GetPressReleaseList）。

    线 A 实测 investors.micron.com 列表接口 403、investor.wdc.com 动态加载
    受阻（2026-10），本适配器按 Q4 公开结构编写，未经真实验证；
    对应注册行初始为 blocked，验证通过后逐家启用。
    条目字段：PressReleaseId（稳定 ID）、Title、LinkToDetailPage、
    PressReleaseDate（ASP.NET /Date(ms)/ 或 ISO）。
    """

    platform = "q4_press_release"
    parser_version = 1
    supports_pagination = True

    def page_url(self, entry_url: str, page: int) -> str | None:
        if page == 0:
            return entry_url
        sep = "&" if "?" in entry_url else "?"
        return f"{entry_url}{sep}pageNumber={page + 1}"

    def parse_listing(self, body: bytes, entry_url: str) -> list[IrItem]:
        try:
            data = json.loads(body.decode("utf-8", errors="replace"))
        except ValueError as exc:
            raise IrFetchError("Q4 列表 JSON 非法", error_class="parse_error") from exc
        rows = data.get("GetPressReleaseListResult")
        if not isinstance(rows, list):
            raise IrFetchError("Q4 列表缺少 GetPressReleaseListResult",
                               error_class="parse_error")
        items: list[IrItem] = []
        for row in rows:
            item_id = row.get("PressReleaseId")
            title = row.get("Title")
            link = row.get("LinkToDetailPage") or ""
            date_raw = str(row.get("PressReleaseDate") or "")
            if not item_id or not title:
                raise IrFetchError("Q4 条目缺 PressReleaseId/Title",
                                   error_class="parse_error")
            published = self._parse_date(date_raw)
            precision = "second" if published is not None else (
                "day" if date_raw else "unknown")
            url = link if link.startswith("http") else normalize_url(
                f"{entry_url.rstrip('/')}/{link.lstrip('/')}") if link else entry_url
            items.append(IrItem(
                source_record_id=str(item_id),
                title=str(title),
                url=url,
                published_raw=date_raw,
                published_at=published,
                precision=precision,
                content_hash=_content_hash({
                    "id": str(item_id), "title": str(title),
                    "link": url, "date": date_raw}),
            ))
        return items

    @staticmethod
    def _parse_date(raw: str) -> datetime | None:
        if not raw:
            return None
        m = re.fullmatch(r"/Date\((\d+)\)/", raw)
        try:
            if m:
                return datetime.fromtimestamp(int(m.group(1)) / 1000,
                                              tz=timezone.utc)
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                return None  # 无时区裸时间不当精确时间用
            return dt
        except (ValueError, OverflowError, OSError) as exc:
            raise IrFetchError(f"Q4 PressReleaseDate 非法: {raw!r}",
                               error_class="parse_error") from exc


IR_ADAPTERS: dict[str, IrAdapter] = {
    a.platform: a for a in (NebiusNewsroomAdapter(), Q4PressReleaseAdapter())
}


# ── 首批公司注册种子（新增公司 = 加一行；新平台才写适配器）──
# enabled/source_status 是初始状态：已存在的注册行不被 seed 覆盖
# （部署验证通过后逐家启用由运营显式调整）。

IR_SEEDS: dict[str, dict] = {
    "NBIS": {
        "platform": "nebius_newsroom",
        "allowed_domain": "nebius.com",
        "entry_url": "https://nebius.com/newsroom",
        "source_timezone": "UTC",  # publication_datetime 带 Z
        "initial_status": "pending_verification",
        "initial_reason": "适配器按 2026-10-21 实测 __NEXT_DATA__ 结构实现；"
                          "启用前需真实采集验证（分页/时区/历史覆盖）",
    },
    "MU": {
        "platform": "q4_press_release",
        "allowed_domain": "investors.micron.com",
        "entry_url": "https://investors.micron.com/news/default.aspx",
        "source_timezone": "America/New_York",
        "initial_status": "blocked",
        "initial_reason": "列表接口实测 403 受阻（2026-10 线 A 验证）",
    },
    "WDC": {
        "platform": "q4_press_release",
        "allowed_domain": "investor.wdc.com",
        "entry_url": "https://investor.wdc.com/",
        "source_timezone": "America/New_York",
        "initial_status": "blocked",
        "initial_reason": "抓取失败：动态加载/受阻（2026-10 线 A 验证）",
    },
}
