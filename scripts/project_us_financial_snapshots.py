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

TTM_FIELDS = [
    "revenues",
    "net_income",
    "net_cash_from_operations",
    "capital_expenditures",
]


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


# ── 数据采集 ──────────────────────────────────────────────────

def _get_all_us_stocks() -> list[str]:
    with Connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT stock_code FROM stock_info WHERE market = 'US' ORDER BY stock_code")
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
    """计算衍生字段：FCF、ROE、margins、YoY 等。"""
    # FCF
    if "net_cash_from_operations" in df.columns and "capital_expenditures" in df.columns:
        df["fcf"] = df.apply(
            lambda r: (r["net_cash_from_operations"] - r["capital_expenditures"])
            if pd.notna(r.get("net_cash_from_operations")) and pd.notna(r.get("capital_expenditures"))
            else None, axis=1,
        )
    else:
        df["fcf"] = None

    # ROE = net_income / total_equity
    df["roe"] = df.apply(lambda r: _safe_div(r.get("net_income"), r.get("total_equity")), axis=1)

    # ROA = net_income / total_assets
    df["roa"] = df.apply(lambda r: _safe_div(r.get("net_income"), r.get("total_assets")), axis=1)

    # Margins
    df["gross_margin"] = df.apply(lambda r: _safe_div(r.get("gross_profit"), r.get("revenues")), axis=1)
    df["operating_margin"] = df.apply(lambda r: _safe_div(r.get("operating_income"), r.get("revenues")), axis=1)
    df["net_margin"] = df.apply(lambda r: _safe_div(r.get("net_income"), r.get("revenues")), axis=1)

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

    # Book value per share
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

def build_ttm_snapshot(all_facts: list, annual_df: pd.DataFrame, projection_run_id: str) -> pd.DataFrame:
    """从事实和年度快照构建 TTM 快照。"""
    # 收集所有 annual + quarterly 事实（用于 TTM 公式）
    accepted_forms = ANNUAL_FORMS | {"10-Q", "10-Q/A", "10-QT", "10-QT/A"}
    usable = [
        f for f in all_facts
        if f.form and f.form.upper() in accepted_forms
        and f.unit.upper() == "USD"
        and f.standard_field in TTM_FIELDS
    ]

    if not usable:
        return pd.DataFrame()

    # 构建每只股票的时序 DataFrame
    records = []
    for f in usable:
        records.append({
            "stock_code": f.stock_code,
            "report_date": f.report_date,
            "filed_date": f.filed_date,
            "accession_no": f.accession_no,
            "standard_field": f.standard_field,
            "value_numeric": _to_decimal(f.value_numeric),
            "form": f.form,
            "is_annual": f.form.upper() in ANNUAL_FORMS,
        })

    df = pd.DataFrame(records)

    results = []
    for stock_code, group in df.groupby("stock_code"):
        group = group.sort_values("report_date")
        latest_row = group.iloc[-1]
        latest_date = latest_row["report_date"]
        is_annual = latest_row["is_annual"]

        ttm_values: dict[str, Decimal | None] = {}
        for field in TTM_FIELDS:
            if is_annual:
                ttm_values[field] = _get_field_value(group, latest_date, field)
            else:
                ttm_values[field] = _compute_ttm_for_field(group, field, latest_date)

        ttm_cfo = ttm_values.get("net_cash_from_operations")
        ttm_capex = ttm_values.get("capital_expenditures")
        ttm_fcf = None
        if ttm_cfo is not None and ttm_capex is not None:
            ttm_fcf = ttm_cfo - ttm_capex

        ttm_ni_val = ttm_values.get("net_income")
        rev_ttm_val = ttm_values.get("revenues")

        # 最新年度权益
        equity_info = _latest_annual_equity(annual_df, stock_code)

        quality_flags = []
        for field in TTM_FIELDS:
            if ttm_values.get(field) is None:
                quality_flags.append(f"missing_component_{field}")
        ttm_date = latest_row["ttm_report_date"] if "ttm_report_date" in latest_row.index else latest_date

        results.append({
            "stock_code": stock_code,
            "ttm_report_date": ttm_date,
            "ttm_filed_date": latest_row.get("filed_date"),
            "ttm_accession_no": latest_row.get("accession_no"),
            "revenue_ttm": _to_decimal(rev_ttm_val),
            "net_income_ttm": _to_decimal(ttm_ni_val),
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
    rows = group[(group["report_date"] == report_date) & (group["standard_field"] == field)]
    if rows.empty:
        return None
    return _to_decimal(rows.iloc[0]["value_numeric"])


def _compute_ttm_for_field(group: pd.DataFrame, field: str, latest_date) -> Decimal | None:
    """TTM = latest_cumulative + last_annual - prior_year_same_period。

    任一组件缺失时返回 None，不伪造完整 TTM。
    """
    latest_val = _get_field_value(group, latest_date, field)
    if latest_val is None:
        return None

    # Last annual
    annual = group[group["is_annual"] == True]
    la_rows = annual[annual["report_date"] < latest_date]
    if la_rows.empty:
        return None  # 无上年度，无法构造 TTM

    la_row = la_rows.iloc[-1]
    la_val = _get_field_value(group, la_row["report_date"], field)
    if la_val is None:
        return None  # 上年度缺该字段

    # Prior year same period (±7 days)
    target = latest_date - timedelta(days=365)
    group_copy = group.copy()
    group_copy["date_diff"] = group_copy["report_date"].apply(
        lambda d: abs((d - target).days) if hasattr(d, "days") or isinstance(d, date) else 9999
    )
    prior = group_copy[group_copy["date_diff"] <= 7].sort_values("date_diff")
    if prior.empty:
        return None  # 无去年同期，无法构造 TTM

    py_row = prior.iloc[0]
    py_val = _get_field_value(group, py_row["report_date"], field)
    if py_val is None:
        return None  # 去年同期缺该字段

    return latest_val + la_val - py_val


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
    "revenues", "net_income",
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
    "revenue_ttm", "net_income_ttm", "cfo_ttm", "capex_ttm", "fcf_ttm",
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

    # 将 Decimal / Timestamp 转为合适的 Python 类型
    for col in write_df.columns:
        write_df[col] = write_df[col].apply(lambda v: float(v) if isinstance(v, Decimal) else v)

    # quality_flags 需要转为 JSON 数组
    if "quality_flags" in write_df.columns:
        write_df["quality_flags"] = write_df["quality_flags"].apply(
            lambda v: v if isinstance(v, list) else []
        )

    rows = write_df.where(write_df.notna(), None).values.tolist()
    rows = [tuple(r) for r in rows]

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
    return p.parse_args()


def main():
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    if os.environ.get("STOCK_MARKETS", "") != "US":
        logger.error("STOCK_MARKETS must be 'US'")
        return 1

    projection_run_id = str(uuid.uuid4())
    logger.info("Selector run ID: %s", projection_run_id)

    stocks = args.stocks.split(",") if args.stocks else _get_all_us_stocks()
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
    ttm_df = build_ttm_snapshot(all_facts, annual_df, projection_run_id)
    logger.info("TTM rows: %d", len(ttm_df))

    if args.dry_run:
        logger.info("Dry run — skipping DB write")
        if not annual_df.empty:
            print(annual_df[["stock_code", "report_date", "revenues", "net_income", "roe"]].head(10).to_string())
        if not ttm_df.empty:
            print(ttm_df[["stock_code", "revenue_ttm", "net_income_ttm", "fcf_ttm"]].head(10).to_string())
        return 0

    # 4. 写入数据库
    with Connection() as conn:
        n_annual = _write_snapshot(annual_df, "us_financial_current_annual",
                                    ANNUAL_COLUMNS, conn, stocks)
        n_ttm = _write_snapshot(ttm_df, "us_financial_current_ttm",
                                TTM_COLUMNS, conn, stocks)
        conn.commit()
        logger.info("Wrote %d annual rows, %d TTM rows", n_annual, n_ttm)

    logger.info("Projection complete. Run ID: %s", projection_run_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
