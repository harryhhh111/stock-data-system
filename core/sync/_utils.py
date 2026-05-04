"""sync/_utils.py — 通用工具函数 + 市场配置 + 核心同步逻辑。"""

from __future__ import annotations

import logging

import psycopg2
import psycopg2.extras

from config import DBConfig
from db import upsert, execute

logger = logging.getLogger("sync")


# ── sync_progress 表 ──────────────────────────────────────────


def ensure_sync_progress_table():
    """确保 sync_progress 表存在（含增量同步字段）。"""
    from config import DBConfig

    with psycopg2.connect(DBConfig().dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS sync_progress (
                    stock_code VARCHAR(20) PRIMARY KEY,
                    market VARCHAR(10),
                    last_sync_time TIMESTAMPTZ,
                    tables_synced TEXT[],
                    status VARCHAR(20),
                    error_detail TEXT
                )
            """)
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_sync_progress_market ON sync_progress(market)"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_sync_progress_status ON sync_progress(status)"
            )
            cur.execute(
                "ALTER TABLE sync_progress ADD COLUMN IF NOT EXISTS last_report_date DATE"
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_sync_progress_last_report ON sync_progress(last_report_date)"
            )
        conn.commit()


def _em_code(stock_code: str) -> str:
    """根据 A 股代码推导东方财富代码（如 SH600519）。"""
    if stock_code.startswith("6"):
        return f"SH{stock_code}"
    elif stock_code.startswith(("0", "3")):
        return f"SZ{stock_code}"
    elif stock_code.startswith(("4", "8")):
        return f"BJ{stock_code}"
    return f"SZ{stock_code}"


# ── 市场配置注册 ──────────────────────────────────────────
MARKET_CONFIG: dict[str, dict] = {
    "CN_A": {
        "fetcher_cls": "core.fetchers.a_financial.AFinancialFetcher",
        "transformer_cls": "core.transformers.eastmoney.EastmoneyTransformer",
        "tables": ["income_statement", "balance_sheet", "cash_flow_statement"],
        "conflict_keys": ["stock_code", "report_date", "report_type"],
        "fetch_methods": ["fetch_income", "fetch_balance", "fetch_cashflow"],
        "transform_methods": [
            "transform_income",
            "transform_balance",
            "transform_cashflow",
        ],
        "fetch_kwargs_builder": lambda stock_code, fetcher: {
            "symbol": stock_code,
            "em_code": _em_code(stock_code),
        },
    },
    "CN_HK": {
        "fetcher_cls": "core.fetchers.hk_financial.HkFinancialFetcher",
        "transformer_cls": "core.transformers.eastmoney_hk.EastmoneyHkTransformer",
        "tables": ["income_statement", "balance_sheet", "cash_flow_statement"],
        "conflict_keys": ["stock_code", "report_date", "report_type"],
        "fetch_methods": ["fetch_income", "fetch_balance", "fetch_cashflow"],
        "transform_methods": [
            "transform_income",
            "transform_balance",
            "transform_cashflow",
        ],
        "fetch_kwargs_builder": lambda stock_code, fetcher: {"stock_code": stock_code},
    },
    "US": {
        "fetcher_cls": "core.fetchers.us_financial.USFinancialFetcher",
        "transformer_cls": "core.transformers.us_gaap.USGAAPTransformer",
        "tables": ["us_income_statement", "us_balance_sheet", "us_cash_flow_statement"],
        "conflict_keys": ["stock_code", "report_date", "report_type"],
        "fetch_methods": ["fetch_income", "fetch_balance", "fetch_cashflow"],
        "transform_methods": [
            "transform_income",
            "transform_balance",
            "transform_cashflow",
        ],
        "special": "us",
    },
}


# ── 同步单只股票（核心函数）─────────────────────────────────


def sync_one_stock(stock_code: str, market: str, save_snapshot: bool = True) -> tuple[bool, list[str], list[str], str | None]:
    """同步单只股票的三大报表（通用版）。

    支持 CN_A、CN_HK 市场。US 市场走 sync_us_market 特殊路径。

    Args:
        stock_code: 股票代码
        market: 市场标识
        save_snapshot: 是否保存原始 API 响应到 raw_snapshot（增量同步设为 False）

    Returns:
        (ok, tables_synced, tables_failed, error)
        ok: 至少有一张表同步成功
        tables_synced: 成功同步的表名列表
        tables_failed: 预期但未成功同步的表名列表
    """
    cfg = MARKET_CONFIG.get(market)
    if cfg is None:
        return False, [], [], f"不支持的市场: {market}"
    if cfg.get("special"):
        return False, [], [], f"市场 {market} 需要走专用同步路径"

    expected_tables: list[str] = cfg["tables"]
    tables_synced: list[str] = []
    tables_failed: list[str] = []

    try:
        # 动态导入
        parts = cfg["fetcher_cls"].rsplit(".", 1)
        module = __import__(parts[0], fromlist=[parts[1]])
        fetcher_cls = getattr(module, parts[1])
        fetcher = fetcher_cls()
        if not save_snapshot:
            fetcher.skip_snapshot = True

        parts = cfg["transformer_cls"].rsplit(".", 1)
        module = __import__(parts[0], fromlist=[parts[1]])
        transformer_cls = getattr(module, parts[1])
        transformer = transformer_cls()

        fetch_kwargs = cfg["fetch_kwargs_builder"](stock_code, fetcher)

        # Fetch + Transform + Upsert 三步走
        for fetch_method, transform_method, table, conflict_keys in zip(
            cfg["fetch_methods"],
            cfg["transform_methods"],
            expected_tables,
            [cfg["conflict_keys"]] * len(expected_tables),
        ):
            try:
                raw_df = getattr(fetcher, fetch_method)(**fetch_kwargs)
                if raw_df is None or raw_df.empty:
                    tables_failed.append(table)
                    continue
                records = getattr(transformer, transform_method)(raw_df)
                if records:
                    upsert(table, records, conflict_keys)
                    tables_synced.append(table)
                else:
                    tables_failed.append(table)
            except Exception as exc:
                logger.warning("%s %s 失败: %s", stock_code, table, exc)
                tables_failed.append(table)

        return (len(tables_synced) > 0, tables_synced, tables_failed, None)

    except Exception as exc:
        error_msg = f"{type(exc).__name__}: {exc}"
        logger.error("%s (%s) 同步失败: %s", stock_code, market, error_msg)
        return False, tables_synced, [t for t in expected_tables if t not in tables_synced], error_msg


def correct_market_cap(records: list[dict]) -> int:
    """用 stock_share.total_shares × close 修复异常的 market_cap。

    当行情 API 返回的市值与 close × total_shares 偏差 > 10% 时，
    以自行计算值覆盖（API 偶发总股本数据错乱）。

    Returns:
        修正的记录数
    """
    if not records:
        return 0

    # 收集需要查询的 stock_code
    codes = list({r["stock_code"] for r in records if r.get("market") and r.get("close")})
    if not codes:
        return 0

    # 批量查询 total_shares（取每只股票最新一条记录）
    shares_map: dict[str, float] = {}
    try:
        rows = execute(
            """SELECT DISTINCT ON (stock_code) stock_code, total_shares
               FROM stock_share
               WHERE stock_code = ANY(%s) AND total_shares IS NOT NULL AND total_shares > 0
               ORDER BY stock_code, trade_date DESC""",
            ([codes],),
            fetch=True,
        )
        if rows:
            for r in rows:
                shares_map[r[0]] = float(r[1])
    except Exception:
        return 0

    if not shares_map:
        return 0

    corrected = 0
    for rec in records:
        shares = shares_map.get(rec.get("stock_code", ""))
        if not shares:
            continue
        close = rec.get("close")
        if close is None or close == 0:
            continue
        expected_mcap = close * shares
        orig_mcap = rec.get("market_cap")
        if orig_mcap is None:
            rec["market_cap"] = expected_mcap
            corrected += 1
        elif abs(float(orig_mcap) - expected_mcap) / expected_mcap > 0.1:
            rec["market_cap"] = expected_mcap
            corrected += 1

    if corrected:
        logger.info("市值修正: %d 条记录", corrected)
    return corrected


# ── 物化视图自动刷新 ──────────────────────────────────────────

# 各操作需要刷新的视图
_REFRESH_MAP: dict[str, list[str]] = {
    "financial": ["mv_financial_indicator", "mv_indicator_ttm", "mv_fcf_yield"],
    "daily": ["mv_fcf_yield"],
    "dividend": [],  # dividend_split 暂无视图依赖
}


def refresh_views_after_sync(sync_type: str) -> None:
    """同步完成后自动刷新相关物化视图。

    根据同步类型决定刷新哪些视图，避免不必要的全量刷新。
    """
    views = _REFRESH_MAP.get(sync_type)
    if not views:
        return

    import time as _time

    logger.info("同步后自动刷新物化视图: %s", ", ".join(views))
    for v in views:
        try:
            t0 = _time.time()
            execute(f"REFRESH MATERIALIZED VIEW CONCURRENTLY {v}", commit=True)
            elapsed = _time.time() - t0
            logger.info("  %s 刷新完成 (%.1fs)", v, elapsed)
        except Exception as exc:
            logger.warning("  %s 刷新失败（可能无数据或未创建）: %s", v, exc)
