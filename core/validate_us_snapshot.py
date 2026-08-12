"""Phase B3b — 美股日常数据校验的版本事实层实现。

三个美股校验（异常值 / 会计等式 / standalone 跨季）的新数据源路径，
由独立开关 ``US_VALIDATION_SNAPSHOT_CURRENT`` 控制（默认关，走 legacy）：

- 消费者可见口径校验（anomalies / logic）：用 ``USFactSelector``
  latest-restated 选择后的值，pivot 成全历史期间行；
- 摄入正确性校验（standalone 跨季）：用选择前的同期间全部 revenues
  事实（USD、无维度、正式 10-Q/10-K），同 accession 内复用 selector 的
  canonical-tag 规则归一。

硬约束（规格 US_PHASE_B3B_VALIDATION_SWITCH_TASK.md §2/§3）：

- pivot 粒度不提前合并：一行 = (stock_code, period_kind, period_start,
  report_date, unit)，保留 form / fiscal_period_raw；
- 合并只允许同 period_kind + 同 period_start 内取非 NULL（会计等式）；
- DB/数据错误直接抛出，不得回退旧宽表路径；
- 缺失输入的跳过/报问题行为与 legacy 完全一致（NULL 不触发问题）；
- 跳过必须按原因计数（stats dict），不得静默跳过。
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Optional

import db
from core.selectors.us_financial import (
    USFactSelector,
    _DISALLOWED_STANDARD_FIELD_TAGS,
    _DISALLOWED_STOCK_FIELD_TAGS,
)
from core.us_financial_exclusion import BUSINESS_REASON_CODES, TECHNICAL_REASON_CODES

logger = logging.getLogger(__name__)

# selector 分块大小（与 scripts/project_us_financial_snapshots.py 一致）
SELECTOR_CHUNK_SIZE = 200

# 校验所需标准字段（规格 §3.3.1）
PIVOT_FIELDS = [
    "revenues",
    "net_income",
    "total_assets",
    "total_liabilities",
    "total_equity",
    "total_equity_including_nci",
    "total_current_assets",
    "cash_and_equivalents",
    "net_cash_from_operations",
]

# instant 字段（资产负债表）与 duration 字段（利润表/现金流量表）
_INSTANT_FIELDS = {
    "total_assets",
    "total_liabilities",
    "total_equity",
    "total_equity_including_nci",
    "total_current_assets",
    "cash_and_equivalents",
}
_DURATION_FIELDS = {"revenues", "net_income", "net_cash_from_operations"}

# standalone / cumulative 期间分类阈值（天数 = report_date - period_start，
# 与 projection 的年度判定 >= 330 天一致）
_STANDALONE_MIN_DAYS = 75
_STANDALONE_MAX_DAYS = 115
_ANNUAL_MIN_DAYS = 330

# standalone 跨季差异阈值：>1% 或 $10M（沿用 legacy）
_CROSS_DIFF_RATIO = 0.01
_CROSS_DIFF_ABS = 10_000_000

_OFFICIAL_FORMS = ("10-Q", "10-Q/A", "10-K", "10-K/A")


def us_validation_snapshot_enabled() -> bool:
    """Phase B3b 独立开关：美股校验走版本事实层。

    默认关闭（legacy）。不复用 B1/B2/B3a 开关；CN 校验不受其影响。
    """
    return os.getenv("US_VALIDATION_SNAPSHOT_CURRENT", "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _d(val: Any) -> Optional[float]:
    """Decimal / None / float 统一转 float 或 None（与 core.validate._d 一致）。"""
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _get_all_us_stocks() -> list[str]:
    rows = db.execute(
        "SELECT stock_code FROM stock_info WHERE market = 'US' "
        "AND (delist_date IS NULL OR delist_date > CURRENT_DATE) ORDER BY stock_code",
        fetch=True,
    ) or []
    return [r[0] for r in rows]


# ──────────────────────────────────────────────────────────
#  版本层 pivot 数据源（规格 §3.3）
# ──────────────────────────────────────────────────────────


def _select_facts_chunked(
    stock_codes: list[str],
    fields: list[str],
) -> list:
    """分块调用 selector（latest-restated）。任何失败直接抛出，不返回部分数据。"""
    selector = USFactSelector()
    all_facts: list = []
    total_chunks = (len(stock_codes) + SELECTOR_CHUNK_SIZE - 1) // SELECTOR_CHUNK_SIZE
    for i in range(0, len(stock_codes), SELECTOR_CHUNK_SIZE):
        chunk = stock_codes[i : i + SELECTOR_CHUNK_SIZE]
        logger.info(
            "Selector chunk %d/%d: %d stocks",
            i // SELECTOR_CHUNK_SIZE + 1,
            total_chunks,
            len(chunk),
        )
        facts = selector.select(
            stock_codes=chunk, basis="latest-restated", fields=fields,
        )
        all_facts.extend(facts)
    return all_facts


def _pivot_facts(facts: list, stats: dict | None = None) -> list[dict]:
    """把 SelectedFact 列表 pivot 成期间行，一行一个期间粒度键。

    行键：(stock_code, period_kind, period_start, report_date, unit)。
    同一行键内不同字段各占一列；同字段多值（不同维度范围的事实撞键）时
    优先取无维度事实，其余情况保留先见值并按 pivot_field_collision 计数。

    不做任何跨 period_start / 跨 period_kind 合并。
    """
    if stats is None:
        stats = {}
    rows: dict[tuple, dict] = {}
    for f in facts:
        unit = (f.unit or "").upper()
        if unit != "USD":
            stats["non_usd_skipped"] = stats.get("non_usd_skipped", 0) + 1
            continue
        key = (f.stock_code, f.period_kind, f.period_start, f.report_date, unit)
        row = rows.get(key)
        if row is None:
            row = {
                "stock_code": f.stock_code,
                "period_kind": f.period_kind,
                "period_start": f.period_start,
                "report_date": f.report_date,
                "unit": unit,
                "forms": set(),
                "fiscal_periods": set(),
            }
            rows[key] = row
        if f.form:
            row["forms"].add(f.form)
        if f.fiscal_period_raw:
            row["fiscal_periods"].add(str(f.fiscal_period_raw))
        val = _d(f.value_numeric)
        if val is None:
            continue
        field_name = f.standard_field
        existing = row.get(field_name)
        if existing is None:
            row[field_name] = val
            row[f"_{field_name}__dimless"] = not f.dimensions
        elif existing != val:
            # 撞键：优先无维度事实；同维度性时保留先见值
            stats["pivot_field_collision"] = stats.get("pivot_field_collision", 0) + 1
            if not f.dimensions and not row.get(f"_{field_name}__dimless"):
                row[field_name] = val
                row[f"_{field_name}__dimless"] = True

    out = sorted(
        rows.values(),
        key=lambda r: (
            r["stock_code"],
            r["report_date"] or date.min,
            r["period_kind"] or "",
            r["period_start"] or date.min,
        ),
    )
    return out


def load_validation_pivot(
    stock_codes: list[str] | None = None,
    stats: dict | None = None,
) -> list[dict]:
    """latest-restated 全历史 pivot（annual + quarterly，USD duration/instant）。

    stock_codes 为 None 时取全部美股。selector 失败直接抛出。
    """
    if stock_codes is None:
        stock_codes = _get_all_us_stocks()
    if not stock_codes:
        raise RuntimeError("validation pivot: no US stocks found in stock_info")
    facts = _select_facts_chunked(stock_codes, PIVOT_FIELDS)
    if not facts:
        raise RuntimeError(
            "validation pivot: selector returned no facts "
            f"for {len(stock_codes)} stocks"
        )
    rows = _pivot_facts(facts, stats)
    logger.info("validation pivot: %d period rows from %d facts", len(rows), len(facts))
    return rows


# ──────────────────────────────────────────────────────────
#  1. 异常值检测（消费者可见口径，latest-restated 选择后的值）
# ──────────────────────────────────────────────────────────


def check_anomalies_us_snapshot(
    pivot_rows: list[dict],
    issues: list,
    stats: dict | None = None,
) -> int:
    """对 pivot 期间行做异常值检测。返回扫描行数。

    按期间粒度逐行校验（相当于旧表的 report_type 分行），不跨期间合并：
    - instant 行：负资产、资产负债率 > 200%；
    - duration 行：净利 > 1.5×收入、CFO 与净利背离。
    缺失字段（NULL）与 legacy 一致：跳过该项检查，不报问题。
    """
    from core.validate import ValidationIssue

    scanned = 0
    for row in pivot_rows:
        rd = str(row["report_date"])
        stock_code = row["stock_code"]

        if row["period_kind"] == "instant":
            if not any(row.get(f) is not None for f in _INSTANT_FIELDS):
                continue
            scanned += 1
            total_assets = row.get("total_assets")
            total_liabilities = row.get("total_liabilities")

            # 负资产
            if total_assets is not None and total_assets < 0:
                issues.append(
                    ValidationIssue(
                        stock_code=stock_code,
                        market="US",
                        report_date=rd,
                        check_name="negative_total_assets",
                        severity="error",
                        field_name="total_assets",
                        actual_value=str(total_assets),
                        message=f"Negative total assets: {total_assets:,.0f}",
                        suggestion="Data entry error or going concern issue",
                    )
                )

            # 资产负债率 > 200%
            if total_assets and total_liabilities:
                ratio = total_liabilities / total_assets
                if ratio > 2.0:
                    issues.append(
                        ValidationIssue(
                            stock_code=stock_code,
                            market="US",
                            report_date=rd,
                            check_name="debt_ratio_extreme",
                            severity="warning",
                            field_name="total_liabilities/total_assets",
                            actual_value=f"{ratio:.2%}",
                            expected_value="< 200%",
                            message=f"Debt ratio {ratio:.1%} exceeds 200%",
                            suggestion="Possibly insolvent",
                        )
                    )

        elif row["period_kind"] == "duration":
            if not any(row.get(f) is not None for f in _DURATION_FIELDS):
                continue
            scanned += 1
            revenues = row.get("revenues")
            net_income = row.get("net_income")
            cfo = row.get("net_cash_from_operations")

            # 净利润超过营收
            if net_income is not None and revenues is not None:
                if revenues > 0 and net_income > revenues * 1.5:
                    issues.append(
                        ValidationIssue(
                            stock_code=stock_code,
                            market="US",
                            report_date=rd,
                            check_name="net_income_exceeds_revenue",
                            severity="warning",
                            field_name="net_income/revenues",
                            actual_value=f"net_income={net_income:,.0f}, revenues={revenues:,.0f}",
                            message=f"net_income ({net_income:,.0f}) far exceeds revenues ({revenues:,.0f})",
                            suggestion="Possible large non-recurring gains",
                        )
                    )

            # CFO 与净利润背离
            if cfo is not None and net_income is not None:
                if net_income > 0 and cfo < 0:
                    issues.append(
                        ValidationIssue(
                            stock_code=stock_code,
                            market="US",
                            report_date=rd,
                            check_name="cfo_negative_income_positive",
                            severity="warning",
                            field_name="net_cash_from_operations/net_income",
                            actual_value=f"CFO={cfo:,.0f}, net_income={net_income:,.0f}",
                            message="Positive net income but negative operating cash flow",
                            suggestion="Earnings quality is questionable",
                        )
                    )

    return scanned


# ──────────────────────────────────────────────────────────
#  2. 逻辑一致性检查（会计等式，含 NCI fallback）
# ──────────────────────────────────────────────────────────


def _merge_same_period_rows(pivot_rows: list[dict]) -> list[dict]:
    """按 (stock_code, report_date, period_kind, period_start) 合并取非 NULL。

    规格 §3.3.3 固定规则：check_logic_us 确需按报告日合并时，只允许合并
    **同 period_kind、同 period_start** 的行取非 NULL 值（对应旧实现中
    annual/quarterly 两行明细互补的语义）；跨 period_start 的事实不得合并。
    pivot 行在该键上本就唯一，此合并是防御性的，同时把规则固化在代码里。
    """
    merged: dict[tuple, dict] = {}
    for row in pivot_rows:
        key = (
            row["stock_code"],
            row["report_date"],
            row["period_kind"],
            row["period_start"],
        )
        target = merged.get(key)
        if target is None:
            target = {
                "stock_code": row["stock_code"],
                "report_date": row["report_date"],
                "period_kind": row["period_kind"],
                "period_start": row["period_start"],
            }
            merged[key] = target
        for f in _INSTANT_FIELDS:
            if target.get(f) is None and row.get(f) is not None:
                target[f] = row[f]
    return [merged[k] for k in sorted(merged, key=lambda k: (k[0], k[1] or date.min))]


def check_logic_us_snapshot(
    pivot_rows: list[dict],
    issues: list,
    stats: dict | None = None,
) -> int:
    """美股会计等式检查（latest-restated 选择后的值）。返回扫描行数。

    合并规则见 _merge_same_period_rows：仅同 period_kind + 同 period_start。
    与 legacy 一致，只有 total_assets / total_liabilities / total_equity
    三者均非 NULL 的行才计入扫描并参与检查（缺失则跳过，不报问题）。
    """
    from core.validate import ValidationIssue

    tolerance_ratio = 0.01
    scanned = 0

    for row in _merge_same_period_rows(pivot_rows):
        total_assets = row.get("total_assets")
        total_liabilities = row.get("total_liabilities")
        total_equity = row.get("total_equity")
        if not (total_assets and total_liabilities is not None and total_equity is not None):
            continue  # 缺失语义与 legacy 一致：WHERE 三字段非 NULL 才扫描
        scanned += 1

        stock_code = row["stock_code"]
        rd = str(row["report_date"])
        total_equity_nci = row.get("total_equity_including_nci")
        current_assets = row.get("total_current_assets")
        cash_equiv = row.get("cash_and_equivalents")

        # 会计等式
        rhs = total_liabilities + total_equity
        if total_assets != 0:
            diff_ratio = abs(total_assets - rhs) / abs(total_assets)
            if diff_ratio > tolerance_ratio:
                # 尝试用 total_equity_including_nci
                if total_equity_nci is not None:
                    rhs2 = total_liabilities + total_equity_nci
                    diff2 = abs(total_assets - rhs2) / abs(total_assets)
                    if diff2 <= tolerance_ratio:
                        continue  # 用含 NCI 的权益就平了，跳过
                issues.append(
                    ValidationIssue(
                        stock_code=stock_code,
                        market="US",
                        report_date=rd,
                        check_name="balance_equation",
                        severity="error",
                        field_name="total_assets vs total_liabilities + total_equity",
                        actual_value=f"assets={total_assets:,.0f}, liab+equity={rhs:,.0f}, diff={diff_ratio:.2%}",
                        expected_value="diff < 1%",
                        message=f"Balance sheet equation off by {diff_ratio:.2%}",
                        suggestion="Check if NCI (non-controlling interest) is recorded separately",
                    )
                )

        # 流动资产 >= 现金
        if current_assets is not None and cash_equiv is not None:
            if cash_equiv > current_assets and current_assets >= 0:
                issues.append(
                    ValidationIssue(
                        stock_code=stock_code,
                        market="US",
                        report_date=rd,
                        check_name="cash_exceeds_current_assets",
                        severity="error",
                        field_name="cash_and_equivalents vs total_current_assets",
                        actual_value=f"cash={cash_equiv:,.0f}, current_assets={current_assets:,.0f}",
                        expected_value="cash <= current_assets",
                        message=f"Cash ({cash_equiv:,.0f}) > current assets ({current_assets:,.0f})",
                        suggestion="Data may be incorrect",
                    )
                )

    return scanned


# ──────────────────────────────────────────────────────────
#  3. standalone 跨季交叉校验（选择前事实，摄入正确性）
# ──────────────────────────────────────────────────────────


def _load_standalone_revenue_candidates() -> list[dict]:
    """加载选择前的 revenues 候选事实（规格 §3.4.1）。

    仅取 USD、无维度、正式 10-Q/10-K（含 /A）、duration、非 NULL 的事实；
    与 selector 一样排除 active exclusion（已人工定性不可用的值不参与，
    避免把已剔除的坏值报成摄入问题）。DB 错误直接抛出。
    """
    sql = """
    SELECT
        f.fact_version_id,
        f.stock_code,
        f.accession_no,
        f.sec_tag,
        f.period_start,
        f.report_date,
        f.form,
        f.filed_date,
        f.value_numeric
    FROM us_financial_fact_version f
    LEFT JOIN us_financial_fact_exclusion e
      ON e.fact_version_id = f.fact_version_id
     AND e.status = 'active'
     AND (
         e.reason_code = ANY(%s)
         OR (
             e.reason_code = ANY(%s)
             AND e.effective_from::date <= %s
         )
     )
    WHERE e.fact_version_id IS NULL
      AND f.standard_field = 'revenues'
      AND f.period_kind = 'duration'
      AND f.unit = 'USD'
      AND f.value_numeric IS NOT NULL
      AND upper(f.form) IN ('10-Q', '10-Q/A', '10-K', '10-K/A')
      AND f.dimensions = '{}'::jsonb
    ORDER BY f.stock_code, f.report_date, f.period_start, f.filed_date
    """
    params = (
        list(TECHNICAL_REASON_CODES),
        list(BUSINESS_REASON_CODES),
        datetime.now().date(),
    )
    rows = db.execute(sql, params, fetch=True) or []
    cols = [
        "fact_version_id",
        "stock_code",
        "accession_no",
        "sec_tag",
        "period_start",
        "report_date",
        "form",
        "filed_date",
        "value_numeric",
    ]
    return [dict(zip(cols, row)) for row in rows]


def _normalize_standalone_candidates(
    candidates: list[dict],
    stats: dict,
) -> list[dict]:
    """同 accession 内 canonical-tag 归一 + 歧义计数（规格 §3.4.2）。

    复用 selector 的 canonical-tag 优先级与 per-stock 禁用规则
    （_filter_canonical_tag_candidates），不另写一套。归一后同一
    (stock, accession, period_start, report_date) 仍有多个不同值时按
    ambiguous_candidates 计数并丢弃该组。

    输出条目粒度为 (stock, period_start, report_date, sec_tag)，每个条目带
    **全部披露版本**（versions，按 filed_date 升序）：

    - 跨 accession 重述不做"取最新"的取舍——跨季比较必须与 cumulative
      同一披露 vintage（pairing 时按 cumulative 的 filed_date 截取
      as-of 版本），否则把不同 vintage 的 standalone 混进同一次累加
      会制造假阳性（AWI：2018 重述的 Q1 配上 2017 原报的 Q2）；
    - 同 accession 内与 canonical 值相等的其他 tag 记录为同值别名
      （如 CBRE 同一披露中 Revenues == RFC），配对时该值对这些 tag
      都可用，不算跨 tag 混比；不同值的 tag 各自独立成条目
      （规格 §3.4"防止不同 tag 的重述值被混合制造假阳性"）。
    """
    # 按 (stock, period_start, report_date) 分组后交给 selector 的同 accession 归一
    by_period: dict[tuple, list[dict]] = {}
    for c in candidates:
        key = (c["stock_code"], c["period_start"], c["report_date"])
        by_period.setdefault(key, []).append(c)

    normalized: list[dict] = []
    for (stock_code, period_start, report_date), group in by_period.items():
        # selector 静态规则需要 standard_field / dimensions 键
        for c in group:
            c.setdefault("standard_field", "revenues")
            c.setdefault("dimensions", {})

        # 按 accession 分组做 canonical 归一（selector 静态规则本就按 accession 内部
        # 分组，逐 accession 调用等价）
        ambiguous = False
        # tag -> {(accession): version}（同 accession 同 tag 只应一条）
        tag_versions: dict[str, dict[str, dict]] = {}
        by_accession_raw: dict[str, list[dict]] = {}
        for c in group:
            by_accession_raw.setdefault(str(c.get("accession_no") or ""), []).append(c)
        for accession, facts in by_accession_raw.items():
            kept = USFactSelector._filter_canonical_tag_candidates(facts)
            values = {float(c["value_numeric"]) for c in kept}
            if len(values) > 1:
                ambiguous = True
                break
            if not kept:
                continue
            pick = kept[0]
            kept_value = float(pick["value_numeric"])
            # 同值别名 tag（排除 disallowed）
            alias_tags = {
                str(f.get("sec_tag") or "")
                for f in facts
                if f.get("value_numeric") is not None
                and float(f["value_numeric"]) == kept_value
                and ("revenues", str(f.get("sec_tag") or ""))
                not in _DISALLOWED_STANDARD_FIELD_TAGS
                and (
                    stock_code,
                    "revenues",
                    str(f.get("sec_tag") or ""),
                )
                not in _DISALLOWED_STOCK_FIELD_TAGS
            } or {str(pick.get("sec_tag") or "")}
            for tag in alias_tags:
                tag_versions.setdefault(tag, {})[accession] = {
                    "value": kept_value,
                    "accession_no": pick.get("accession_no"),
                    "filed_date": pick.get("filed_date"),
                    "form": pick.get("form"),
                }
        if ambiguous:
            stats["ambiguous_candidates"] = stats.get("ambiguous_candidates", 0) + 1
            continue

        days = (report_date - period_start).days if period_start and report_date else None
        for tag, versions_by_acc in tag_versions.items():
            versions = sorted(
                versions_by_acc.values(),
                key=lambda v: (v.get("filed_date") or date.min, str(v.get("accession_no") or "")),
            )
            normalized.append(
                {
                    "stock_code": stock_code,
                    "sec_tag": tag,
                    "period_start": period_start,
                    "report_date": report_date,
                    "period_days": days,
                    "versions": versions,
                }
            )
    return normalized


def _classify_period(days: int | None) -> str:
    """standalone（≈90 天）/ cumulative（同财年更长 YTD）/ annual（>=330 天）。"""
    if days is None:
        return "unknown"
    if days >= _ANNUAL_MIN_DAYS:
        return "annual"
    if _STANDALONE_MIN_DAYS <= days <= _STANDALONE_MAX_DAYS:
        return "standalone"
    if _STANDALONE_MAX_DAYS < days < _ANNUAL_MIN_DAYS:
        return "cumulative"
    return "unknown"


def _fiscal_year_of(report_date: date, fy_end_month: int) -> int:
    """与 legacy 相同的财年归属：报告月份 > 财年末月份则归下一财年。"""
    if report_date.month > fy_end_month:
        return report_date.year + 1
    return report_date.year


def _latest_version_as_of(entry: dict, ceiling: date | None) -> dict | None:
    """条目在 ceiling（含）之前最新披露版本；无则 None。"""
    chosen = None
    for v in entry["versions"]:
        fd = v.get("filed_date")
        if ceiling is None or fd is None or fd <= ceiling:
            if chosen is None or (
                (fd or date.min), str(v.get("accession_no") or "")
            ) > (
                (chosen.get("filed_date") or date.min),
                str(chosen.get("accession_no") or ""),
            ):
                chosen = v
    return chosen


def _tile_standalone_chain(
    standalones: list[dict],
    start: date,
    end: date,
    ceiling: date | None,
    tolerance_days: int = 3,
) -> list[dict] | None:
    """用 standalone 季度事实连续平铺 [start, end]；无法铺满返回 None。

    vintage 对齐：每段取 filed_date <= ceiling（cumulative 的 filed_date）
    的最新版本，保证链与 cumulative 处于同一披露时点口径。
    返回值为选中的 version 列表。
    """
    by_start = sorted(standalones, key=lambda c: c["period_start"])
    chain: list[dict] = []
    cursor = start
    while True:
        entry = next(
            (
                c
                for c in by_start
                if abs((c["period_start"] - cursor).days) <= tolerance_days
            ),
            None,
        )
        if entry is None:
            return None
        version = _latest_version_as_of(entry, ceiling)
        if version is None:
            return None
        chain.append({**version, "report_date": entry["report_date"]})
        if abs((entry["report_date"] - end).days) <= tolerance_days:
            return chain
        cursor = entry["report_date"] + timedelta(days=1)
        if len(chain) > 4:
            return None


def _check_stock_standalone(
    stock_code: str,
    candidates: list[dict],
    issues: list,
    stats: dict,
) -> None:
    """单只股票的 standalone→cumulative 跨季校验（选择前事实）。"""
    from core.validate import ValidationIssue

    annual = [c for c in candidates if c["period_class"] == "annual"]
    standalones = [c for c in candidates if c["period_class"] == "standalone"]
    cumulatives = [c for c in candidates if c["period_class"] == "cumulative"]

    # ── 行级检查：负 standalone / 负 cumulative（取各条目最新披露版本）──
    # 同期间可能保留多个 tag 的条目，按 (类别, report_date) 去重，避免同键重复报警
    reported_negative: set[tuple] = set()
    for c in standalones:
        value = c["versions"][-1]["value"]
        if value < 0 and ("std", c["report_date"]) not in reported_negative:
            reported_negative.add(("std", c["report_date"]))
            issues.append(
                ValidationIssue(
                    stock_code=stock_code,
                    market="US",
                    report_date=str(c["report_date"]),
                    check_name="negative_standalone_revenue",
                    severity="warning",
                    field_name="revenues_standalone",
                    actual_value=str(value),
                    expected_value="> 0",
                    message=f"Negative standalone revenue: {value:,.0f}",
                    suggestion="Check raw SEC data: negative quarterly revenue is unusual.",
                )
            )
    for c in cumulatives:
        value = c["versions"][-1]["value"]
        if value < 0 and ("cum", c["report_date"]) not in reported_negative:
            reported_negative.add(("cum", c["report_date"]))
            issues.append(
                ValidationIssue(
                    stock_code=stock_code,
                    market="US",
                    report_date=str(c["report_date"]),
                    check_name="negative_cumulative_revenue",
                    severity="warning",
                    field_name="revenues",
                    actual_value=str(value),
                    expected_value="> 0",
                    message=f"Negative cumulative revenue: {value:,.0f}",
                    suggestion="Check raw SEC data.",
                )
            )

    # ── 财年边界推导（沿用 legacy：由年度披露的最大报告期月份推出）──
    if not annual:
        # 无法确定财年：所有 cumulative 候选都无法配对，按原因计数
        stats["undeterminable_fiscal_year"] = stats.get(
            "undeterminable_fiscal_year", 0
        ) + len(cumulatives)
        return
    fy_end_month = max(c["report_date"] for c in annual).month

    # ── 按财年分组 ──
    fy_standalones: dict[int, list[dict]] = {}
    for c in standalones:
        fy = _fiscal_year_of(c["report_date"], fy_end_month)
        fy_standalones.setdefault(fy, []).append(c)
    fy_cumulatives: dict[int, list[dict]] = {}
    for c in cumulatives:
        fy = _fiscal_year_of(c["report_date"], fy_end_month)
        fy_cumulatives.setdefault(fy, []).append(c)

    all_fys = sorted(set(fy_standalones) | set(fy_cumulatives))
    for fy in all_fys:
        fy_std = sorted(fy_standalones.get(fy, []), key=lambda c: c["report_date"])
        fy_cum = sorted(fy_cumulatives.get(fy, []), key=lambda c: c["report_date"])

        # 缺 cumulative：财年内有 Q2+ 的 standalone（即期初之后还有季度），
        # 但没有同 tag 的 cumulative 覆盖到该 standalone 的报告日
        if fy_std:
            fy_start = min(c["period_start"] for c in fy_std)
            for st in fy_std:
                if abs((st["period_start"] - fy_start).days) <= 3:
                    continue  # Q1 不需要 cumulative
                covered = any(
                    abs((c["report_date"] - st["report_date"]).days) <= 3
                    and abs((c["period_start"] - fy_start).days) <= 3
                    and c["sec_tag"] == st["sec_tag"]
                    for c in fy_cum
                )
                if not covered:
                    stats["missing_cumulative"] = stats.get("missing_cumulative", 0) + 1

        for cum in fy_cum:
            # Q4 排除：cumulative 结束于财年末（其累计口径由年度行承载，
            # 与 legacy 的 quarter_num < max_quarter_num 一致）
            if cum["report_date"].month == fy_end_month:
                stats["q4_excluded"] = stats.get("q4_excluded", 0) + 1
                continue

            # 配对（规格 §3.4.4）：取该 (期间, tag) 条目的最新披露版本，
            # standalone 链同 tag、同期间起点、铺满区间、vintage 对齐
            # （链上每段取 cumulative filed_date 之前的最新版本），且末段
            # standalone 与 cumulative 来自同一 accession（同一披露来源）。
            v = cum["versions"][-1]
            same_tag_std = [c for c in fy_std if c["sec_tag"] == cum["sec_tag"]]
            chain = _tile_standalone_chain(
                same_tag_std,
                cum["period_start"],
                cum["report_date"],
                v.get("filed_date"),
            )
            if chain is None or chain[-1]["accession_no"] != v["accession_no"]:
                stats["missing_standalone"] = stats.get("missing_standalone", 0) + 1
                continue

            running_sum = sum(c["value"] for c in chain)
            cum_rev = v["value"]
            discrepancy = abs(cum_rev - running_sum)
            if running_sum <= 0:
                continue  # 与 legacy running_std_sum > 0 一致
            if discrepancy > max(abs(cum_rev) * _CROSS_DIFF_RATIO, _CROSS_DIFF_ABS):
                qn = len(chain)
                issues.append(
                    ValidationIssue(
                        stock_code=stock_code,
                        market="US",
                        report_date=str(cum["report_date"]),
                        check_name="standalone_cross_quarter_sum",
                        severity="error",
                        field_name="revenues",
                        actual_value=f"cumulative={cum_rev:,.0f}, sum_standalone={running_sum:,.0f}",
                        expected_value="difference < 1% or $10M",
                        message=(
                            f"FY{fy} Q{qn}: cumulative revenue ({cum_rev:,.0f}) != "
                            f"sum of standalone Q1..Q{qn} ({running_sum:,.0f}), "
                            f"diff={discrepancy:,.0f}"
                            + (
                                f" ({discrepancy/cum_rev*100:.1f}%)"
                                if cum_rev and cum_rev != 0
                                else ""
                            )
                        ),
                        suggestion="Check raw SEC data for missing or misclassified quarters.",
                    )
                )


def check_standalone_cross_validation_us_snapshot(
    issues: list,
    stats: dict | None = None,
    candidates: list[dict] | None = None,
) -> int:
    """standalone 跨季交叉校验（选择前事实）。返回扫描候选事实数。

    跳过按四类原因计数并写入 stats：missing_standalone /
    missing_cumulative / ambiguous_candidates / undeterminable_fiscal_year
    （另有 q4_excluded 透明计数）。不得静默跳过。
    """
    if stats is None:
        stats = {}
    if candidates is None:
        candidates = _load_standalone_revenue_candidates()
    normalized = _normalize_standalone_candidates(candidates, stats)
    for c in normalized:
        c["period_class"] = _classify_period(c["period_days"])

    by_stock: dict[str, list[dict]] = {}
    for c in normalized:
        by_stock.setdefault(c["stock_code"], []).append(c)
    for stock_code in sorted(by_stock):
        _check_stock_standalone(stock_code, by_stock[stock_code], issues, stats)

    scanned = len(normalized)
    logger.info(
        "standalone cross validation: %d candidates scanned, skips=%s",
        scanned,
        {
            k: stats.get(k, 0)
            for k in (
                "missing_standalone",
                "missing_cumulative",
                "ambiguous_candidates",
                "undeterminable_fiscal_year",
                "q4_excluded",
            )
        },
    )
    return scanned


# ──────────────────────────────────────────────────────────
#  汇总入口：run_validation 的 US 新路径
# ──────────────────────────────────────────────────────────


def run_us_snapshot_checks(
    issues: list,
    stats: dict | None = None,
) -> dict[str, int]:
    """依次执行三个美股校验（版本事实层路径），返回各项扫描行数。

    pivot 只加载一次供 anomalies / logic 共用；standalone 校验用独立的
    选择前候选事实。任何 DB/selector 错误直接向上抛，不得回退 legacy。
    """
    if stats is None:
        stats = {}
    pivot_rows = load_validation_pivot(stats=stats)
    scanned_anomalies = check_anomalies_us_snapshot(pivot_rows, issues, stats)
    scanned_logic = check_logic_us_snapshot(pivot_rows, issues, stats)
    scanned_standalone = check_standalone_cross_validation_us_snapshot(
        issues, stats=stats
    )
    return {
        "anomalies": scanned_anomalies,
        "logic": scanned_logic,
        "standalone": scanned_standalone,
    }
