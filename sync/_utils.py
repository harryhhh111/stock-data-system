"""sync/_utils.py — 共享工具函数、配置和 logger。"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime

# 确保项目根目录在 path 中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("TQDM_DISABLE", "1")

import psycopg2

from config import DBConfig
from db import upsert, execute, health_check, save_raw_snapshot
from transformers.base import transform_report_type
from incremental import (
    ensure_last_report_date_column,
    determine_stocks_to_sync,
    update_last_report_date,
)

logger = logging.getLogger("sync")


# ── sync_progress 表 ──────────────────────────────────────────


def ensure_sync_progress_table():
    """确保 sync_progress 表存在（含增量同步字段）。"""
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
            # 增量同步：添加 last_report_date 列
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
        "fetcher_cls": "fetchers.a_financial.AFinancialFetcher",
        "transformer_cls": "transformers.eastmoney.EastmoneyTransformer",
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
        "fetcher_cls": "fetchers.hk_financial.HkFinancialFetcher",
        "transformer_cls": "transformers.eastmoney_hk.EastmoneyHkTransformer",
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
        "fetcher_cls": "fetchers.us_financial.USFinancialFetcher",
        "transformer_cls": "transformers.us_gaap.USGAAPTransformer",
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
