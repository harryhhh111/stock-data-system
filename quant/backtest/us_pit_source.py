"""US PIT 数据源(Phase B4)— 回测专用,从版本事实层做 as-of 选股池构建。

与 legacy PITPreloader(旧宽表 + filed_date 过滤)的差异:
- 旧宽表每期间只存最新值,重述后原值不可见;版本层保留全部 filing 版本,
  as-of 能正确看到"当时披露的值"(重述前 vs 重述后);
- TTM 与 ROE 等指标公式与 current snapshot 共享同一套规则(严格三组件、
  ROE 四象限、GP 推导、common 备用口径),仅事实可见性由 as-of 决定;
- 52/53 周白名单只对当前期间配对学生效,历史配对按严格规则处理(NULL 不补)。

开关:US_BACKTEST_PIT_VERSION=1 时 PITPreloader 走本模块;默认关闭走 legacy。
"""

from __future__ import annotations

import logging
import os
import pickle
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from core.selectors.us_financial import USFactSelector
from core.us_financial_exclusion import BUSINESS_REASON_CODES, TECHNICAL_REASON_CODES
from core.us_financial_versioning import ANNUAL_FORMS
from db import Connection

import sys

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_SCRIPTS = _PROJECT_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
import project_us_financial_snapshots as _snap  # noqa: E402

logger = logging.getLogger(__name__)

# 回测需要的字段:年度比率衍生 + TTM 组件 + 季度 yoy
PIT_FIELDS = [
    "revenues", "net_income", "net_income_common",
    "total_assets", "total_liabilities", "total_equity", "total_equity_including_nci",
    "net_cash_from_operations", "capital_expenditures",
    "cost_of_goods_sold", "gross_profit", "operating_income",
    "total_current_assets", "total_current_liabilities", "inventory_net",
    "eps_basic", "eps_diluted",
]

CACHE_DIR = Path("build/pit_cache")

# 缓存内容格式版本:输出契约变化(如 Decimal→float、新增 pe_ttm/pb 占位列)时递增,
# 避免读到旧格式缓存
CACHE_SCHEMA = "v3"

_QUARTERLY_FORMS = {"10-Q", "10-Q/A", "10-QT", "10-QT/A"}


def us_backtest_pit_enabled() -> bool:
    """Phase B4 独立开关:回测 US 数据源走版本事实层 as-of。默认关闭(legacy)。"""
    return os.getenv("US_BACKTEST_PIT_VERSION", "").lower() in {
        "1", "true", "yes", "on",
    }


# ── 事实批量加载 ──────────────────────────────────────────────

def load_fact_rows(
    min_filed_date: str = "2014-01-01",
    min_report_date: str = "2016-01-01",
    chunk_size: int = 200,
) -> list[dict[str, Any]]:
    """一次性加载回测所需字段的全部事实版本(含被后续重述覆盖的旧版本)。

    按 stock_code 分块查询以命中 idx_us_fact_period 索引(全表扫 5.5M 行过慢)。
    min_report_date 默认 2016:覆盖 2021 年起回测的 3-5 年 ROE 回看与 TTM 组件。
    排除规则(us_financial_fact_exclusion)不在此处应用——其 business 排除随
    reference_date 变化,必须在每个 as-of 日期分别应用。
    """
    with Connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT stock_code FROM stock_info WHERE market = 'US' ORDER BY stock_code"
        )
        stocks = [r[0] for r in cur.fetchall()]
        cur.close()

    placeholders = ", ".join(["%s"] * len(PIT_FIELDS))
    sql = f"""
        SELECT f.fact_version_id, f.stock_code, f.statement, f.standard_field,
               f.period_kind, f.period_start, f.report_date, f.unit,
               f.value_hash, f.value_numeric, f.value_text,
               f.accession_no, f.form, f.filed_date, f.dimensions, f.sec_tag,
               f.context_hash, f.fiscal_period_raw
        FROM us_financial_fact_version f
        WHERE f.stock_code = ANY(%s)
          AND f.standard_field IN ({placeholders})
          AND f.filed_date >= %s
          AND f.report_date >= %s
    """
    rows: list[dict[str, Any]] = []
    with Connection() as conn:
        cur = conn.cursor()
        for i in range(0, len(stocks), chunk_size):
            chunk = stocks[i:i + chunk_size]
            cur.execute(sql, (chunk, *PIT_FIELDS, min_filed_date, min_report_date))
            cols = [d[0] for d in cur.description]
            rows.extend(dict(zip(cols, r)) for r in cur.fetchall())
            logger.info(
                "PIT facts chunk %d/%d loaded, cumulative %d rows",
                i // chunk_size + 1, (len(stocks) + chunk_size - 1) // chunk_size, len(rows),
            )
        cur.close()
    logger.info("PIT facts loaded: %d rows", len(rows))
    return rows


def load_exclusions() -> list[dict[str, Any]]:
    """加载 active 事实排除记录(逐 as-of 日期应用)。"""
    with Connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT fact_version_id, reason_code, effective_from
            FROM us_financial_fact_exclusion
            WHERE status = 'active'
            """
        )
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        cur.close()
    return rows


def _apply_exclusions(
    facts: list[dict[str, Any]],
    exclusions: list[dict[str, Any]],
    as_of_date: date,
) -> list[dict[str, Any]]:
    """技术排除始终生效;业务排除仅在 effective_from <= as_of_date 时生效。

    与 selector._load_facts 的排除语义一致(reference_date = as_of_date)。
    """
    excluded_ids: set[int] = set()
    for e in exclusions:
        rc = e.get("reason_code")
        if rc in TECHNICAL_REASON_CODES:
            excluded_ids.add(e["fact_version_id"])
        elif rc in BUSINESS_REASON_CODES:
            eff = e.get("effective_from")
            eff_date = eff.date() if hasattr(eff, "date") and not isinstance(eff, date) else eff
            if eff_date is None or eff_date <= as_of_date:
                excluded_ids.add(e["fact_version_id"])
    if not excluded_ids:
        return facts
    return [f for f in facts if f["fact_version_id"] not in excluded_ids]


def select_as_of(
    facts: list[dict[str, Any]],
    exclusions: list[dict[str, Any]],
    as_of_date: date,
) -> list:
    """在预加载事实上做 as-of 选择(latest-restated 规则,仅 filed <= as_of 可见)。"""
    visible = _apply_exclusions(facts, exclusions, as_of_date)
    selector = USFactSelector()
    selector._load_facts = lambda *args, **kwargs: visible
    return selector.select(basis="as-of", as_of_date=as_of_date, fields=PIT_FIELDS)


# ── 年度衍生:复用 current projection 已验收的纯函数 ──────────

def _to_dec(v):
    return _snap._to_decimal(v)


def _safe_div(a, b):
    return _snap._safe_div(a, b)


def _build_annual_df(
    selected: list, run_id: str, keep_years: int | None = None
) -> pd.DataFrame:
    """as-of 事实 → 年度快照(与 current projection 同一 pivot+派生路径)。

    keep_years 非 None 时,每只股票只保留最近 N 个年度(引擎热路径的性能优化:
    最新年度 + yoy 只需要最近 2 年,ROE 历史只需要最近 N 年;更早年度不影响
    这些值)。数据集构建(manifest/审计)用 keep_years=None 保留全历史。
    """
    if keep_years is not None:
        # 先按 (stock) 找年度期间集合,再过滤事实
        annual_rds: dict[str, set] = {}
        for f in selected:
            if not f.form or f.form.upper() not in ANNUAL_FORMS:
                continue
            if not f.unit or f.unit.upper() != "USD":
                continue
            if not _snap._is_annual_period(f):
                continue
            annual_rds.setdefault(f.stock_code, set()).add(f.report_date)
        keep: dict[str, set] = {
            s: set(sorted(rds, reverse=True)[:keep_years])
            for s, rds in annual_rds.items()
        }
        filtered = [
            f for f in selected
            if not (f.form and f.form.upper() in ANNUAL_FORMS)
            or f.report_date in keep.get(f.stock_code, set())
        ]
        return _snap.build_annual_snapshot(filtered, run_id)
    return _snap.build_annual_snapshot(selected, run_id)


def _yoy(curr, prev):
    if curr is None or prev is None or prev == 0:
        return None
    return (curr - prev) / abs(prev)


# ── universe 构建 ─────────────────────────────────────────────

def build_universe(
    selected: list,
    as_of_date: date,
    info: pd.DataFrame,
    shares: pd.DataFrame,
    run_id: str = "pit",
    annual_df: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """由 as-of 选择结果构建与 legacy _get_universe_us 同列契约的选股池。"""
    if annual_df is None:
        # 引擎热路径:最新年度 + yoy 只需最近 2 个年度
        annual_df = _build_annual_df(selected, run_id, keep_years=2)

    annual_latest: dict[str, dict] = {}
    if not annual_df.empty:
        for stock, group in annual_df.groupby("stock_code"):
            latest = group.sort_values("report_date").iloc[-1]
            d = latest.to_dict()
            # 列名对齐 universe 契约(total_liabilities → total_liab)
            d["total_liab"] = d.get("total_liabilities")
            annual_latest[stock] = d

    # 季度 yoy 补充(legacy 行为:年度缺失时用最新季度 yoy 填充)
    q_yoy = _quarterly_yoy(selected)

    # TTM(严格三组件,与 production projection 同逻辑)
    # 组件只可能落在最近 ~3.3 年:更早的事实对 TTM 无贡献,先裁剪再建索引
    from datetime import timedelta
    ttm_cutoff = as_of_date - timedelta(days=1200)
    ttm_facts = [f for f in selected if f.report_date and f.report_date >= ttm_cutoff]
    component_index = _snap.build_ttm_component_index(ttm_facts)

    rows = []
    for _, info_row in info.iterrows():
        stock = info_row["stock_code"]
        d = dict(annual_latest.get(stock) or {})

        rev_yoy = d.get("revenue_yoy")
        ni_yoy = d.get("net_profit_yoy")
        q = q_yoy.get(stock, {})
        if rev_yoy is None:
            rev_yoy = q.get("revenue_yoy")
        if ni_yoy is None:
            ni_yoy = q.get("net_profit_yoy")

        def _ttm(field):
            info_t = component_index.get((stock, field)) or {}
            return info_t.get("value")

        ni_ttm = _ttm("net_income")
        if ni_ttm is None:
            ni_ttm = _ttm("net_income_common")

        list_date = info_row.get("list_date")
        days_since = None
        if pd.notna(list_date) and list_date is not None:
            days_since = (as_of_date - list_date).days

        rows.append({
            "stock_code": stock,
            "stock_name": info_row.get("stock_name"),
            "market": info_row.get("market"),
            "industry": info_row.get("industry"),
            "list_date": list_date,
            "roe": d.get("roe"),
            "gross_margin": d.get("gross_margin"),
            "operating_margin": d.get("operating_margin"),
            "net_margin": d.get("net_margin"),
            "debt_ratio": d.get("debt_ratio"),
            "current_ratio": d.get("current_ratio"),
            "quick_ratio": d.get("quick_ratio"),
            "total_equity": d.get("total_equity"),
            "total_assets": d.get("total_assets"),
            "total_liab": d.get("total_liab"),
            "eps_basic": d.get("eps_basic"),
            "eps_diluted": d.get("eps_diluted"),
            "revenue_yoy": rev_yoy,
            "net_profit_yoy": ni_yoy,
            "fcf": d.get("fcf"),
            "annual_fcf": d.get("fcf"),
            "parent_equity": d.get("total_equity"),
            "revenue_ttm": _ttm("revenues"),
            "net_profit_ttm": ni_ttm,
            "cfo_ttm": _ttm("net_cash_from_operations"),
            "capex_ttm": _ttm("capital_expenditures"),
            # 显式 NULL 占位:与 quote 合并时把 daily_quote 的供应商 pe_ttm/pb
            # 顶到 _q 后缀列并丢弃——启用分支的 PE/PB 只能由 common.build_universe
            # 用 as-of 分子/分母本地计算,否则保持 NULL(发布门槛 §4.1)。
            "pe_ttm": None,
            "pb": None,
            "report_date": None,
            "days_since_list": days_since,
        })

    result = pd.DataFrame(rows)
    if result.empty:
        return result

    sh = shares[shares["trade_date"] <= as_of_date]
    latest_shares = sh.drop_duplicates(subset="stock_code", keep="first")
    result = result.merge(
        latest_shares[["stock_code", "total_shares"]], on="stock_code", how="left"
    )

    # universe 契约是 float 数值列(legacy 为 float64);Decimal → float,None → NaN
    for col in result.columns:
        if col in ("stock_code", "stock_name", "market", "industry", "list_date",
                   "report_date"):
            continue
        result[col] = result[col].map(
            lambda v: float(v) if v is not None and not (isinstance(v, float) and pd.isna(v)) else np.nan
        ).astype(float)
    return result


def _minus_year(d: date) -> date:
    """前一年同日;2/29 落在平年时取 2/28。"""
    try:
        return d.replace(year=d.year - 1)
    except ValueError:
        return d.replace(year=d.year - 1, day=28)


def _quarterly_yoy(selected: list) -> dict[str, dict]:
    """最新季度累计 revenues/net_income 与去年同期累计比较的 yoy。"""
    by_stock_field: dict[tuple, list] = {}
    for f in selected:
        if not f.form or f.form.upper() not in _QUARTERLY_FORMS:
            continue
        if not f.unit or f.unit.upper() != "USD":
            continue
        if f.period_kind != "duration" or not f.period_start or not f.report_date:
            continue
        if f.standard_field not in ("revenues", "net_income"):
            continue
        by_stock_field.setdefault((f.stock_code, f.standard_field), []).append(f)

    out: dict[str, dict] = {}
    for (stock, field), facts in by_stock_field.items():
        facts.sort(key=lambda f: f.report_date, reverse=True)
        latest = facts[0]
        target_start = _minus_year(latest.period_start)
        target_end = _minus_year(latest.report_date)
        prior = None
        for f in facts[1:]:
            if f.period_start == target_start and f.report_date == target_end:
                prior = f
                break
        curr_v = _to_dec(latest.value_numeric)
        prior_v = _to_dec(prior.value_numeric) if prior else None
        yoy = _yoy(curr_v, prior_v)
        if yoy is not None:
            entry = out.setdefault(stock, {})
            entry["revenue_yoy" if field == "revenues" else "net_profit_yoy"] = yoy
    return out


def build_roe_history(
    selected: list, years: int, run_id: str = "pit",
    annual_df: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """PIT 连续年 ROE:取最近 N 个可见年度(先取行,不排除 NULL——由过滤规则判定)。"""
    if annual_df is None:
        annual_df = _build_annual_df(selected, run_id, keep_years=max(years, 2))
    if annual_df.empty:
        return pd.DataFrame(columns=["stock_code", "report_date", "roe"])
    rows = []
    for stock, group in annual_df.groupby("stock_code"):
        latest_n = group.sort_values("report_date").tail(years)
        for _, r in latest_n.iterrows():
            rows.append({
                "stock_code": stock,
                "report_date": r["report_date"],
                "roe": float(r["roe"]) if r.get("roe") is not None and not pd.isna(r.get("roe")) else np.nan,
            })
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=["stock_code", "report_date", "roe"])
    return df.sort_values(["stock_code", "report_date"], ascending=[True, False])


# ── 磁盘缓存 ──────────────────────────────────────────────────

def _cache_path(kind: str, as_of_date: date, watermark: str, extra: str = "") -> Path:
    name = f"{kind}_{CACHE_SCHEMA}_{as_of_date.isoformat()}_{watermark}{extra}.pkl"
    return CACHE_DIR / name


def load_cached(kind: str, as_of_date: date, watermark: str, extra: str = ""):
    path = _cache_path(kind, as_of_date, watermark, extra)
    if not path.exists():
        return None
    with open(path, "rb") as f:
        return pickle.load(f)


def save_cached(kind: str, as_of_date: date, watermark: str, value, extra: str = "") -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _cache_path(kind, as_of_date, watermark, extra)
    with open(path, "wb") as f:
        pickle.dump(value, f)
