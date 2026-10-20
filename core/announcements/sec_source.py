"""core/announcements/sec_source.py — SEC submissions API 公告来源适配器。

合同：docs/WATCHLIST_ANNOUNCEMENTS_MVP_PLAN.md §2/§3/§4。

- 所有 SEC 请求统一经过 core/sec_client.py 的 sec_client.get()，
  共享 2 req/s 预算与共享退避；本模块不另建 HTTP 重试循环。
- 表单范围：8-K/10-K/10-Q/6-K/20-F/40-F 及相应 /A 修订；/A 独立成行
  （accession 不同）。标题使用中性表单描述，不伪造新闻标题。
- published_at 取 acceptanceDateTime，语义为"SEC 接收时间"，不声称它是
  事件首次公开时间；原始字符串与时区原样保留。缺失时退回 filingDate，
  published_at 置 NULL、精度 'day'，不补午夜伪装精确时间。
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from core import sec_client

logger = logging.getLogger(__name__)

# 解析器版本：解析逻辑变更时递增；公告与运行记录引用该版本。
PARSER_VERSION = 1

TARGET_FORMS = frozenset({"8-K", "10-K", "10-Q", "6-K", "20-F", "40-F"})

# 中性表单描述（方案 §2：SEC 标题使用中性表单描述，不得伪造新闻标题）
FORM_DESCRIPTIONS = {
    "8-K": "当前报告",
    "10-K": "年报",
    "10-Q": "季报",
    "6-K": "外国发行人临时报告",
    "20-F": "外国发行人年报",
    "40-F": "外国发行人年报（加拿大）",
}

# SEC submissions API 入口；测试通过 monkeypatch 指向本地 HTTP 服务。
SUBMISSIONS_BASE = "https://data.sec.gov/submissions"

# SEC acceptance 时间为美东时间（ submissions JSON 现代格式多带 Z，
# 无时区后缀的历史格式按美东解释）。
SEC_TIMEZONE = "America/New_York"


def is_target_form(form: str) -> bool:
    """表单筛选：目标表单及相应 /A 修订。"""
    base = form[:-2] if form.endswith("/A") else form
    return base in TARGET_FORMS


def neutral_title(form: str, description: str = "") -> str:
    """中性表单描述标题，如 "8-K 当前报告"、"10-K/A 年报（修订）"。"""
    amended = form.endswith("/A")
    base = form[:-2] if amended else form
    title = f"{form} {FORM_DESCRIPTIONS.get(base, '申报文件')}"
    if amended:
        title += "（修订）"
    return title


@dataclass(frozen=True)
class SecFiling:
    """一条 SEC 申报记录（已按目标表单过滤后使用）。"""

    accession: str            # 去重身份（source_record_id）
    form: str                 # 原始表单类型，开放 text（含 /A）
    filing_date: date         # SEC filing date（窗口归属判断用）
    acceptance_raw: str       # acceptanceDateTime 原始字符串（可空）
    published_at: datetime | None  # UTC；仅日期时为 None
    published_precision: str  # 'second' | 'day'
    primary_document: str
    url: str                  # 主文件原文链接
    content_hash: str         # 变更检测

    @property
    def accession_nodash(self) -> str:
        return self.accession.replace("-", "")


def _parse_acceptance(raw: str) -> datetime | None:
    """解析 acceptanceDateTime；带时区直接解析，裸时间按美东解释。"""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(SEC_TIMEZONE))
    return dt


def _content_hash(fields: dict) -> str:
    canonical = json.dumps(fields, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _parse_filing_rows(data: dict, cik: str) -> list[SecFiling]:
    """解析 submissions JSON 的平行数组段（recent 或 filings.files 页）。

    必需数组（accessionNumber/form/filingDate/primaryDocument）缺失属于
    响应结构损坏，是解析失败（落 failed，不推进检查点）；数组齐全但为
    空才是"窗口内无公告"（success_empty）。数组长度不一致或关键字段
    缺失同样明确抛错——不静默截断，否则会把解析失败伪装成无公告。
    """
    arrays = data
    for key in ("accessionNumber", "form", "filingDate", "primaryDocument"):
        if key not in arrays:
            raise ValueError(f"submissions 响应缺少必需数组: {key}")
    accession = arrays["accessionNumber"] or []
    n = len(accession)
    for key in ("form", "filingDate", "primaryDocument"):
        if len(arrays[key] or []) != n:
            raise ValueError(f"submissions 数组长度不一致: {key}")
    forms = arrays["form"]
    filing_dates = arrays["filingDate"]
    acceptances = arrays.get("acceptanceDateTime") or [""] * n
    docs = arrays["primaryDocument"]

    filings: list[SecFiling] = []
    cik_int = str(int(cik))  # URL 用无补零 CIK
    for i in range(n):
        form = str(forms[i])
        if not is_target_form(form):
            continue
        if not docs[i]:
            raise ValueError(f"{accession[i]}: primaryDocument 缺失")
        try:
            fdate = date.fromisoformat(str(filing_dates[i]))
        except ValueError as exc:
            raise ValueError(f"{accession[i]}: filingDate 非法: {filing_dates[i]!r}") from exc
        acc_raw = str(acceptances[i]) if i < len(acceptances) else ""
        published = _parse_acceptance(acc_raw)
        accession_nodash = str(accession[i]).replace("-", "")
        url = (
            f"https://www.sec.gov/Archives/edgar/data/{cik_int}/"
            f"{accession_nodash}/{docs[i]}"
        )
        filings.append(SecFiling(
            accession=str(accession[i]),
            form=form,
            filing_date=fdate,
            acceptance_raw=acc_raw,
            published_at=published,
            published_precision="second" if published is not None else "day",
            primary_document=str(docs[i]),
            url=url,
            content_hash=_content_hash({
                "accession": str(accession[i]),
                "form": form,
                "filingDate": str(filing_dates[i]),
                "acceptanceDateTime": acc_raw,
                "primaryDocument": str(docs[i]),
            }),
        ))
    return filings


@dataclass(frozen=True)
class SubmissionsPage:
    """filings.files 中的一个历史分页描述。"""

    name: str
    filing_from: date
    filing_to: date


@dataclass
class SubmissionsResult:
    filings: list[SecFiling]          # recent 段目标表单记录
    recent_min_date: date | None      # recent 段（全部表单）最早 filingDate
    files: list[SubmissionsPage]      # 历史分页目录


def parse_submissions(data: dict) -> SubmissionsResult:
    """解析 submissions 主文档。"""
    filings_root = data.get("filings")
    if not isinstance(filings_root, dict) or not isinstance(filings_root.get("recent"), dict):
        raise ValueError("submissions JSON 缺少 filings.recent")
    recent = filings_root["recent"]
    cik = str(data.get("cik", "")).strip()
    if not cik:
        raise ValueError("submissions JSON 缺少 cik")

    filings = _parse_filing_rows(recent, cik)
    all_dates = [d for d in (recent.get("filingDate") or []) if d]
    recent_min = min((date.fromisoformat(str(d)) for d in all_dates), default=None)

    pages: list[SubmissionsPage] = []
    for entry in filings_root.get("files") or []:
        pages.append(SubmissionsPage(
            name=str(entry["name"]),
            filing_from=date.fromisoformat(str(entry["filingFrom"])),
            filing_to=date.fromisoformat(str(entry["filingTo"])),
        ))
    return SubmissionsResult(filings=filings, recent_min_date=recent_min, files=pages)


def parse_page(data: dict, cik: str) -> list[SecFiling]:
    """解析 filings.files 分页（与 recent 同为平行数组格式）。"""
    return _parse_filing_rows(data, cik)


def fetch_submissions(cik: str, *, timeout: float = 60,
                      total_timeout: float = 180) -> dict:
    """获取 submissions 主文档。失败抛 SECError，不返回空结果伪装。"""
    url = f"{SUBMISSIONS_BASE}/CIK{cik}.json"
    response = sec_client.get(
        url, task="watchlist_announcements", identity=f"submissions/{cik}",
        timeout=timeout, total_timeout=total_timeout,
    )
    return response.json()


def fetch_page(name: str, *, timeout: float = 60, total_timeout: float = 180,
               ) -> dict | None:
    """获取 filings.files 分页；404 返回 None（历史不可访问，由调用方记缺口）。

    其他失败抛 SECError。只允许业务 404 绕过 HTTP 失败（sec_client 约束）。
    """
    url = f"{SUBMISSIONS_BASE}/{name}"
    response = sec_client.get(
        url, task="watchlist_announcements", identity=f"submissions_page/{name}",
        timeout=timeout, total_timeout=total_timeout, allowed_statuses=(404,),
    )
    if response.status_code == 404:
        return None
    return response.json()
