"""回测数据预加载 — 一次加载，内存 PIT 过滤。"""

from __future__ import annotations

import io
from datetime import date

import pandas as pd
from db import Connection


def _copy_df(conn, sql: str, str_cols: tuple[str, ...] = ()) -> pd.DataFrame:
    """COPY CSV 快速加载，str_cols 指定需要强制字符串的列。"""
    buf = io.StringIO()
    cur = conn.cursor()
    cur.copy_expert(f"COPY ({sql}) TO STDOUT WITH CSV HEADER", buf)
    cur.close()
    buf.seek(0)
    dtype = {c: str for c in str_cols}
    df = pd.read_csv(buf, dtype=dtype or None)
    buf.close()
    return df


class PITPreloader:
    """一次性加载财务 / TTM / 股本 / 信息到内存，pandas 做 PIT。"""

    def __init__(self, market: str) -> None:
        if market not in ("CN_A", "CN_HK"):
            raise ValueError(f"仅支持 CN_A / CN_HK，收到: {market}")
        self.market = market

    def load(self) -> None:
        """加载并预排序全部静态数据。"""
        with Connection() as conn:
            # ── 财务指标（只取回测用到的列，省 ~30% 加载时间）──
            self.fin = _copy_df(
                conn,
                "SELECT stock_code, report_date, report_type, notice_date,"
                "  roe, gross_margin, operating_margin, net_margin,"
                "  debt_ratio, current_ratio, quick_ratio,"
                "  parent_equity, total_equity, total_assets, total_liab,"
                "  eps_basic, eps_diluted, revenue_yoy, net_profit_yoy, fcf"
                " FROM mv_financial_indicator",
                str_cols=("stock_code", "report_type"),
            )
            self.fin["notice_date"] = pd.to_datetime(
                self.fin["notice_date"], errors="coerce"
            ).dt.date
            self.fin["report_date"] = pd.to_datetime(
                self.fin["report_date"], errors="coerce"
            ).dt.date
            # 按 stock_code asc, report_date desc 预排序
            self.fin = self.fin.sort_values(
                ["stock_code", "report_date"], ascending=[True, False]
            )

            # ── TTM ──
            self.ttm = _copy_df(
                conn,
                "SELECT * FROM mv_indicator_ttm_hist",
                str_cols=("stock_code", "report_type"),
            )
            self.ttm["notice_date"] = pd.to_datetime(
                self.ttm["notice_date"], errors="coerce"
            ).dt.date
            self.ttm["report_date"] = pd.to_datetime(
                self.ttm["report_date"], errors="coerce"
            ).dt.date
            self.ttm = self.ttm.sort_values(
                ["stock_code", "report_date"], ascending=[True, False]
            )

            # ── 股本 ──
            self.shares = _copy_df(
                conn,
                "SELECT stock_code, trade_date, total_shares FROM stock_share",
                str_cols=("stock_code",),
            )
            self.shares["trade_date"] = pd.to_datetime(
                self.shares["trade_date"], errors="coerce"
            ).dt.date
            self.shares = self.shares.sort_values(
                ["stock_code", "trade_date"], ascending=[True, False]
            )

            # ── 股票信息 ──
            self.info = _copy_df(
                conn,
                f"SELECT stock_code, stock_name, market, industry, list_date "
                f"FROM stock_info WHERE market = '{self.market}'",
                str_cols=("stock_code", "stock_name", "industry", "market"),
            )
            self.info["list_date"] = pd.to_datetime(
                self.info["list_date"], errors="coerce"
            ).dt.date

    # ── PIT 查询 ────────────────────────────────────────────

    def get_universe(self, as_of_date: date) -> pd.DataFrame:
        """内存构建 PIT 选股池的财务部分（不含行情）。"""
        # latest_annual
        annual = self.fin[
            (self.fin["report_type"] == "annual")
            & (self.fin["notice_date"] <= as_of_date)
        ]
        latest_annual = annual.drop_duplicates(subset="stock_code", keep="first")

        # latest_quarterly_yoy
        q = self.fin[
            (self.fin["report_type"] == "quarterly")
            & (self.fin["notice_date"] <= as_of_date)
            & self.fin["revenue_yoy"].notna()
        ]
        latest_q = q.drop_duplicates(subset="stock_code", keep="first")[
            ["stock_code", "revenue_yoy", "net_profit_yoy"]
        ]

        # latest TTM
        ttm_valid = self.ttm[self.ttm["notice_date"] <= as_of_date]
        latest_ttm = ttm_valid.drop_duplicates(subset="stock_code", keep="first")

        # latest stock_share
        sh = self.shares[self.shares["trade_date"] <= as_of_date]
        latest_shares = sh.drop_duplicates(subset="stock_code", keep="first")

        # 以 stock_info 为底，只取需要的列避免冲突
        result = self.info[
            ["stock_code", "stock_name", "market", "industry", "list_date"]
        ].copy()

        la_cols = [
            "stock_code", "roe", "gross_margin", "operating_margin", "net_margin",
            "debt_ratio", "current_ratio", "quick_ratio",
            "parent_equity", "total_equity", "total_assets", "total_liab",
            "eps_basic", "eps_diluted",
            "revenue_yoy", "net_profit_yoy", "fcf",
        ]
        result = result.merge(
            latest_annual[la_cols], on="stock_code", how="left"
        )
        result = result.merge(
            latest_q, on="stock_code", how="left", suffixes=("", "_q")
        )
        result = result.merge(
            latest_ttm[
                ["stock_code", "revenue_ttm", "net_profit_ttm", "cfo_ttm",
                 "capex_ttm", "report_date"]
            ],
            on="stock_code", how="left",
        )
        result = result.merge(
            latest_shares[["stock_code", "total_shares"]],
            on="stock_code", how="left",
        )

        # COALESCE YoY
        if "revenue_yoy_q" in result.columns:
            result["revenue_yoy"] = result["revenue_yoy"].fillna(
                result["revenue_yoy_q"]
            )
        if "net_profit_yoy_q" in result.columns:
            result["net_profit_yoy"] = result["net_profit_yoy"].fillna(
                result["net_profit_yoy_q"]
            )
        result = result.drop(
            columns=[c for c in result.columns if c.endswith("_q")],
            errors="ignore",
        )

        result["annual_fcf"] = result["fcf"]

        # days_since_list（list_date 可能全为 NaT）
        ld = result["list_date"]
        if ld.notna().any():
            result["days_since_list"] = (as_of_date - ld).dt.days
        else:
            result["days_since_list"] = None

        return result

    def get_roe_history(self, as_of_date: date, years: int = 3) -> pd.DataFrame:
        """PIT 连续年 ROE。"""
        annual = self.fin[
            (self.fin["report_type"] == "annual")
            & self.fin["roe"].notna()
            & (self.fin["notice_date"] <= as_of_date)
        ]
        ranked = annual.copy()
        ranked["rn"] = ranked.groupby("stock_code").cumcount() + 1
        result = ranked[ranked["rn"] <= years][
            ["stock_code", "report_date", "roe"]
        ]
        return result.sort_values(
            ["stock_code", "report_date"], ascending=[True, False]
        )
