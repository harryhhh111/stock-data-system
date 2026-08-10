"""个股分析器 — 美股数据查询层。

两条读取路径由 feature flag 切换：

- flag 关闭（默认）：旧宽表/物化视图路径（``_legacy_*``），Phase B 期间作为整体回退；
- ``US_FINANCIAL_VERSION_CANARY=1``（仅 canary 股票）或
  ``US_FINANCIAL_VERSION_CURRENT=1``（全部美股）：current snapshot 路径，
  只读 ``us_financial_current_annual`` / ``us_financial_current_ttm`` /
  ``daily_quote``（仅 close、market_cap、trade_date）。

Phase B2 复用本模块的 snapshot 装配：``load_us_snapshot_universe()`` 为筛选器、
FCF+ROE 策略与 US 行业中位数提供同一集合查询，由独立开关
``US_SCREENER_SNAPSHOT_CURRENT`` 控制（不复用 B1 开关）。

snapshot 路径的硬约束（Phase B1 规格 §2）：

- 不读取 mv_us_fcf_yield / mv_us_indicator_ttm / mv_us_financial_indicator /
  us_income_statement / us_balance_sheet / us_cash_flow_statement，
  也不透传 daily_quote.pe_ttm / pb（供应商估值）；
- PE/PB/FCF Yield 由 snapshot 财务与同一行情市值本地自算；
- 无 snapshot 时返回显式 financial_data_status，财务字段为 NULL，行情照显，
  不得回退旧宽表；新路径自身出错时直接抛出并记录日志，不得 catch 后回旧路径。
"""

import csv
import logging
import os
from pathlib import Path

import pandas as pd

from db import Connection
from quant.metrics import compute_pb, compute_pe

logger = logging.getLogger(__name__)

CANARY_STOCKS = {
    "PLTR", "MELI", "ONTO", "SAM", "HRB",
    "VZ", "TDC", "ACGL", "GAP", "CRM",
}

# financial_data_status 取值（Phase B1 规格 §2.3）
STATUS_SNAPSHOT_AVAILABLE = "snapshot_available"
STATUS_SELECTOR_EXCEPTION = "selector_exception"
STATUS_OUT_OF_SYNC_SCOPE = "out_of_sync_scope"
STATUS_SNAPSHOT_UNAVAILABLE = "snapshot_unavailable"

# Phase A 登记的 selector exception 清单（stock_code, report_date, field）
_EXCEPTIONS_CSV = (
    Path(__file__).resolve().parents[2] / "docs" / "core" / "US_PHASE_A_EXCEPTIONS.csv"
)

# 新路径 SQL：只读 snapshot 与行情，不读旧财务对象、不读供应商 PE/PB。
_SQL_SNAPSHOT_QUOTE = """
    SELECT s.stock_code, s.stock_name, s.market, s.industry, s.list_date,
           q.trade_date, q.close, q.market_cap
    FROM stock_info s
    LEFT JOIN LATERAL (
        SELECT trade_date, close, market_cap
        FROM daily_quote
        WHERE stock_code = s.stock_code AND market = 'US'
        ORDER BY trade_date DESC LIMIT 1
    ) q ON true
    WHERE s.stock_code = %s AND s.market = %s
"""

_SQL_SNAPSHOT_TTM = """
    SELECT ttm_report_date, ttm_filed_date, ttm_accession_no,
           revenue_ttm, net_income_ttm, net_income_common_ttm,
           cfo_ttm, capex_ttm, fcf_ttm,
           equity_report_date, equity_filed_date, equity_accession_no,
           total_equity, quality_flags, generated_at
    FROM us_financial_current_ttm
    WHERE stock_code = %s
"""

_SQL_SNAPSHOT_ANNUAL = """
    SELECT report_date, filed_date, accession_no, form,
           revenues AS operating_revenue,
           net_income AS parent_net_profit,
           net_income AS net_profit,
           gross_margin, operating_margin, net_margin,
           roe, roa, eps_basic,
           debt_ratio, current_ratio, quick_ratio,
           total_assets, total_liabilities AS total_liab, total_equity,
           fcf, net_cash_from_operations AS cfo_net,
           capital_expenditures AS capex,
           revenue_yoy, net_profit_yoy
    FROM us_financial_current_annual
    WHERE stock_code = %s
    ORDER BY report_date DESC
    LIMIT %s
"""

_SQL_SNAPSHOT_TTM_FRAME = """
    SELECT ttm_report_date AS report_date, 'ttm' AS report_type,
           revenue_ttm,
           COALESCE(net_income_ttm, net_income_common_ttm) AS net_profit_ttm,
           cfo_ttm, capex_ttm
    FROM us_financial_current_ttm
    WHERE stock_code = %s
"""

# 旧路径禁止出现在新路径 SQL 中的对象（规格 §2.1）；测试据此做静态校验。
_LEGACY_FORBIDDEN_OBJECTS = (
    "mv_us_fcf_yield",
    "mv_us_indicator_ttm",
    "mv_us_financial_indicator",
    "us_income_statement",
    "us_balance_sheet",
    "us_cash_flow_statement",
)

# Phase B2 全市场 universe 集合查询：stock + 最新行情 + 最新 annual snapshot + TTM snapshot。
# 禁止逐股循环查权益；daily_quote 只取 close / market_cap / trade_date / currency，
# 不读供应商 pe_ttm / pb。{extra_where} 仅供行业中位数按 industry / exclude_code 过滤。
_SQL_US_SNAPSHOT_UNIVERSE = """
    SELECT
        s.stock_code, s.stock_name, s.market, s.industry, s.list_date,
        (CURRENT_DATE - s.list_date) AS days_since_list,

        q.trade_date AS quote_date, q.close, q.market_cap,
        q.currency AS quote_currency,

        a.report_date AS annual_report_date,
        a.filed_date AS annual_filed_date,
        a.accession_no AS annual_accession_no,
        a.roe, a.gross_margin, a.operating_margin, a.net_margin,
        a.debt_ratio, a.current_ratio, a.quick_ratio,
        a.revenue_yoy, a.net_profit_yoy, a.eps_basic, a.revenues,
        a.total_assets, a.total_liabilities,
        a.total_equity AS annual_total_equity,
        a.fcf AS annual_fcf,
        a.quality_flags AS annual_quality_flags,

        t.ttm_report_date, t.ttm_filed_date, t.ttm_accession_no,
        t.revenue_ttm, t.net_income_ttm, t.net_income_common_ttm,
        t.cfo_ttm, t.capex_ttm, t.fcf_ttm,
        t.equity_report_date, t.equity_filed_date, t.total_equity,
        t.quality_flags

    FROM stock_info s

    LEFT JOIN LATERAL (
        SELECT trade_date, close, market_cap, currency
        FROM daily_quote
        WHERE stock_code = s.stock_code AND market = 'US'
        ORDER BY trade_date DESC LIMIT 1
    ) q ON true

    LEFT JOIN LATERAL (
        SELECT report_date, filed_date, accession_no,
               roe, gross_margin, operating_margin, net_margin,
               debt_ratio, current_ratio, quick_ratio,
               revenue_yoy, net_profit_yoy, eps_basic, revenues,
               total_assets, total_liabilities, total_equity, fcf, quality_flags
        FROM us_financial_current_annual a2
        WHERE a2.stock_code = s.stock_code
        ORDER BY a2.report_date DESC, a2.filed_date DESC, a2.accession_no DESC
        LIMIT 1
    ) a ON true

    LEFT JOIN us_financial_current_ttm t ON t.stock_code = s.stock_code

    WHERE s.market = 'US'
    {extra_where}
    ORDER BY s.stock_code
"""


def screener_snapshot_enabled() -> bool:
    """Phase B2 独立开关：筛选器 / FCF+ROE 策略 / US 行业中位数走 current snapshot。

    默认关闭（legacy）。不得复用 B1 的 US_FINANCIAL_VERSION_CURRENT，
    避免部署时意外切换筛选器。
    """
    return os.getenv("US_SCREENER_SNAPSHOT_CURRENT", "").lower() in {
        "1", "true", "yes", "on",
    }


def _canary_enabled(stock_code: str) -> bool:
    """兼容旧 canary 开关，并支持通过单一开关扩大到全部美股。"""
    current_enabled = os.getenv("US_FINANCIAL_VERSION_CURRENT", "").lower() in {
        "1", "true", "yes", "on",
    }
    if current_enabled:
        return True
    enabled = os.getenv("US_FINANCIAL_VERSION_CANARY", "").lower() in {
        "1", "true", "yes", "on",
    }
    configured = os.getenv("US_FINANCIAL_VERSION_CANARY_STOCKS")
    stocks = (
        {code.strip().upper() for code in configured.split(",") if code.strip()}
        if configured else CANARY_STOCKS
    )
    return enabled and stock_code.upper() in stocks


def _pandas_scalar(value):
    """将 Decimal 等数据库精确数值转换为 pandas 数值列可安全写入的标量。"""
    if value is None or pd.isna(value):
        return None
    return float(value)


def _as_date(value):
    """数据库日期/时间戳统一转为 date；缺失返回 None。"""
    if value is None or pd.isna(value):
        return None
    return pd.Timestamp(value).date()


def _flags_list(flags) -> list[str]:
    if flags is None:
        return []
    if isinstance(flags, (list, tuple)):
        return [str(f) for f in flags]
    return [str(flags)]


def _load_registered_exceptions() -> frozenset:
    """加载 Phase A selector exception 清单，返回 (stock, report_date, field) 键集。

    清单缺失只降级 selector_exception 状态的辨识能力，必须显式告警，不静默。
    """
    path = os.getenv("US_PHASE_A_EXCEPTIONS_PATH") or str(_EXCEPTIONS_CSV)
    keys: set[tuple[str, str, str]] = set()
    try:
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                stock = (row.get("stock_code") or "").strip().upper()
                report_date = (row.get("report_date") or "").strip()
                field = (row.get("field") or "").strip()
                if stock and report_date and field:
                    keys.add((stock, report_date, field))
    except FileNotFoundError:
        logger.warning("Phase A exception list not found: %s", path)
    return frozenset(keys)


def compute_effective_net_income(net_income_ttm, net_income_common_ttm):
    """effective NI = COALESCE(net_income_ttm, net_income_common_ttm)。

    返回 (value, basis)，basis ∈ consolidated | common | unavailable。
    B1 个股页与 B2 筛选器/行业中位数共用同一口径，禁止复制第二套分母逻辑。
    """
    ni = _pandas_scalar(net_income_ttm)
    nic = _pandas_scalar(net_income_common_ttm)
    if ni is not None:
        return ni, "consolidated"
    if nic is not None:
        return nic, "common"
    return None, "unavailable"


def _financial_data_status(stock_code: str, ttm_row, exceptions: frozenset | None = None) -> str:
    """根据 TTM snapshot 行判定 financial_data_status（规格 §2.3）。

    exceptions 可由调用方批量预加载（如全市场 universe），避免每行重读 CSV。
    """
    if ttm_row is None:
        return STATUS_SNAPSHOT_UNAVAILABLE
    flags = [f.lower() for f in _flags_list(ttm_row.get("quality_flags"))]
    if any("out_of_sync_scope" in f for f in flags):
        return STATUS_OUT_OF_SYNC_SCOPE
    if exceptions is None:
        exceptions = _load_registered_exceptions()
    report_date = str(ttm_row.get("ttm_report_date") or "")[:10]
    for field in ("revenue_ttm", "net_income_ttm", "fcf_ttm", "cfo_ttm", "capex_ttm"):
        if ttm_row.get(field) is None or pd.isna(ttm_row.get(field)):
            if (stock_code.upper(), report_date, field) in exceptions:
                return STATUS_SELECTOR_EXCEPTION
    return STATUS_SNAPSHOT_AVAILABLE


# ── legacy 路径（feature flag 关闭时的整体回退，Phase B 期间保留） ──


def _legacy_stock_info(stock_code: str, market: str) -> pd.DataFrame:
    """获取股票基本信息和最新行情数据。

    优先使用 mv_us_fcf_yield（含 FCF Yield），若不存在则 fallback 到 daily_quote。
    """
    sql_fy = """
        SELECT s.stock_code, s.stock_name, s.market, s.industry, s.list_date,
               fy.trade_date, fy.close, fy.market_cap, fy.pe_ttm, fy.pb, fy.fcf_yield,
               fy.fcf_ttm, fy.revenue_ttm, fy.net_profit_ttm, fy.cfo_ttm,
               fy.ttm_report_date,
               t.filed_date AS ttm_notice_date
        FROM stock_info s
        LEFT JOIN mv_us_fcf_yield fy ON s.stock_code = fy.stock_code
        LEFT JOIN LATERAL (
            SELECT filed_date FROM mv_us_indicator_ttm
            WHERE stock_code = s.stock_code
            ORDER BY report_date DESC LIMIT 1
        ) t ON true
        WHERE s.stock_code = %s AND s.market = %s
    """
    sql_fallback = """
        SELECT s.stock_code, s.stock_name, s.market, s.industry, s.list_date,
               q.close, q.market_cap, q.pe_ttm, q.pb,
               NULL AS fcf_yield, NULL AS fcf_ttm,
               NULL AS revenue_ttm, NULL AS net_profit_ttm, NULL AS cfo_ttm,
               NULL AS ttm_report_date
        FROM stock_info s
        LEFT JOIN LATERAL (
            SELECT trade_date, close, market_cap, pe_ttm, pb
            FROM daily_quote
            WHERE stock_code = s.stock_code AND market = 'US'
              AND market_cap IS NOT NULL AND market_cap > 0
            ORDER BY trade_date DESC LIMIT 1
        ) q ON true
        WHERE s.stock_code = %s AND s.market = %s
    """
    with Connection() as conn:
        df = pd.read_sql(sql_fy, conn, params=(stock_code, market))
        if df.empty or pd.isna(df.iloc[0].get("close")):
            df = pd.read_sql(sql_fallback, conn, params=(stock_code, market))
        return df


def _legacy_financial_history(stock_code: str, years: int = 5) -> pd.DataFrame:
    """获取个股历史年度财务数据。

    列名通过 SQL 别名与 CN 视图保持一致，analysis.py 无需修改。
    """
    sql = """
        SELECT fi.report_date,
               i.revenues AS operating_revenue,
               i.net_income AS parent_net_profit,
               i.net_income AS net_profit,
               fi.gross_margin, fi.operating_margin, fi.net_margin,
               fi.roe, fi.roa, fi.eps_basic,
               fi.debt_ratio, fi.current_ratio, fi.quick_ratio,
               fi.total_assets, fi.total_liab, fi.total_equity,
               fi.fcf, fi.cfo AS cfo_net, fi.capex,
               fi.revenue_yoy, fi.net_profit_yoy
        FROM mv_us_financial_indicator fi
        JOIN us_income_statement i
            ON fi.stock_code = i.stock_code
            AND fi.report_date = i.report_date
            AND fi.report_type = i.report_type
        WHERE fi.stock_code = %s AND fi.report_type = 'annual'
        ORDER BY fi.report_date DESC
        LIMIT %s
    """
    with Connection() as conn:
        return pd.read_sql(sql, conn, params=(stock_code, years))


def _legacy_ttm_data(stock_code: str) -> pd.DataFrame:
    """获取 TTM 滚动指标。

    net_income_ttm 别名为 net_profit_ttm 以保持下游兼容。
    filed_date 别名为 ttm_notice_date 用于判断数据时效。
    """
    sql = """
        SELECT report_date, report_type, filed_date AS ttm_notice_date,
               revenue_ttm, net_income_ttm AS net_profit_ttm, cfo_ttm, capex_ttm
        FROM mv_us_indicator_ttm
        WHERE stock_code = %s
    """
    with Connection() as conn:
        return pd.read_sql(sql, conn, params=(stock_code,))


# ── snapshot 路径（Phase B1 新路径） ──


def _snapshot_stock_info(stock_code: str, market: str) -> pd.DataFrame:
    """从 current snapshot + 最新行情装配个股概况，估值全部本地自算。

    查询错误直接向上抛（路由层显式报错），不得回退旧路径。
    """
    with Connection() as conn:
        quote = pd.read_sql(_SQL_SNAPSHOT_QUOTE, conn, params=(stock_code, market))
        if quote.empty:
            return quote
        ttm = pd.read_sql(_SQL_SNAPSHOT_TTM, conn, params=(stock_code.upper(),))

    q = quote.iloc[0]
    market_cap = _pandas_scalar(q.get("market_cap"))
    trade_date = _as_date(q.get("trade_date"))

    out = {
        "stock_code": q.get("stock_code"),
        "stock_name": q.get("stock_name"),
        "market": q.get("market"),
        "industry": q.get("industry"),
        "list_date": q.get("list_date"),
        "trade_date": trade_date,
        "close": _pandas_scalar(q.get("close")),
        "market_cap": market_cap,
        "pe_ttm": None,
        "pb": None,
        "fcf_yield": None,
        "fcf_ttm": None,
        "revenue_ttm": None,
        "net_profit_ttm": None,
        "cfo_ttm": None,
        "ttm_report_date": None,
        "ttm_filed_date": None,
        "ttm_accession_no": None,
        "pb_equity_date": None,
        "net_income_basis": "unavailable",
        "financial_data_status": STATUS_SNAPSHOT_UNAVAILABLE,
        "quality_flags": [],
    }

    if ttm.empty:
        logger.warning(
            "no current snapshot for %s; returning quote only with status %s",
            stock_code, STATUS_SNAPSHOT_UNAVAILABLE,
        )
        return pd.DataFrame([out])

    t = ttm.iloc[0]
    effective_net_income, basis = compute_effective_net_income(
        t.get("net_income_ttm"), t.get("net_income_common_ttm"),
    )

    fcf = _pandas_scalar(t.get("fcf_ttm"))
    equity = _pandas_scalar(t.get("total_equity"))
    equity_filed_date = _as_date(t.get("equity_filed_date"))
    equity_report_date = _as_date(t.get("equity_report_date"))

    out.update({
        "revenue_ttm": _pandas_scalar(t.get("revenue_ttm")),
        "net_profit_ttm": effective_net_income,
        "cfo_ttm": _pandas_scalar(t.get("cfo_ttm")),
        "fcf_ttm": fcf,
        "ttm_report_date": _as_date(t.get("ttm_report_date")),
        "ttm_filed_date": _as_date(t.get("ttm_filed_date")),
        "ttm_accession_no": t.get("ttm_accession_no"),
        "net_income_basis": basis,
        "financial_data_status": _financial_data_status(stock_code, t),
        "quality_flags": _flags_list(t.get("quality_flags")),
        # PE：仅正利润可算；亏损/无利润为 NULL，由前端按 basis 显示 N/M。
        "pe_ttm": compute_pe(market_cap, effective_net_income),
        # FCF Yield：负值保留；fcf_ttm 为 NULL（含已登记 exception）时为 NULL。
        "fcf_yield": (
            fcf / market_cap
            if fcf is not None and market_cap is not None and market_cap > 0
            else None
        ),
    })

    # PB：parent equity 必须在行情 trade_date 当日已披露（equity_filed_date ≤ trade_date）。
    if (
        equity_filed_date is not None
        and trade_date is not None
        and equity_filed_date <= trade_date
    ):
        out["pb"] = compute_pb(market_cap, equity)
        out["pb_equity_date"] = equity_report_date
    elif equity is not None:
        logger.warning(
            "skip PB for %s: equity filed %s not available at trade date %s",
            stock_code, equity_filed_date, trade_date,
        )

    return pd.DataFrame([out])


# ── Phase B2：全市场 snapshot universe（筛选器 / 策略 / 行业中位数共用） ──


def load_us_snapshot_universe(
    industry: str | None = None,
    exclude_code: str = "",
) -> pd.DataFrame:
    """从 current snapshot + 最新行情装配全市场（或单行业）universe，估值全部本地自算。

    - 单次集合查询装配 stock / 最新 daily_quote / 最新 annual snapshot / TTM snapshot，
      禁止逐股循环查询权益；
    - PE/PB/FCF Yield 口径与 B1 个股页完全一致（compute_pe/compute_pb + 同一行情市值）；
    - PB 只用 TTM snapshot 的 parent equity，且 equity_filed_date ≤ quote_date；
    - 无 snapshot 的股票保留行情行，财务/估值字段为 NULL，status 显式标注；
    - DB/数据错误直接向上抛，不得 catch 回退旧财务数据。
    """
    extra_where = ""
    params: list = []
    if industry is not None:
        extra_where += " AND s.industry = %s"
        params.append(industry)
    if exclude_code:
        extra_where += " AND s.stock_code != %s"
        params.append(exclude_code)

    sql = _SQL_US_SNAPSHOT_UNIVERSE.format(extra_where=extra_where)
    with Connection() as conn:
        df = pd.read_sql(sql, conn, params=params or None)
    if df.empty:
        return df

    # Phase A exception 清单整批加载一次；缺失时 _load_registered_exceptions 已 warning。
    exceptions = _load_registered_exceptions()

    eff = [
        compute_effective_net_income(ni, nic)
        for ni, nic in zip(df["net_income_ttm"], df["net_income_common_ttm"])
    ]
    df["net_profit_ttm"] = [v for v, _ in eff]
    df["net_income_basis"] = [b for _, b in eff]

    market_caps = [_pandas_scalar(v) for v in df["market_cap"]]
    df["pe_ttm"] = [
        compute_pe(mc, ni) for mc, ni in zip(market_caps, df["net_profit_ttm"])
    ]
    # FCF Yield：负值保留；fcf_ttm 为 NULL（含已登记 exception）时为 NULL。
    df["fcf_yield"] = [
        (fcf / mc if fcf is not None and mc is not None and mc > 0 else None)
        for fcf, mc in zip(
            (_pandas_scalar(v) for v in df["fcf_ttm"]), market_caps,
        )
    ]

    # PB：parent equity 必须在行情 trade_date 当日已披露（equity_filed_date ≤ quote_date）。
    pb_values: list = []
    pb_equity_dates: list = []
    for _, row in df.iterrows():
        mc = _pandas_scalar(row.get("market_cap"))
        equity = _pandas_scalar(row.get("total_equity"))
        equity_filed = _as_date(row.get("equity_filed_date"))
        quote_date = _as_date(row.get("quote_date"))
        if equity_filed is not None and quote_date is not None and equity_filed <= quote_date:
            pb_values.append(compute_pb(mc, equity))
            pb_equity_dates.append(_as_date(row.get("equity_report_date")))
        else:
            if equity is not None:
                logger.warning(
                    "skip PB for %s: equity filed %s not available at trade date %s",
                    row.get("stock_code"), equity_filed, quote_date,
                )
            pb_values.append(None)
            pb_equity_dates.append(None)
    df["pb"] = pb_values
    df["pb_equity_date"] = pb_equity_dates

    df["financial_data_status"] = [
        (
            _financial_data_status(code, row, exceptions)
            if not pd.isna(row.get("ttm_report_date"))
            else STATUS_SNAPSHOT_UNAVAILABLE
        )
        for code, (_, row) in zip(df["stock_code"], df.iterrows())
    ]
    return df


def industry_stats_from_universe(df: pd.DataFrame) -> dict:
    """对已装配的 snapshot universe（单行业子集）计算中位数统计。

    年度中位数：ROE / gross margin / net margin / debt ratio；
    估值中位数：本地计算的 PE / PB / FCF Yield，PE/PB 仅正值入中位数。
    空行业或样本不足（无有效值）返回明确 NULL。
    """
    def _median(column: str, positive_only: bool = False):
        if df.empty or column not in df.columns:
            return None
        s = pd.to_numeric(df[column], errors="coerce").dropna()
        if positive_only:
            s = s[s > 0]
        if s.empty:
            return None
        return float(s.median())

    return {
        "peer_count": len(df),
        "median_roe": _median("roe"),
        "median_gross_margin": _median("gross_margin"),
        "median_net_margin": _median("net_margin"),
        "median_debt_ratio": _median("debt_ratio"),
        "median_pe": _median("pe_ttm", positive_only=True),
        "median_pb": _median("pb", positive_only=True),
        "median_fcf_yield": _median("fcf_yield"),
    }


def _snapshot_industry_stats(industry: str, exclude_code: str = "") -> pd.DataFrame:
    """US 同行业中位数（Phase B2 新分支）：与筛选器同一 snapshot universe。

    排除当前股票；中位数语义见 industry_stats_from_universe()。
    """
    df = load_us_snapshot_universe(industry=industry, exclude_code=exclude_code)
    return pd.DataFrame([industry_stats_from_universe(df)])


# ── 对外接口：按 feature flag 分发 ──


def get_financial_history(stock_code: str, years: int = 5) -> pd.DataFrame:
    if not _canary_enabled(stock_code):
        return _legacy_financial_history(stock_code, years)
    with Connection() as conn:
        return pd.read_sql(
            _SQL_SNAPSHOT_ANNUAL, conn, params=(stock_code.upper(), years),
        )


def get_ttm_data(stock_code: str) -> pd.DataFrame:
    if not _canary_enabled(stock_code):
        return _legacy_ttm_data(stock_code)
    with Connection() as conn:
        return pd.read_sql(_SQL_SNAPSHOT_TTM_FRAME, conn, params=(stock_code.upper(),))


def get_stock_info(stock_code: str, market: str) -> pd.DataFrame:
    if not _canary_enabled(stock_code):
        return _legacy_stock_info(stock_code, market)
    return _snapshot_stock_info(stock_code, market)


def get_industry_stats(industry: str, market: str, exclude_code: str = "") -> pd.DataFrame:
    """获取同行业股票的估值和财务指标中位数。

    US 市场在 ``US_SCREENER_SNAPSHOT_CURRENT`` 开启时走 current snapshot
    （Phase B2）；默认 legacy 宽表/物化视图路径。CN 市场由 query_cn 处理。
    """
    if market == "US" and screener_snapshot_enabled():
        return _snapshot_industry_stats(industry, exclude_code)
    sql = """
        WITH peers AS (
            SELECT stock_code FROM stock_info
            WHERE industry = %s AND market = %s AND stock_code != %s
        ),
        peer_fin AS (
            SELECT DISTINCT ON (fi.stock_code)
                fi.stock_code, fi.roe, fi.gross_margin, fi.net_margin, fi.debt_ratio
            FROM mv_us_financial_indicator fi
            WHERE fi.stock_code IN (SELECT stock_code FROM peers)
              AND fi.report_type = 'annual'
              AND fi.roe IS NOT NULL
            ORDER BY fi.stock_code, fi.report_date DESC
        ),
        peer_mkt AS (
            SELECT fy.stock_code, fy.pe_ttm, fy.pb, fy.fcf_yield
            FROM mv_us_fcf_yield fy
            WHERE fy.stock_code IN (SELECT stock_code FROM peers)
        )
        SELECT
            (SELECT COUNT(*) FROM peers) AS peer_count,
            PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY pf.roe) AS median_roe,
            PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY pf.gross_margin) AS median_gross_margin,
            PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY pf.net_margin) AS median_net_margin,
            PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY pf.debt_ratio) AS median_debt_ratio,
            PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY pm.pe_ttm)
                FILTER (WHERE pm.pe_ttm > 0) AS median_pe,
            PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY pm.pb)
                FILTER (WHERE pm.pb > 0) AS median_pb,
            PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY pm.fcf_yield) AS median_fcf_yield
        FROM peers p
        LEFT JOIN peer_fin pf ON p.stock_code = pf.stock_code
        LEFT JOIN peer_mkt pm ON p.stock_code = pm.stock_code
    """
    with Connection() as conn:
        return pd.read_sql(sql, conn, params=(industry, market, exclude_code))
