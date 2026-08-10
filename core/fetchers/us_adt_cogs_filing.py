"""core/fetchers/us_adt_cogs_filing.py — ADT 合并 Cost of Revenue 受限 filing-source 链路。

规格:docs/core/US_ADT_CONSOLIDATED_COGS_IMPLEMENTATION_TASK.md

ADT 的合并 Cost of Revenue 由发行人扩展 tag
`adt:CostofRevenueExcludingDepreciationDepletionandAmortization` 的**无维度 context**
披露;SEC CompanyFacts 不含扩展命名空间,因此必须直接从 SEC Archives 的 filing
inline XBRL 原件补 ingest。本模块是该链路**唯一**入口,且只服务 ADT 这一个
tag、APPROVED_FILINGS 白名单内的 accession:

- 同 tag 的无维度总额与有维度子项都原样写入版本层(保留再选择);
- selector 层(core/selectors/us_financial.py 的 _apply_dimensionless_only_restrictions)
  只允许 dimensions={} 的事实进入选择;
- expected_dimensionless_total 是审计证据值,仅用于重放时校验"解析结果与审计
  一致",绝不作为数值来源;不一致即抛 ADTIngestBlocked,不静默放行;
- 新的 ADT 年度 filing 默认不在白名单, gross_margin=NULL 是预期保守结果;
  扩充白名单须先完成同等审计(见任务文档 §4.4)。
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

import config
from core.fetchers.us_inline_xbrl import localname, parse_inline_xbrl
from db import get_or_create_raw_snapshot_version, save_raw_snapshot_observation

logger = logging.getLogger(__name__)

ADT_STOCK = "ADT"
ADT_CIK = "0001703056"
ADT_CIK_NODASH = "1703056"
ADT_COGS_TAG = "CostofRevenueExcludingDepreciationDepletionandAmortization"
ADT_COGS_TAG_LOCAL = ADT_COGS_TAG.lower()
ADT_TAXONOMY = "adt"
ADT_STANDARD_FIELD = "cost_of_goods_sold"

RAW_DATA_TYPE = "filing_xbrl_instance"
RAW_SOURCE = "sec_edgar_archives"

SEC_ARCHIVES = "https://www.sec.gov/Archives/edgar/data"
HEADERS = {"User-Agent": config.sec.user_agent, "Accept": "*/*"}

CACHE_ROOT = Path("data/sec_filing_cache") / ADT_STOCK


class ADTIngestBlocked(RuntimeError):
    """白名单 filing 的证据校验失败:阻断,不得静默成功。"""


@dataclass(frozen=True)
class ApprovedFiling:
    """经审计认可的年度 filing。

    expected_totals: 该 filing 利润表(含比较期)披露的各年度无维度合并总额,
    仅作重放校验,不是数值来源。ADT 在 FY2023 10-K 起按持续经营重述收入,
    比较期 COGS 与重述收入同 accession 配对——这是口径一致性的来源。
    """

    fiscal_year: int
    accession_no: str
    form: str
    filed_date: date
    expected_totals: dict[int, Decimal]


# FY2022 原 10-K 与 10-K/A 均入版本层:前者保证 as-of 历史无空窗,后者由
# latest-restated 语义作为 current。每个 filing 的比较期事实同样保留(版本层
# 按 filed_date 自然完成"原报 vs 重述"的选择)。
APPROVED_FILINGS: tuple[ApprovedFiling, ...] = (
    ApprovedFiling(2021, "0001703056-22-000042", "10-K", date(2022, 3, 1), {
        2019: Decimal("1390284000"), 2020: Decimal("1516528000"),
        2021: Decimal("1550173000")}),
    ApprovedFiling(2022, "0001703056-23-000046", "10-K", date(2023, 2, 28), {
        2020: Decimal("1516528000"), 2021: Decimal("1550173000"),
        2022: Decimal("2039848000")}),
    ApprovedFiling(2022, "0001703056-23-000146", "10-K/A", date(2023, 7, 27), {
        2020: Decimal("1516528000"), 2021: Decimal("1550173000"),
        2022: Decimal("2039848000")}),
    ApprovedFiling(2023, "0001703056-24-000020", "10-K", date(2024, 2, 28), {
        2021: Decimal("772785000"), 2022: Decimal("1200492000"),
        2023: Decimal("1008466000")}),
    ApprovedFiling(2024, "0001703056-25-000022", "10-K", date(2025, 2, 27), {
        2022: Decimal("698782000"), 2023: Decimal("751682000"),
        2024: Decimal("847114000")}),
    ApprovedFiling(2025, "0001703056-26-000022", "10-K", date(2026, 3, 2), {
        2023: Decimal("751682000"), 2024: Decimal("847114000"),
        2025: Decimal("982972000")}),
)

_USD_UNITS = {"USD", "ISO4217:USD"}


# ── 抓取(缓存到 data/sec_filing_cache/ADT/)─────────────────

def _http_get(url: str) -> bytes:
    import requests

    resp = requests.get(url, headers=HEADERS, timeout=60)
    if resp.status_code != 200:
        raise ADTIngestBlocked(f"SEC 请求失败 {resp.status_code}: {url}")
    time.sleep(0.2)  # SEC 限流自律
    return resp.content


def fetch_main_document(
    accession: str,
    cache_root: Path = CACHE_ROOT,
    fetch: bool = True,
) -> tuple[str, str, str]:
    """取 filing 主文档(最大的非 R .htm)。Returns: (文件名, 文本, filing URL)。"""
    acc_nodash = accession.replace("-", "")
    base = f"{SEC_ARCHIVES}/{ADT_CIK_NODASH}/{acc_nodash}"
    dest = cache_root / acc_nodash
    dest.mkdir(parents=True, exist_ok=True)

    index_path = dest / "index.json"
    if not index_path.exists():
        if not fetch:
            raise ADTIngestBlocked(f"本地缓存缺失且 fetch=False: {index_path}")
        index_path.write_bytes(_http_get(f"{base}/index.json"))
    items = json.loads(index_path.read_text())["directory"]["item"]
    htm_candidates = sorted(
        (
            (it["name"], int(it.get("size", "0") or 0))
            for it in items
            if it.get("type") != "dir"
            and it["name"].lower().endswith(".htm")
            and not it["name"].startswith("R")
        ),
        key=lambda kv: -kv[1],
    )
    if not htm_candidates:
        raise ADTIngestBlocked(f"filing 目录无主文档 htm: {base}")
    doc_name = htm_candidates[0][0]
    doc_path = dest / doc_name
    if not doc_path.exists():
        if not fetch:
            raise ADTIngestBlocked(f"本地缓存缺失且 fetch=False: {doc_path}")
        doc_path.write_bytes(_http_get(f"{base}/{doc_name}"))
    return doc_name, doc_path.read_text(errors="replace"), f"{base}/"


# ── fact_records 构建(纯函数,可离线测试)─────────────────────

def _is_annual_duration(period_start: Optional[date], period_end: Optional[date]) -> Optional[int]:
    """日历年年度 duration(330–380 天,年末 12-31)→ 年度;否则 None。"""
    if period_start is None or period_end is None:
        return None
    days = (period_end - period_start).days
    if not (330 <= days <= 380) or (period_end.month, period_end.day) != (12, 31):
        return None
    return period_end.year


def extract_cogs_fact_records(
    html_text: str,
    filing: ApprovedFiling,
) -> tuple[list[dict], list[dict]]:
    """从主文档提取目标 tag 的年度 duration facts → writer 的 fact_records。

    覆盖该 filing 的当前年度与比较期(重述配对所需);无维度总额与有维度
    子项都输出(保留再选择);taxonomy="adt"、standard_field="cost_of_goods_sold"。
    年度期间但不在该 filing 批准年度内的事实进 skipped(UNAPPROVED_PERIOD,
    大声记录);非年度/非 USD/无 context 同样进 skipped,绝不静默丢弃、
    也绝不映射。

    Returns: (fact_records, skipped_records)
    skipped_records: [{sec_tag, context_id, value_numeric, reason}]
    """
    facts, contexts = parse_inline_xbrl(html_text, frozenset({ADT_COGS_TAG_LOCAL}))
    records: list[dict] = []
    skipped: list[dict] = []
    for f in facts:
        if localname(f.sec_tag) != ADT_COGS_TAG_LOCAL:
            continue
        ctx = contexts.get(f.context_id or "")
        if ctx is None:
            skipped.append({"sec_tag": f.sec_tag, "context_id": f.context_id,
                            "value_numeric": str(f.value_numeric), "reason": "NO_CONTEXT"})
            continue
        if ctx.instant is not None or ctx.period_end is None:
            skipped.append({"sec_tag": f.sec_tag, "context_id": f.context_id,
                            "value_numeric": str(f.value_numeric), "reason": "NOT_DURATION"})
            continue
        year = _is_annual_duration(ctx.period_start, ctx.period_end)
        if year is None:
            # 季度/半年等非年度期间不映射,属正常披露,不记录
            continue
        if year not in filing.expected_totals:
            skipped.append({"sec_tag": f.sec_tag, "context_id": f.context_id,
                            "value_numeric": str(f.value_numeric),
                            "reason": f"UNAPPROVED_PERIOD:{year}"})
            continue
        if (f.unit or "").upper() not in _USD_UNITS:
            skipped.append({"sec_tag": f.sec_tag, "context_id": f.context_id,
                            "value_numeric": str(f.value_numeric),
                            "reason": f"NON_USD_UNIT:{f.unit}"})
            continue
        records.append({
            "accn": filing.accession_no,
            "end": ctx.period_end.isoformat(),
            "val": f.value_numeric,
            "start": ctx.period_start.isoformat() if ctx.period_start else None,
            "fp": "FY",
            "fy": year,
            "form": filing.form,
            "filed": filing.filed_date.isoformat(),
            "frame": None,
            "unit": "USD",
            "tag": ADT_COGS_TAG,
            "field": ADT_STANDARD_FIELD,
            "dimensions": dict(ctx.dimensions),
            "_period_kind": "duration",
            "taxonomy": ADT_TAXONOMY,
        })
    return records, skipped


def verify_against_audit(records: list[dict], filing: ApprovedFiling) -> None:
    """重放校验:每个批准年度的无维度总额必须与审计证据唯一一致,否则阻断。"""
    if not records:
        raise ADTIngestBlocked(f"{filing.accession_no} 未解析到任何目标 fact")
    by_year: dict[int, set[Decimal]] = {}
    for r in records:
        if not r["dimensions"]:
            by_year.setdefault(r["fy"], set()).add(r["val"])
    for year, expected in filing.expected_totals.items():
        if by_year.get(year) != {expected}:
            raise ADTIngestBlocked(
                f"{filing.accession_no} FY{year} 无维度总额校验失败: 解析得 "
                f"{sorted(str(v) for v in by_year.get(year, set()))},"
                f"审计期望 {expected}"
            )
    unexpected = sorted(set(by_year) - set(filing.expected_totals))
    if unexpected:
        raise ADTIngestBlocked(
            f"{filing.accession_no} 出现批准清单外年度的无维度总额: {unexpected}"
        )


# ── 正式链路 ingest ─────────────────────────────────────────

def ingest_approved_filing(
    filing: ApprovedFiling,
    cache_root: Path = CACHE_ROOT,
    fetch: bool = True,
) -> dict[str, Any]:
    """单个白名单 filing 走完整正式链路:

    SEC Archives 原件 → raw_snapshot_version(filing_xbrl_instance)→
    observation → FetchContext → us_ingest_run → USFactVersionWriter。
    """
    from core.fetchers.us_financial import FetchContext, USFinancialFetcher

    doc_name, html_text, filing_url = fetch_main_document(
        filing.accession_no, cache_root, fetch=fetch)
    records, skipped = extract_cogs_fact_records(html_text, filing)
    verify_against_audit(records, filing)

    content_hash = hashlib.sha256(html_text.encode("utf-8")).hexdigest()
    snapshot_id = get_or_create_raw_snapshot_version(
        stock_code=ADT_STOCK,
        data_type=RAW_DATA_TYPE,
        source=RAW_SOURCE,
        api_params={
            "accession_no": filing.accession_no,
            "cik": ADT_CIK,
            "form": filing.form,
            "main_document_url": f"{filing_url}{doc_name}",
        },
        content_hash=content_hash,
        raw_data={
            "format": "inline_xbrl_html",
            "document": doc_name,
            "accession_no": filing.accession_no,
            "form": filing.form,
            "fiscal_year": filing.fiscal_year,
            "cik": ADT_CIK,
            "content": html_text,
        },
        parser_status="parsed",
    )
    save_raw_snapshot_observation(
        snapshot_id, http_status=200,
        fetch_source="network" if fetch else "cache",
    )

    context = FetchContext(
        stock_code=ADT_STOCK,
        cik=ADT_CIK,
        snapshot_id=snapshot_id,
        content_hash=content_hash,
    )
    fetcher = USFinancialFetcher()
    fetcher._write_version_layer(records, [], "income", context)

    logger.info(
        "ADT %s (%s): %d 条 fact_records 已提交版本层,%d 条 skipped",
        filing.accession_no, filing.form, len(records), len(skipped),
    )
    return {
        "accession_no": filing.accession_no,
        "form": filing.form,
        "fiscal_year": filing.fiscal_year,
        "snapshot_id": snapshot_id,
        "content_hash": content_hash,
        "document": doc_name,
        "filing_url": filing_url,
        "records": records,
        "skipped": skipped,
    }
