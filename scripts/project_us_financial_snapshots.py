#!/usr/bin/env python3
"""版本层财务快照生成（Phase A）。

从 latest-restated selector 生成两张快照表：
  us_financial_current_annual — 每只股票最近 5 个年度
  us_financial_current_ttm   — 每只股票一行 TTM

用法:
  python scripts/project_us_financial_snapshots.py          # 全部 1,003 只
  python scripts/project_us_financial_snapshots.py --dry-run # 不写库
  python scripts/project_us_financial_snapshots.py --stocks AAPL,WMT,HD
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

# 确保项目根目录在 Python path 中
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pandas as pd
import psycopg2.extras

from config import db as db_config
from core.selectors.us_financial import USFactSelector
from core.us_financial_versioning import ANNUAL_FORMS
from db import Connection

logger = logging.getLogger(__name__)

SELECTOR_CHUNK_SIZE = 200

# ── 需要从 selector 获取的标准字段 ─────────────────────────────

ANNUAL_STANDARD_FIELDS = [
    "revenues",
    "net_income",
    "net_income_common",
    "total_assets",
    "total_liabilities",
    "total_equity",
    "total_equity_including_nci",
    "net_cash_from_operations",
    "capital_expenditures",
    "cost_of_goods_sold",
    "gross_profit",
    "operating_income",
    "eps_basic",
    "eps_diluted",
    "weighted_avg_shares_basic",
    "total_current_assets",
    "total_current_liabilities",
    "inventory_net",
]

# TTM 主口径（net_income_common 单独处理，避免混用 consolidated/common 组件）
TTM_FIELDS = [
    "revenues",
    "net_income",
    "net_cash_from_operations",
    "capital_expenditures",
]

# TTM 利润双口径：native consolidated + common attributable
TTM_NET_INCOME_FIELDS = ["net_income", "net_income_common"]

# TTM 组件计算使用的全部字段（主口径 + 净利润双口径，保持顺序且去重）
TTM_COMPONENT_FIELDS = list(dict.fromkeys(TTM_FIELDS + TTM_NET_INCOME_FIELDS))


# ── 辅助函数 ──────────────────────────────────────────────────

def _is_annual_period(fact) -> bool:
    """检查 SelectedFact 的期间是否 ≥ 330 天（排除 Q4 单季数据）。"""
    if fact.period_kind == "instant":
        return True
    if fact.period_kind == "duration" and fact.period_start and fact.report_date:
        return (fact.report_date - fact.period_start).days >= 330
    return False


def _to_decimal(val: Any) -> Decimal | None:
    if val is None:
        return None
    try:
        if isinstance(val, float) and pd.isna(val):
            return None
    except Exception:
        pass
    try:
        return Decimal(str(val))
    except Exception:
        return None


def _safe_div(a, b) -> Decimal | None:
    ad = _to_decimal(a)
    bd = _to_decimal(b)
    if ad is not None and bd is not None and bd != 0:
        return ad / bd
    return None


# ── 52/53 周 TTM 期间白名单 ────────────────────────────────────

DEFAULT_TTM_52_53_ALLOWLIST_PATH = Path("docs/core/US_TTM_52_53_WEEK_ALLOWLIST.csv")


def _parse_date_str(value: str) -> date | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def load_ttm_52_53_allowlist(path: Path | str | None = None) -> set[tuple[str, date, date, str]]:
    """加载 52/53 周 TTM 期间白名单。

    每行定义一个被精确允许的最新期/去年同期配对：
      (stock_code, latest_report_date, prior_year_report_date, fiscal_period_raw)
    只有该配对精确命中且 4 <= period_diff <= 7 时，才放宽 TTM 可比性判断。
    """
    if path is None:
        path = DEFAULT_TTM_52_53_ALLOWLIST_PATH
    p = Path(path)
    allowlist: set[tuple[str, date, date, str]] = set()
    if not p.exists():
        logger.warning("52/53-week allowlist not found: %s", p)
        return allowlist
    with open(p, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            stock = (row.get("stock_code") or "").strip().upper()
            latest = _parse_date_str(row.get("latest_report_date") or "")
            prior = _parse_date_str(row.get("prior_year_report_date") or "")
            fp = (row.get("fiscal_period_raw") or "").strip().upper()
            if stock and latest and prior and fp:
                allowlist.add((stock, latest, prior, fp))
    logger.info("Loaded %d 52/53-week allowlist entries from %s", len(allowlist), p)
    return allowlist


def is_allowlisted_52_53_pair(
    allowlist: set[tuple[str, date, date, str]] | None,
    stock_code: str,
    latest_report_date: date,
    prior_year_report_date: date,
    latest_period_days: int | None,
    prior_year_period_days: int | None,
    fiscal_period_raw: str | None,
) -> bool:
    """判断该期间配对是否在 52/53 周白名单中。

    调用者已确认 4 <= period_diff <= 7；本函数只做精确名单命中检查。
    """
    if not allowlist:
        return False
    if not fiscal_period_raw:
        return False
    key = (
        stock_code.upper(),
        latest_report_date,
        prior_year_report_date,
        str(fiscal_period_raw).strip().upper(),
    )
    return key in allowlist


# ── 数据采集 ──────────────────────────────────────────────────

def _get_all_us_stocks() -> list[str]:
    with Connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT stock_code FROM stock_info WHERE market = 'US' "
                "AND (delist_date IS NULL OR delist_date > CURRENT_DATE) ORDER BY stock_code")
            return [r[0] for r in cur.fetchall()]


def _fetch_facts(stock_codes: list[str], fields: list[str]) -> list:
    """分批调用 selector 获取 latest-restated 事实。"""
    selector = USFactSelector()
    all_facts = []
    for i in range(0, len(stock_codes), SELECTOR_CHUNK_SIZE):
        chunk = stock_codes[i:i + SELECTOR_CHUNK_SIZE]
        logger.info("Selector chunk %d/%d: %d stocks",
                     i // SELECTOR_CHUNK_SIZE + 1,
                     (len(stock_codes) + SELECTOR_CHUNK_SIZE - 1) // SELECTOR_CHUNK_SIZE,
                     len(chunk))
        facts = selector.select(stock_codes=chunk, basis="latest-restated", fields=fields)
        all_facts.extend(facts)
    return all_facts


# ── 年度快照构建 ──────────────────────────────────────────────

def build_annual_snapshot(all_facts: list, projection_run_id: str) -> pd.DataFrame:
    """从 SelectedFact 构建年度快照 DataFrame。

    过滤条件：form 在 ANNUAL_FORMS + unit=USD + duration 类型期间 ≥ 330 天。
    """
    annual = [
        f for f in all_facts
        if f.form and f.form.upper() in ANNUAL_FORMS
        and f.unit.upper() == "USD"
        and _is_annual_period(f)
    ]
    logger.info("Annual USD facts (with period check): %d", len(annual))

    if not annual:
        return pd.DataFrame()

    # 按 (stock_code, report_date, standard_field) pivot
    # 元数据（filed_date/accession_no/form）按每个 (stock_code, report_date)
    # 取最新的 filed_date 对应的记录（最接近当前披露）
    records: dict[tuple, dict] = {}
    for f in annual:
        key = (f.stock_code, f.report_date)
        if key not in records:
            records[key] = {
                "stock_code": f.stock_code,
                "report_date": f.report_date,
                "filed_date": f.filed_date,
                "accession_no": f.accession_no,
                "form": f.form,
            }
        else:
            # 保留最晚的 filed_date 对应的 accession（更可能是 latest-restated 选中的披露）
            existing_fd = records[key].get("filed_date")
            if f.filed_date and (existing_fd is None or f.filed_date > existing_fd):
                records[key]["filed_date"] = f.filed_date
                records[key]["accession_no"] = f.accession_no
                records[key]["form"] = f.form
        val = _to_decimal(f.value_numeric)
        if val is not None:
            records[key][f.standard_field] = val

    df = pd.DataFrame(list(records.values()))

    # 确保列存在
    for col in ANNUAL_STANDARD_FIELDS:
        if col not in df.columns:
            df[col] = None

    return _compute_derived_fields(df, projection_run_id)


def _compute_derived_fields(df: pd.DataFrame, projection_run_id: str) -> pd.DataFrame:
    """计算衍生字段：FCF、ROE、margins、YoY 等。

    遵循同口径语义：
    - total_equity / net_income 为原生 parent/consolidated 口径，不做 fallback 回填；
    - 仅在明确允许的路径下使用 including_nci / common 作为 fallback，并打 quality flag。
    """
    # 确保原始列存在
    for col in ANNUAL_STANDARD_FIELDS:
        if col not in df.columns:
            df[col] = None

    # 逐行 quality_flags（list of str）
    df["quality_flags"] = [[] for _ in range(len(df))]

    # FCF
    if "net_cash_from_operations" in df.columns and "capital_expenditures" in df.columns:
        df["fcf"] = df.apply(
            lambda r: (r["net_cash_from_operations"] - r["capital_expenditures"])
            if pd.notna(r.get("net_cash_from_operations")) and pd.notna(r.get("capital_expenditures"))
            else None, axis=1,
        )
    else:
        df["fcf"] = None

    # 毛利率：优先原生 gross_profit；缺失时可用 revenues - cost_of_goods_sold 推导
    def _gross_margin(r):
        gp = _to_decimal(r.get("gross_profit"))
        rev = _to_decimal(r.get("revenues"))
        cogs = _to_decimal(r.get("cost_of_goods_sold"))
        if gp is not None and rev is not None and rev != 0:
            return gp / rev
        if rev is not None and cogs is not None and rev != 0:
            idx = r.name if hasattr(r, "name") else None
            if idx is not None:
                df.at[idx, "quality_flags"].append("gross_profit_derived_from_cogs")
            return (rev - cogs) / rev
        return None

    df["gross_margin"] = df.apply(_gross_margin, axis=1)
    df["operating_margin"] = df.apply(lambda r: _safe_div(r.get("operating_income"), r.get("revenues")), axis=1)

    # ROE 四象限
    def _roe(r):
        ni = _to_decimal(r.get("net_income"))
        nic = _to_decimal(r.get("net_income_common"))
        te = _to_decimal(r.get("total_equity"))
        tei = _to_decimal(r.get("total_equity_including_nci"))
        idx = r.name if hasattr(r, "name") else None

        if ni is not None and te is not None and te != 0:
            return ni / te
        if ni is not None and tei is not None and tei != 0:
            if idx is not None:
                df.at[idx, "quality_flags"].append("roe_equity_including_nci_fallback")
            return ni / tei
        if nic is not None and te is not None and te != 0:
            if idx is not None:
                df.at[idx, "quality_flags"].append("net_income_common_fallback")
            return nic / te
        # 混合口径拒绝仅指：唯一可算路径是被禁止的 nic÷tei 双 fallback。
        # 其他情况（缺分子、缺分母、零分母）是普通缺失，不打此 flag。
        if ni is None and nic is not None and te is None and tei is not None and tei != 0:
            if idx is not None:
                df.at[idx, "quality_flags"].append("roe_mixed_basis_rejected")
        return None

    df["roe"] = df.apply(_roe, axis=1)

    # ROA：native net_income 优先，缺失时可用 net_income_common
    def _roa(r):
        ni = _to_decimal(r.get("net_income"))
        nic = _to_decimal(r.get("net_income_common"))
        ta = _to_decimal(r.get("total_assets"))
        idx = r.name if hasattr(r, "name") else None
        if ni is not None and ta is not None and ta != 0:
            return ni / ta
        if nic is not None and ta is not None and ta != 0:
            if idx is not None:
                df.at[idx, "quality_flags"].append("net_income_common_fallback")
            return nic / ta
        return None

    df["roa"] = df.apply(_roa, axis=1)

    # net_margin：native net_income 优先，缺失时可用 net_income_common
    def _net_margin(r):
        ni = _to_decimal(r.get("net_income"))
        nic = _to_decimal(r.get("net_income_common"))
        rev = _to_decimal(r.get("revenues"))
        idx = r.name if hasattr(r, "name") else None
        if ni is not None and rev is not None and rev != 0:
            return ni / rev
        if nic is not None and rev is not None and rev != 0:
            if idx is not None:
                df.at[idx, "quality_flags"].append("net_income_common_fallback")
            return nic / rev
        return None

    df["net_margin"] = df.apply(_net_margin, axis=1)

    # Debt ratio
    df["debt_ratio"] = df.apply(lambda r: _safe_div(r.get("total_liabilities"), r.get("total_assets")), axis=1)

    # Current / Quick ratio
    df["current_ratio"] = df.apply(lambda r: _safe_div(r.get("total_current_assets"), r.get("total_current_liabilities")), axis=1)
    df["quick_ratio"] = df.apply(
        lambda r: _safe_div(
            (_to_decimal(r.get("total_current_assets")) or Decimal(0))
            - (_to_decimal(r.get("inventory_net")) or Decimal(0)),
            r.get("total_current_liabilities")
        ) if pd.notna(r.get("total_current_assets")) else None, axis=1,
    )

    # Book value per share：严格只使用 parent equity，不 fallback
    df["book_value_per_share"] = df.apply(
        lambda r: _safe_div(r.get("total_equity"), r.get("weighted_avg_shares_basic")), axis=1,
    )

    # YoY growth (sorted within each stock)
    df = df.sort_values(["stock_code", "report_date"])
    for col, yoy_col in [("revenues", "revenue_yoy"), ("net_income", "net_profit_yoy")]:
        yoy_values = []
        for stock, group in df.groupby("stock_code"):
            vals = group[col].tolist()
            yoy = [None]  # first year has no prior
            for i in range(1, len(vals)):
                prev = _to_decimal(vals[i - 1])
                curr = _to_decimal(vals[i])
                if prev and prev != 0 and curr is not None:
                    yoy.append(float((curr - prev) / abs(prev)))
                else:
                    yoy.append(None)
            yoy_values.extend(yoy)
        df[yoy_col] = yoy_values

    # 去重并排序 quality_flags
    df["quality_flags"] = df["quality_flags"].apply(lambda flags: sorted(set(flags)))

    # Metadata
    df["selector_basis"] = "latest-restated"
    df["projection_run_id"] = projection_run_id
    df["generated_at"] = datetime.now()

    return df


def _keep_latest_5_annual(df: pd.DataFrame) -> pd.DataFrame:
    """每只股票只保留最近 5 个年度。"""
    if df.empty:
        return df
    return df.sort_values("report_date").groupby("stock_code").tail(5)


# ── TTM 快照构建 ──────────────────────────────────────────────

def build_ttm_component_index(
    all_facts: list,
    allowlist: set[tuple[str, date, date, str]] | None = None,
) -> dict[tuple[str, str], dict]:
    """从 selector 输出构建可复现的 TTM 组件索引。

    返回 dict: {(stock_code, standard_field): {
        'value': Decimal | None,
        'quality_flags': list[str],
        'components': {
            'latest': {...},
            'last_annual': {...},
            'prior_year': {...},
        },
    }}

    本函数与 build_ttm_snapshot 使用同一套过滤、排序和计算逻辑，
    因此组件重算值必须与 us_financial_current_ttm 中的 snapshot 一致。
    """
    quarterly_forms = {"10-Q", "10-Q/A", "10-QT", "10-QT/A"}
    accepted_forms = ANNUAL_FORMS | quarterly_forms
    usable = [
        f for f in all_facts
        if f.form and f.form.upper() in accepted_forms
        and f.unit.upper() == "USD"
        and f.standard_field in TTM_COMPONENT_FIELDS
        and (f.period_kind == "instant"
             or f.form.upper() in quarterly_forms
             or (f.period_start and f.report_date
                 and (f.report_date - f.period_start).days >= 330))
    ]

    if not usable:
        return {}

    records = []
    for f in usable:
        period_days = None
        if f.period_start and f.report_date:
            period_days = (f.report_date - f.period_start).days
        records.append({
            "stock_code": f.stock_code,
            "report_date": f.report_date,
            "filed_date": f.filed_date,
            "accession_no": f.accession_no,
            "standard_field": f.standard_field,
            "value_numeric": _to_decimal(f.value_numeric),
            "form": f.form,
            "is_annual": f.form.upper() in ANNUAL_FORMS,
            "period_start": f.period_start,
            "period_days": period_days,
            "fiscal_period_raw": f.fiscal_period_raw,
        })

    df = pd.DataFrame(records)
    index: dict[tuple[str, str], dict] = {}
    for stock_code, group in df.groupby("stock_code"):
        group = group.sort_values("report_date")
        latest_date = group["report_date"].max()
        is_annual = group[group["report_date"] == latest_date]["is_annual"].any()
        for field in TTM_COMPONENT_FIELDS:
            sub = group[group["standard_field"] == field]
            if is_annual:
                val = _get_field_value(group, latest_date, field)
                flags = []
                if val is None:
                    flags.append(f"missing_component_{field}")
                components = {"latest": None, "last_annual": None, "prior_year": None}
                if not sub.empty:
                    lr = sub[sub["report_date"] == latest_date]
                    if not lr.empty:
                        lr = lr.sort_values("period_days", ascending=False).iloc[0]
                        components["latest"] = {
                            "report_date": lr["report_date"],
                            "accession_no": lr["accession_no"],
                            "filed_date": lr["filed_date"],
                            "value": _to_decimal(lr["value_numeric"]),
                            "period_days": lr["period_days"],
                        }
            else:
                val, flags, components = _compute_ttm_for_field_with_components(
                    sub, field, latest_date, allowlist=allowlist
                )
            index[(stock_code, field)] = {
                "value": val,
                "quality_flags": flags,
                "components": components,
            }
    return index


def build_ttm_snapshot(
    all_facts: list,
    annual_df: pd.DataFrame,
    projection_run_id: str,
    allowlist: set[tuple[str, date, date, str]] | None = None,
) -> pd.DataFrame:
    """从事实和年度快照构建 TTM 快照。"""
    component_index = build_ttm_component_index(all_facts, allowlist=allowlist)
    if not component_index:
        return pd.DataFrame()

    results = []
    stocks = sorted({k[0] for k in component_index})
    for stock_code in stocks:
        ttm_values: dict[str, Decimal | None] = {}
        quality_flags = []
        for field in TTM_FIELDS:
            info = component_index.get((stock_code, field), {})
            ttm_values[field] = info.get("value")
            quality_flags.extend(info.get("quality_flags", []))

        # 净利润 common 口径独立计算，不混入主口径 TTM_FIELDS 的缺件状态
        nic_info = component_index.get((stock_code, "net_income_common"), {})
        ttm_nic_val = nic_info.get("value")

        ttm_cfo = ttm_values.get("net_cash_from_operations")
        ttm_capex = ttm_values.get("capital_expenditures")
        ttm_fcf = None
        if ttm_cfo is not None and ttm_capex is not None:
            ttm_fcf = ttm_cfo - ttm_capex
        else:
            quality_flags.append("missing_component_fcf_ttm")

        ttm_ni_val = ttm_values.get("net_income")
        rev_ttm_val = ttm_values.get("revenues")

        # 仅当 native 缺失时才记录 common 口径的可用/不可用状态；
        # native 存在时不引入任何 common 缺件 flag，避免污染主口径
        if ttm_ni_val is None:
            if ttm_nic_val is not None:
                quality_flags.append("ttm_net_income_native_missing_common_available")
            else:
                quality_flags.extend(nic_info.get("quality_flags", []))

        # 最新年度权益
        equity_info = _latest_annual_equity(annual_df, stock_code)

        # TTM 日期与元数据：取该股票任意字段 latest 组件中 report_date 最晚者
        latest_component: dict | None = None
        latest_date = None
        for field in TTM_COMPONENT_FIELDS:
            info = component_index.get((stock_code, field), {})
            comp = ((info.get("components") or {}).get("latest")) or {}
            if comp.get("report_date") and (latest_date is None or comp["report_date"] > latest_date):
                latest_component = comp
                latest_date = comp["report_date"]

        latest_component = latest_component or {}
        ttm_date = latest_component.get("report_date")
        ttm_filed = latest_component.get("filed_date")
        ttm_accession = latest_component.get("accession_no")

        # 去重并按字母排序，保证产物稳定
        quality_flags = sorted(set(quality_flags))

        results.append({
            "stock_code": stock_code,
            "ttm_report_date": ttm_date,
            "ttm_filed_date": ttm_filed,
            "ttm_accession_no": ttm_accession,
            "revenue_ttm": _to_decimal(rev_ttm_val),
            "net_income_ttm": _to_decimal(ttm_ni_val),
            "net_income_common_ttm": _to_decimal(ttm_nic_val),
            "cfo_ttm": _to_decimal(ttm_cfo),
            "capex_ttm": _to_decimal(ttm_capex),
            "fcf_ttm": _to_decimal(ttm_fcf),
            "equity_report_date": equity_info.get("report_date"),
            "equity_filed_date": equity_info.get("filed_date"),
            "equity_accession_no": equity_info.get("accession_no"),
            "total_equity": equity_info.get("total_equity"),
            "projection_run_id": projection_run_id,
            "quality_flags": quality_flags,
        })

    result_df = pd.DataFrame(results)
    result_df["generated_at"] = datetime.now()
    return result_df


def _get_field_value(group: pd.DataFrame, report_date, field: str) -> Decimal | None:
    """取指定 report_date 和 field 的事实值。

    多行时优先取期间最长的（排除 Q4 standalone），避免累计值混入单季。
    """
    rows = group[(group["report_date"] == report_date) & (group["standard_field"] == field)]
    if rows.empty:
        return None
    if "period_days" in rows.columns:
        rows = rows.sort_values("period_days", ascending=False)
    return _to_decimal(rows.iloc[0]["value_numeric"])


def _compute_ttm_for_field_with_components(
    group: pd.DataFrame,
    field: str,
    latest_date,
    allowlist: set[tuple[str, date, date, str]] | None = None,
) -> tuple[Decimal | None, list[str], dict]:
    """TTM = latest_cumulative + last_annual - prior_year_same_period。

    同时验证累计期间可比性：去年同期必须与本期期间长度一致（±3天）。
    对 52/53 周财年，若精确白名单命中且 4 <= period_diff <= 7，则允许放宽并打
    quality flag `ttm_period_52_53_week_allowlisted`。
    返回 (value, quality_flags, components)。任一组件缺失或期间不匹配时 value 为 None。
    components 包含 latest / last_annual / prior_year 的 value/accession/filed_date/period_days。
    """
    components: dict[str, dict | None] = {"latest": None, "last_annual": None, "prior_year": None}
    stock_code = group["stock_code"].iloc[0] if not group.empty else None

    latest_val = _get_field_value(group, latest_date, field)
    latest_rows = group[(group["report_date"] == latest_date) & (group["standard_field"] == field)]
    latest_days = latest_rows["period_days"].max() if "period_days" in latest_rows.columns else None
    latest_fp = None
    if not latest_rows.empty and "fiscal_period_raw" in latest_rows.columns:
        latest_fp = str(latest_rows.iloc[0]["fiscal_period_raw"] or "").strip() or None

    if latest_val is None:
        return None, [f"missing_component_latest_{field}"], components

    if not latest_rows.empty:
        lr = latest_rows.sort_values("period_days", ascending=False).iloc[0]
        components["latest"] = {
            "report_date": lr["report_date"],
            "accession_no": lr["accession_no"],
            "filed_date": lr["filed_date"],
            "value": _to_decimal(lr["value_numeric"]),
            "period_days": latest_days,
        }

    # Last annual
    annual = group[group["is_annual"] == True]
    la_rows = annual[annual["report_date"] < latest_date]
    if la_rows.empty:
        return None, ["missing_component_last_annual"], components

    la_row = la_rows.iloc[-1]
    la_val = _get_field_value(group, la_row["report_date"], field)
    if la_val is None:
        return None, [f"missing_component_la_{field}"], components

    components["last_annual"] = {
        "report_date": la_row["report_date"],
        "accession_no": la_row["accession_no"],
        "filed_date": la_row["filed_date"],
        "value": la_val,
        "period_days": _to_decimal(la_row["period_days"]) if "period_days" in la_row.index else None,
    }

    # Prior year same period：必须期间长度可比（±3天）
    target = latest_date - timedelta(days=365)
    group_copy = group.copy()
    group_copy["date_diff"] = group_copy["report_date"].apply(
        lambda d: abs((d - target).days) if hasattr(d, "days") or isinstance(d, date) else 9999
    )
    candidates = group_copy[
        (group_copy["standard_field"] == field)
        & (group_copy["date_diff"] <= 7)
    ].sort_values("date_diff")
    # 去年同期必须与本期处于相同累计财季位置（fiscal_period_raw）
    if latest_fp and "fiscal_period_raw" in candidates.columns:
        same_fp = candidates[candidates["fiscal_period_raw"].astype(str).str.upper() == latest_fp.upper()]
        if not same_fp.empty:
            candidates = same_fp
    if candidates.empty:
        return None, ["missing_component_prior_year"], components

    # 选期间长度最匹配的去年同期
    flags: list[str] = []
    if latest_days is not None and "period_days" in candidates.columns:
        candidates = candidates.copy()
        candidates["period_diff"] = candidates["period_days"].apply(
            lambda d: abs(d - latest_days) if d is not None else 9999
        )
        candidates = candidates.sort_values(["period_diff", "date_diff"])
        py_row = candidates.iloc[0]
        period_diff = py_row["period_diff"]
        if period_diff <= 3:
            period_mismatch = False
        elif 4 <= period_diff <= 7:
            prior_fp = None
            if "fiscal_period_raw" in candidates.columns:
                prior_fp = str(py_row.get("fiscal_period_raw") or "").strip() or None
            if (
                stock_code
                and latest_fp
                and prior_fp
                and latest_fp.upper() == prior_fp.upper()
                and is_allowlisted_52_53_pair(
                    allowlist,
                    stock_code,
                    latest_date,
                    py_row["report_date"],
                    latest_days,
                    py_row["period_days"],
                    latest_fp,
                )
            ):
                period_mismatch = False
                flags.append("ttm_period_52_53_week_allowlisted")
            else:
                period_mismatch = True
        else:
            period_mismatch = True
    else:
        py_row = candidates.iloc[0]
        period_mismatch = False

    py_val = _get_field_value(group, py_row["report_date"], field)
    if py_val is None:
        return None, [f"missing_component_py_{field}"], components

    components["prior_year"] = {
        "report_date": py_row["report_date"],
        "accession_no": py_row["accession_no"],
        "filed_date": py_row["filed_date"],
        "value": py_val,
        "period_days": _to_decimal(py_row["period_days"]) if "period_days" in py_row.index else None,
    }

    if period_mismatch:
        return None, ["period_mismatch"], components

    return latest_val + la_val - py_val, flags, components


def _compute_ttm_for_field(
    group: pd.DataFrame,
    field: str,
    latest_date,
    allowlist: set[tuple[str, date, date, str]] | None = None,
) -> tuple:
    """兼容旧接口：仅返回 (value, quality_flags)。"""
    val, flags, _ = _compute_ttm_for_field_with_components(
        group, field, latest_date, allowlist=allowlist
    )
    return val, flags


def _latest_annual_equity(annual_df: pd.DataFrame, stock_code: str) -> dict:
    """从年度快照取最新 annual total_equity。"""
    stock_rows = annual_df[annual_df["stock_code"] == stock_code].sort_values("report_date")
    if stock_rows.empty:
        return {}
    row = stock_rows.iloc[-1]
    return {
        "report_date": row.get("report_date"),
        "filed_date": row.get("filed_date"),
        "accession_no": row.get("accession_no"),
        "total_equity": _to_decimal(row.get("total_equity")),
    }


# ── 数据库写入 ────────────────────────────────────────────────

ANNUAL_COLUMNS = [
    "stock_code", "report_date", "filed_date", "accession_no", "form",
    "revenues", "net_income", "net_income_common",
    "total_assets", "total_liabilities", "total_equity", "total_equity_including_nci",
    "net_cash_from_operations", "capital_expenditures", "fcf",
    "roe", "roa", "gross_margin", "operating_margin", "net_margin",
    "debt_ratio", "current_ratio", "quick_ratio",
    "revenue_yoy", "net_profit_yoy",
    "eps_basic", "eps_diluted", "book_value_per_share",
    "selector_basis", "projection_run_id", "quality_flags", "generated_at",
]

TTM_COLUMNS = [
    "stock_code", "ttm_report_date", "ttm_filed_date", "ttm_accession_no",
    "revenue_ttm", "net_income_ttm", "net_income_common_ttm", "cfo_ttm", "capex_ttm", "fcf_ttm",
    "equity_report_date", "equity_filed_date", "equity_accession_no", "total_equity",
    "projection_run_id", "quality_flags", "generated_at",
]


def _write_snapshot(df: pd.DataFrame, table: str, columns: list[str], conn,
                     stock_codes: list[str]):
    """在同一事务内先删除旧快照再写入新快照（避免过期记录残留）。"""
    if df.empty:
        logger.warning("Empty DataFrame for %s, skipping", table)
        return 0

    # 只保留需要的列
    cols_available = [c for c in columns if c in df.columns]
    write_df = df[cols_available].copy()

    # 转为合适的 Python 类型并确保 NaN → None
    rows: list[tuple] = []
    for _, row in write_df.iterrows():
        clean = []
        for col in cols_available:
            v = row[col]
            if v is None:
                clean.append(None)
            elif isinstance(v, list):
                clean.append(v)
            elif isinstance(v, Decimal):
                clean.append(float(v))
            elif isinstance(v, (date, datetime)):
                clean.append(v)
            else:
                # scalar: check for NaN
                try:
                    if pd.isna(v):
                        clean.append(None)
                    else:
                        clean.append(v)
                except (ValueError, TypeError):
                    clean.append(v)
        rows.append(tuple(clean))

    col_names = ", ".join(cols_available)

    with conn.cursor() as cur:
        # 先删除指定股票池的旧数据，再插入新数据
        cur.execute(f"DELETE FROM {table} WHERE stock_code = ANY(%s)", (stock_codes,))
        if rows:
            sql = f"INSERT INTO {table} ({col_names}) VALUES %s"
            psycopg2.extras.execute_values(cur, sql, rows, page_size=500)

    return len(rows)


# ── CLI ───────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description="Generate US financial snapshots from version layer")
    p.add_argument("--dry-run", action="store_true", help="Build DataFrames but do not write to DB")
    p.add_argument("--stocks", default=None, help="Comma-separated stock codes (overrides full market)")
    p.add_argument(
        "--ttm-allowlist",
        default=None,
        help="Path to 52/53-week TTM allowlist CSV (default: docs/core/US_TTM_52_53_WEEK_ALLOWLIST.csv)",
    )
    return p.parse_args()


def run_projection(
    stocks: list[str] | None = None,
    ttm_allowlist_path: str | None = None,
    out_of_sync_scope: set[str] | None = None,
    dry_run: bool = False,
) -> dict:
    """执行一次全 universe(或指定股票池)projection。

    out_of_sync_scope: 不在本轮同步范围的股票,其 annual/TTM 行追加
    `out_of_sync_scope` quality flag(Phase C1 §3.2,不得伪装 fresh)。

    Returns: {"projection_run_id", "stocks", "annual_rows", "ttm_rows", "dry_run"}
    Raises: 写库/构建失败直接抛出,调用方保留上一版 snapshot。
    """
    projection_run_id = str(uuid.uuid4())
    logger.info("Selector run ID: %s", projection_run_id)

    stocks = stocks or _get_all_us_stocks()
    logger.info("Processing %d stocks", len(stocks))

    # 1. 获取年度事实
    logger.info("Fetching annual facts...")
    fields_to_fetch = list(set(ANNUAL_STANDARD_FIELDS + TTM_FIELDS))
    all_facts = _fetch_facts(stocks, fields_to_fetch)
    logger.info("Total facts: %d", len(all_facts))

    # 2. 构建年度快照
    logger.info("Building annual snapshot...")
    annual_df = build_annual_snapshot(all_facts, projection_run_id)
    annual_df = _keep_latest_5_annual(annual_df)
    logger.info("Annual rows: %d (%d stocks)", len(annual_df),
                annual_df["stock_code"].nunique() if not annual_df.empty else 0)

    # 3. 构建 TTM 快照
    logger.info("Building TTM snapshot...")
    allowlist = load_ttm_52_53_allowlist(ttm_allowlist_path)
    ttm_df = build_ttm_snapshot(all_facts, annual_df, projection_run_id, allowlist=allowlist)
    logger.info("TTM rows: %d", len(ttm_df))

    if out_of_sync_scope:
        for df in (annual_df, ttm_df):
            if df.empty or "quality_flags" not in df.columns:
                continue
            mask = df["stock_code"].isin(out_of_sync_scope)
            df.loc[mask, "quality_flags"] = df.loc[mask, "quality_flags"].map(
                lambda flags: sorted(set(flags or []) | {"out_of_sync_scope"})
            )
        logger.info("out_of_sync_scope 标记: %d 只股票", len(out_of_sync_scope))

    if dry_run:
        logger.info("Dry run — skipping DB write")
        return {
            "projection_run_id": projection_run_id,
            "stocks": len(stocks),
            "annual_rows": len(annual_df),
            "ttm_rows": len(ttm_df),
            "dry_run": True,
        }

    # 4. 写入数据库
    with Connection() as conn:
        n_annual = _write_snapshot(annual_df, "us_financial_current_annual",
                                    ANNUAL_COLUMNS, conn, stocks)
        n_ttm = _write_snapshot(ttm_df, "us_financial_current_ttm",
                                TTM_COLUMNS, conn, stocks)
        conn.commit()
        logger.info("Wrote %d annual rows, %d TTM rows", n_annual, n_ttm)

    logger.info("Projection complete. Run ID: %s", projection_run_id)
    return {
        "projection_run_id": projection_run_id,
        "stocks": len(stocks),
        "annual_rows": n_annual,
        "ttm_rows": n_ttm,
        "dry_run": False,
    }


def main():
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    if os.environ.get("STOCK_MARKETS", "") != "US":
        logger.error("STOCK_MARKETS must be 'US'")
        return 1

    stocks = args.stocks.split(",") if args.stocks else None
    run_projection(
        stocks=stocks,
        ttm_allowlist_path=args.ttm_allowlist,
        dry_run=args.dry_run,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
